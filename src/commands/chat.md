---
description: Chat with Gemini via gemini-image (text input, text output)
argument-hint: <prompt>
---

You are a command handler for gemini-image chat. Send the user's prompt to Gemini and return the text response.

## Usage

```bash
gemini-image chat "<prompt>"
```

Or via HTTP API:

```bash
curl -X POST http://localhost:8070/api/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "<prompt>"}'
```

## Rules

1. Pass the user's prompt directly to the command
2. The response is plain text from Gemini
3. Typical response time: 5-30 seconds

## Examples

```
/chat What is quantum computing?
→ gemini-image chat "What is quantum computing?"

/chat 解釋量子力學
→ gemini-image chat "解釋量子力學"
```

User input: $ARGUMENTS
