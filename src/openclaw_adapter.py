"""
OpenClaw / Gemini API adapter.

把 openclaw (或任何走 google-generative-ai 協議的客戶端) 送來的
完整 request body 攤平成 Gemini Web 看得懂的單段 prompt,並把 Gemini 回的
純文字解析回 functionCall / text part。

設計原則:
- 完全無狀態。每次呼叫都把整個對話歷史攤平,不在這裡記憶任何東西。
- openclaw 是唯一的對話真實來源,Gemini Web 每次互動前 new_chat。
"""
from __future__ import annotations

import json
import re
from typing import Any


# ── Tool schema 格式化 ──────────────────────────────────────────────


def _format_tool_schema(func_decl: dict[str, Any]) -> str:
    """把單一 functionDeclaration 格式化成可讀的文字描述。"""
    name = func_decl.get("name", "<unknown>")
    desc = func_decl.get("description", "").strip()
    params = func_decl.get("parameters", {}) or {}
    props = params.get("properties", {}) or {}
    required = set(params.get("required", []) or [])

    arg_lines = []
    for arg_name, arg_schema in props.items():
        arg_type = arg_schema.get("type", "any")
        arg_desc = arg_schema.get("description", "").strip()
        marker = "" if arg_name in required else "?"
        line = f"    - {arg_name}{marker} ({arg_type})"
        if arg_desc:
            line += f": {arg_desc}"
        arg_lines.append(line)

    block = [f"- {name}"]
    if desc:
        block.append(f"  description: {desc}")
    if arg_lines:
        block.append("  arguments:")
        block.extend(arg_lines)
    else:
        block.append("  arguments: (none)")
    return "\n".join(block)


def _extract_function_declarations(tools: list[Any]) -> list[dict[str, Any]]:
    """從 tools 陣列裡撈出所有 functionDeclarations,忽略 google_search 等內建工具。"""
    decls: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        for fd in tool.get("functionDeclarations", []) or []:
            if isinstance(fd, dict) and fd.get("name"):
                decls.append(fd)
    return decls


# ── 訊息歷史攤平 ──────────────────────────────────────────────────────


def _stringify_part(part: dict[str, Any]) -> str:
    """把 Gemini API 的 part 物件轉成可讀文字。"""
    if "text" in part:
        return str(part["text"])

    if "functionCall" in part:
        fc = part["functionCall"] or {}
        name = fc.get("name", "<unknown>")
        args = fc.get("args", {})
        try:
            args_json = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            args_json = str(args)
        return f"[tool_call] {name}({args_json})"

    if "functionResponse" in part:
        fr = part["functionResponse"] or {}
        name = fr.get("name", "<unknown>")
        response = fr.get("response", {})
        try:
            resp_json = json.dumps(response, ensure_ascii=False)
        except (TypeError, ValueError):
            resp_json = str(response)
        return f"[tool_result:{name}] {resp_json}"

    if "inlineData" in part:
        mime = (part.get("inlineData") or {}).get("mimeType", "binary")
        return f"[inline_data:{mime}]"

    return ""


def _role_label(role: str) -> str:
    if role == "user":
        return "User"
    if role == "model" or role == "assistant":
        return "Assistant"
    if role == "function" or role == "tool":
        return "Tool"
    if role == "system":
        return "System"
    return role.capitalize() if role else "User"


def _flatten_history(contents: list[dict[str, Any]]) -> str:
    """把 contents 陣列攤平成多段文字對話歷史。"""
    lines: list[str] = []
    for content in contents or []:
        role = content.get("role", "user")
        parts = content.get("parts", []) or []
        chunks = [s for s in (_stringify_part(p) for p in parts) if s]
        if not chunks:
            continue
        body = "\n".join(chunks)
        lines.append(f"{_role_label(role)}: {body}")
    return "\n\n".join(lines)


def _extract_system_text(system_instruction: Any) -> str:
    """systemInstruction 可以是 dict (有 parts) 或 str。"""
    if not system_instruction:
        return ""
    if isinstance(system_instruction, str):
        return system_instruction.strip()
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts", []) or []
        texts = []
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                texts.append(str(p["text"]))
        return "\n".join(texts).strip()
    return ""


# ── Tool call prompt 模板 ─────────────────────────────────────────────


_TOOL_CALL_INSTRUCTION = """\
[TOOL PROTOCOL — READ CAREFULLY]

You are running inside a custom agent runtime. The following tools — and ONLY these tools — are available to you:

{tool_schemas}

Allowed tool names (exact match required): {tool_names}

CRITICAL RULES:
1. You MUST NOT call any built-in tools (e.g. `google:search`, `google_search`, `tool_code`, `code_execution`, `image_generation`). They do not exist in this runtime and will fail.
2. If you want to call a tool, you MUST use one of the names in the allowed list above, with the exact spelling.
3. To call a tool, your ENTIRE response must be a single JSON object on one line, no markdown, no code fences, no prose before or after:
   {{"tool_call": {{"name": "<one_of_the_allowed_names>", "args": {{<arguments_matching_the_schema>}}}}}}
4. If you do NOT need any tool, respond in plain natural language as usual.
5. Choose EXACTLY ONE: either output the tool_call JSON object, or output plain text. Never both.
6. If the user's request requires capabilities not covered by the allowed tools, respond in plain text explaining what you would need.
"""


