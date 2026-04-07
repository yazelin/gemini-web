# OpenClaw Integration Guide

How to drive an [openclaw](https://github.com/openclaw/openclaw) agent
through the Gemini Web subscription via this server, instead of paying
for the Gemini or Anthropic API.

This guide assumes you already have:

- openclaw installed and a working agent (e.g. `Mori`) configured to talk
  through Telegram or Discord
- A Google account with Gemini Web access (free or Pro/Ultra subscription)
- This repo cloned and installed (`uv sync` + `playwright install chromium`)

## Mental model

```
User on Telegram
   ↓
openclaw gateway (systemd user service)
   ↓ HTTP, google-generative-ai protocol
gemini-web FastAPI server  ← this repo
   ↓ openclaw_adapter: flatten history + tools into one prompt
   ↓ Playwright
gemini.google.com (logged-in browser session)
```

openclaw treats gemini-web as just another LLM provider. The
`openclaw_adapter` module is the translation layer that turns Gemini
API request bodies (with `systemInstruction`, `tools.functionDeclarations`
and a multi-turn `contents[]`) into a single flat prompt that the
Gemini Web chat UI understands, and parses the plain-text reply back
into Gemini API parts (`text` or `functionCall`).

The adapter is **completely stateless**. openclaw remains the only
source of truth for conversation history; gemini-web starts a fresh
chat for every request.

## Step 1: log in once

```bash
HEADLESS=false uv run gemini-web login
```

Manually sign in to your Google account in the Chromium window that
opens. The session is persisted under `~/.gemini-web/profiles/` and
reused across server restarts.

## Step 2: run the server

```bash
HEADLESS=true DEFAULT_TIMEOUT=480 \
  uv run uvicorn src.main:app --host 127.0.0.1 --port 8070
```

`DEFAULT_TIMEOUT=480` gives Playwright up to 8 minutes to wait for
Gemini Web to start producing a response. Long contexts and occasional
backend tail latency mean the default 240 s isn't always enough.

Quick smoke tests:

```bash
# Health
curl -s http://127.0.0.1:8070/api/health

# Plain chat through the Gemini-API-compatible endpoint
curl -sX POST http://127.0.0.1:8070/v1beta/models/gemini-2.5-pro:generateContent \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"hello"}]}]}'
```

## Step 3: register the provider in openclaw

Edit `~/.openclaw/openclaw.json` and add a `models` block at the top
level (alongside `agents`, `auth`, etc.). **Back up the file first.**

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "gemini-web-local": {
        "baseUrl": "http://127.0.0.1:8070",
        "auth": "api-key",
        "apiKey": "dummy-not-used",
        "api": "google-generative-ai",
        "models": [
          {
            "id": "gemini-2.5-pro",
            "name": "Gemini 2.5 Pro (Web)",
            "api": "google-generative-ai",
            "reasoning": false,
            "input": ["text"],
            "contextWindow": 1000000,
            "maxTokens": 8192,
            "compat": {
              "supportsTools": true
            }
          }
        ]
      }
    }
  }
}
```

The `apiKey` is required by the schema but ignored by gemini-web
(unless you set `API_KEYS` in the environment, which is the only way
to enforce auth on the server side).

`baseUrl` is the **API root**, not including `/v1beta`. openclaw
appends `/models/<id>:generateContent` and `/models/<id>:streamGenerateContent`
itself; the server mounts both `/v1beta/models/...` and `/models/...`
so either prefix works.

## Step 4: point an agent at the new provider

```json
{
  "agents": {
    "list": [
      {
        "id": "main",
        "default": true,
        "name": "Mori",
        "model": "gemini-web-local/gemini-2.5-pro"
      }
    ]
  }
}
```

Validate and reload:

```bash
openclaw doctor              # config schema check
openclaw gateway restart     # reload running service
```

## Step 5: verify

Send a message through Telegram (or whatever channel the agent listens
on) and watch the server log:

```bash
tail -f /tmp/gemini-web.log     # or wherever you redirected logs
```

You should see lines like:

```
openclaw request: prompt=12345 chars, turns=1, tools=26, has_tool_call=True
已送出 chat prompt：[System Instruction]...
偵測到 model-response
已重置對話（導航至首頁）
POST /models/gemini-2.5-pro%3AstreamGenerateContent?alt=sse HTTP/1.1 200 OK
```

If the same request also shows up in your Anthropic/Google API
dashboards as billable usage, something is misrouted — double-check
the agent's `model` field and that openclaw doctor reports
`gemini-web-local` as a known provider.

## How tool calling works

Gemini Web has no native function-calling. The adapter fakes it:

1. When openclaw sends `tools.functionDeclarations`, the adapter
   formats every declared tool into a Markdown block and prepends a
   strict tool protocol to the prompt: "respond with ONLY this exact
   JSON shape — `{\"tool_call\": {\"name\": ..., \"args\": ...}}`,
   you may NOT call built-in tools like `google:search`".
2. Multi-turn history is flattened too: a `model` part with a
   `functionCall` becomes `[tool_call] name(args_json)`, a `function`
   part with a `functionResponse` becomes `[tool_result:name] body`.
3. The Gemini reply is fed through a permissive JSON extractor that
   strips Markdown code fences and tolerates surrounding prose.
4. If a `tool_call` is found **and** its `name` is in the original
   `functionDeclarations` allowlist, the adapter wraps it as a
   `functionCall` part. Otherwise it falls back to a plain `text`
   part. The allowlist is the second line of defence against Gemini
   trying to call its own built-in `google:search` tool.

End result: openclaw sees a normal Gemini API response and dispatches
the tool call as if it had come from the real API. The tool result
is sent back on the next turn and the cycle continues until Gemini
produces a plain-text reply.

## Known limitations

- **No real streaming.** `streamGenerateContent` runs the full
  non-streaming flow, then emits the entire result as one SSE chunk.
  openclaw is happy with this, but you don't get partial output.
- **No image input.** Inline images in `contents[].parts[].inlineData`
  are tagged as `[inline_data:mime]` in the flattened prompt — the
  server has no way to forward them into the Gemini Web chat box.
- **No parallel tool calls.** The adapter expects exactly one
  `tool_call` per turn. Gemini API supports multiple calls per turn
  but the prompt protocol explicitly forbids that to keep parsing
  simple.
- **No usage metrics.** `lastCallUsage` always reports 0 because the
  Gemini Web subscription doesn't expose token counts. openclaw will
  show 0 cost which is technically correct but uninformative.
- **Random tail latency.** Gemini Web occasionally takes 30–60 s to
  start responding even for short prompts. Empirically this is
  uncorrelated with prompt size or context length — it's just noise
  from Google's backend. The 480 s `DEFAULT_TIMEOUT` is the safety
  net; openclaw may give up sooner depending on its own per-call
  timeout.
- **Single browser, sequential.** The default `WORKER_COUNT=1` means
  only one openclaw turn can run at a time. Set `WORKER_COUNT=N` and
  log in each profile separately (`gemini-web login --worker N`) if
  you need parallelism for multiple agents.

## Reverting

If anything goes sideways:

```bash
cp ~/.openclaw/openclaw.json.before-gemini-web ~/.openclaw/openclaw.json
openclaw gateway restart
```

(Assuming you backed up the original file before editing — and you
should.)
