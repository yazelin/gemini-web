"""openclaw_adapter unit tests — 純函式,不需要 Playwright。"""
from src.openclaw_adapter import (
    build_prompt,
    build_response_parts,
    parse_tool_call,
)


# ── build_prompt ─────────────────────────────────────────────────────


def test_build_prompt_simple_text_no_tools():
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "Hello"}]},
        ]
    }
    prompt, has_tools, _ = build_prompt(body)
    assert has_tools is False
    assert "User: Hello" in prompt
    assert "Assistant:" in prompt
    assert "tool_call" not in prompt


def test_build_prompt_with_system_instruction():
    body = {
        "systemInstruction": {"parts": [{"text": "You are Mori, a helpful agent."}]},
        "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
    }
    prompt, _, _ = build_prompt(body)
    assert "[System Instruction]" in prompt
    assert "Mori" in prompt
    assert prompt.index("[System Instruction]") < prompt.index("User: Hi")


def test_build_prompt_system_instruction_as_string():
    body = {
        "systemInstruction": "Plain string system prompt.",
        "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
    }
    prompt, _, _ = build_prompt(body)
    assert "Plain string system prompt." in prompt


def test_build_prompt_with_function_tools():
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Search Python"}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "web_search",
                        "description": "Search the web",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        ],
    }
    prompt, has_tools, _ = build_prompt(body)
    assert has_tools is True
    assert "web_search" in prompt
    assert "query" in prompt
    assert "tool_call" in prompt
    assert "Search the web" in prompt


def test_build_prompt_flattens_multi_turn_with_tool_history():
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "Search Python"}]},
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "web_search", "args": {"query": "Python"}}}],
            },
            {
                "role": "function",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "web_search",
                            "response": {"results": ["python.org"]},
                        }
                    }
                ],
            },
            {"role": "user", "parts": [{"text": "Now summarize"}]},
        ],
        "tools": [
            {
                "functionDeclarations": [
                    {"name": "web_search", "parameters": {}}
                ]
            }
        ],
    }
    prompt, has_tools, _ = build_prompt(body)
    assert has_tools is True
    assert "Search Python" in prompt
    assert "[tool_call] web_search" in prompt
    assert "[tool_result:web_search]" in prompt
    assert "python.org" in prompt
    assert "Now summarize" in prompt
    # 順序正確
    assert prompt.index("Search Python") < prompt.index("[tool_call]")
    assert prompt.index("[tool_call]") < prompt.index("[tool_result")
    assert prompt.index("[tool_result") < prompt.index("Now summarize")


# ── parse_tool_call ──────────────────────────────────────────────────


def test_parse_tool_call_standard_format():
    text = '{"tool_call": {"name": "web_search", "args": {"query": "AI"}}}'
    tc = parse_tool_call(text)
    assert tc == {"name": "web_search", "args": {"query": "AI"}}


def test_parse_tool_call_wrapped_in_code_fence():
    text = '```json\n{"tool_call": {"name": "x", "args": {"a": 1}}}\n```'
    tc = parse_tool_call(text)
    assert tc == {"name": "x", "args": {"a": 1}}


def test_parse_tool_call_with_surrounding_text():
    text = 'Sure! Here is the call:\n{"tool_call": {"name": "x", "args": {}}}\nDone.'
    tc = parse_tool_call(text)
    assert tc == {"name": "x", "args": {}}


def test_parse_tool_call_loose_format_with_args():
    text = '{"name": "web_search", "args": {"query": "AI"}}'
    tc = parse_tool_call(text)
    assert tc == {"name": "web_search", "args": {"query": "AI"}}


def test_parse_tool_call_loose_format_with_arguments_alias():
    text = '{"name": "x", "arguments": {"k": "v"}}'
    tc = parse_tool_call(text)
    assert tc == {"name": "x", "args": {"k": "v"}}


def test_parse_tool_call_plain_text_returns_none():
    assert parse_tool_call("Sure, the answer is 42.") is None


def test_parse_tool_call_random_json_object_not_a_tool_call():
    # 沒有 name 也沒有 tool_call 鍵 → 不是 tool call
    assert parse_tool_call('{"foo": "bar"}') is None


def test_parse_tool_call_object_with_only_name_not_a_tool_call():
    # 有 name 但沒有 args/arguments → 不應誤判
    assert parse_tool_call('{"name": "John", "age": 30}') is None


def test_parse_tool_call_empty_string():
    assert parse_tool_call("") is None
    assert parse_tool_call("   ") is None


# ── build_response_parts ─────────────────────────────────────────────


def test_build_response_parts_plain_text_no_tools():
    parts, finish = build_response_parts("Hello world", has_function_tools=False)
    assert parts == [{"text": "Hello world"}]
    assert finish == "STOP"


def test_build_response_parts_text_when_tools_enabled_but_no_call():
    parts, finish = build_response_parts(
        "I think the answer is 42.", has_function_tools=True
    )
    assert parts == [{"text": "I think the answer is 42."}]
    assert finish == "STOP"


def test_build_response_parts_tool_call_detected():
    text = '{"tool_call": {"name": "web_search", "args": {"query": "x"}}}'
    parts, finish = build_response_parts(text, has_function_tools=True)
    assert parts == [
        {"functionCall": {"name": "web_search", "args": {"query": "x"}}}
    ]
    assert finish == "STOP"


def test_build_response_parts_tool_call_ignored_when_tools_disabled():
    # 沒注入工具就不該嘗試解析,即使內容看起來像 tool call
    text = '{"tool_call": {"name": "x", "args": {}}}'
    parts, _ = build_response_parts(text, has_function_tools=False)
    assert parts == [{"text": text}]


# ── 白名單防線 (防止 Gemini 亂叫內建工具) ─────────────────────────────


def test_parse_tool_call_rejects_unknown_tool_name():
    # Gemini 自己叫了 google:search,但我們只宣告 web_search
    text = '{"tool_call": {"name": "google:search", "args": {"query": "x"}}}'
    assert parse_tool_call(text, allowed_names={"web_search"}) is None


def test_parse_tool_call_accepts_whitelisted_name():
    text = '{"tool_call": {"name": "web_search", "args": {"query": "x"}}}'
    tc = parse_tool_call(text, allowed_names={"web_search"})
    assert tc == {"name": "web_search", "args": {"query": "x"}}


def test_build_response_parts_unknown_tool_falls_back_to_text():
    text = '{"tool_call": {"name": "google:search", "args": {"queries": ["x"]}}}'
    parts, _ = build_response_parts(
        text, has_function_tools=True, allowed_tool_names={"web_search"}
    )
    # 因為名稱不在白名單,退回純文字
    assert parts == [{"text": text}]


def test_build_prompt_returns_allowed_names_set():
    body = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {"name": "web_search", "parameters": {}},
                    {"name": "image_gen", "parameters": {}},
                ]
            }
        ],
    }
    _, _, names = build_prompt(body)
    assert names == {"web_search", "image_gen"}