def build_prompt(body: dict[str, Any]) -> tuple[str, bool, set[str]]:
    """
    把 openclaw / Gemini API 的完整 request body 組成一段給 Gemini Web 的 prompt。

    Returns:
        (prompt_text, has_function_tools, allowed_tool_names)
        - has_function_tools 為 True 代表注入了工具呼叫指令
        - allowed_tool_names 是宣告的工具名稱集合,用於後續驗證 tool call
    """
    contents = body.get("contents", []) or []
    system_text = _extract_system_text(body.get("systemInstruction"))
    func_decls = _extract_function_declarations(body.get("tools", []) or [])

    sections: list[str] = []

    if system_text:
        sections.append(f"[System Instruction]\n{system_text}")

    has_func_tools = bool(func_decls)
    allowed_names: set[str] = {fd["name"] for fd in func_decls}
    if has_func_tools:
        tool_schemas = "\n\n".join(_format_tool_schema(fd) for fd in func_decls)
        tool_names_str = ", ".join(f"`{fd['name']}`" for fd in func_decls)
        sections.append(_TOOL_CALL_INSTRUCTION.format(
            tool_schemas=tool_schemas, tool_names=tool_names_str
        ))

    history = _flatten_history(contents)
    if history:
        sections.append(f"[Conversation]\n{history}")

    sections.append("Assistant:")
    return "\n\n".join(sections), has_func_tools, allowed_names


# ── 回應解析 (text → parts) ───────────────────────────────────────────


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 或 ``` ... ``` 包裹。"""
    text = text.strip()
    m = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)\s*```", text)
    if m:
        return m.group(1).strip()
    return text


def _try_extract_json_object(text: str) -> dict[str, Any] | None:
    """從文字裡盡量擷取一個合法的 JSON object。多策略容錯。"""
    cleaned = _strip_code_fence(text)

    # 策略 1: 整段直接 parse
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # 策略 2: 抓第一對 balanced 大括號 (簡單版,不處理字串中的 brace)
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def parse_tool_call(
    text: str, allowed_names: set[str] | None = None
) -> dict[str, Any] | None:
    """
    嘗試把 Gemini 的純文字回應解析成 tool call。

    支援的格式:
        {"tool_call": {"name": "...", "args": {...}}}
        {"name": "...", "args": {...}}        # 寬鬆 fallback
        {"name": "...", "arguments": {...}}   # 寬鬆 fallback

    Args:
        text: Gemini 回應的純文字
        allowed_names: 若提供,只接受名字在此集合內的 tool call;否則退回 None。
                       這是第二道防線,防止 Gemini 呼叫內建工具 (如 google:search)。

    Returns:
        {"name": str, "args": dict}  或 None (若解析失敗、不是 tool call、或名稱不在白名單)
    """
    if not text or not text.strip():
        return None

    obj = _try_extract_json_object(text)
    if obj is None:
        return None

    candidate: dict[str, Any] | None = None

    # 標準格式
    if "tool_call" in obj and isinstance(obj["tool_call"], dict):
        tc = obj["tool_call"]
        name = tc.get("name")
        args = tc.get("args") or tc.get("arguments") or {}
        if isinstance(name, str) and name:
            candidate = {"name": name, "args": args if isinstance(args, dict) else {}}

    # 寬鬆 fallback: 整個 obj 就是 {name, args}
    if candidate is None and "name" in obj and isinstance(obj["name"], str):
        args = obj.get("args") or obj.get("arguments") or {}
        # 避免把普通 JSON 物件誤判為 tool call: 必須有 args 鍵
        if "args" in obj or "arguments" in obj:
            candidate = {"name": obj["name"], "args": args if isinstance(args, dict) else {}}

    if candidate is None:
        return None

    # 第二道防線: 名稱必須在白名單內
    if allowed_names is not None and candidate["name"] not in allowed_names:
        return None

    return candidate


def build_response_parts(
    text: str,
    has_function_tools: bool,
    allowed_tool_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    把 Gemini Web 的純文字回應包成 Gemini API 的 parts 陣列。

    Args:
        text: Gemini 純文字回應
        has_function_tools: 本次請求是否注入了工具
        allowed_tool_names: 宣告的工具名稱集合,用於驗證 tool call name

    Returns:
        (parts, finish_reason)
        若解析出 tool call,parts 會是 [{"functionCall": {...}}]
    """
    if has_function_tools:
        tc = parse_tool_call(text, allowed_names=allowed_tool_names)
        if tc is not None:
            return (
                [{"functionCall": {"name": tc["name"], "args": tc["args"]}}],
                "STOP",
            )

    return ([{"text": text or ""}], "STOP")
