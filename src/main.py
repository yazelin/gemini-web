"""FastAPI 應用程式入口"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import settings
from .openclaw_adapter import build_prompt, build_response_parts
from .worker_pool import WorkerPool, QueueFullError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

worker_pool = WorkerPool(
    worker_count=settings.worker_count,
    max_waiting=settings.queue_max_size,
)
_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服務生命週期：啟動 worker pool，結束時清理"""
    await worker_pool.start()
    logger.info("服務已啟動，%d 個 worker，port %d", settings.worker_count, settings.port)
    yield
    await worker_pool.stop()


app = FastAPI(title="Gemini Image API", lifespan=lifespan)


# ── Request / Response 模型 ──


class GenerateRequest(BaseModel):
    prompt: str
    timeout: int = settings.default_timeout


class ChatRequest(BaseModel):
    prompt: str
    timeout: int = 120


# ── 端點 ──


@app.post("/api/generate")
async def api_generate(req: GenerateRequest):
    """生成圖片"""
    try:
        result = await worker_pool.dispatch("generate", req.prompt, "", req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    return result


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """文字對話"""
    try:
        result = await worker_pool.dispatch("chat", req.prompt, "", req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    return result


@app.get("/api/health")
async def api_health():
    """健康檢查"""
    statuses = await worker_pool.worker_status()
    alive_count = sum(1 for s in statuses if s["alive"])
    logged_in_count = sum(1 for s in statuses if s["alive"] and s["logged_in"])
    total = len(statuses)

    if alive_count == 0:
        status = "down"
    elif logged_in_count < total:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "workers": statuses,
        "workers_available": sum(1 for s in statuses if s["alive"] and s["logged_in"] and not s["busy"]),
        "workers_total": total,
        "queue_waiting": worker_pool.waiting_count,
        "uptime_seconds": round(time.time() - _start_time),
    }


@app.post("/api/new-chat")
async def api_new_chat():
    """手動重置所有 worker 的 Gemini 對話"""
    from .gemini import new_chat
    results = []
    for i, bm in enumerate(worker_pool._workers):
        if bm.page:
            ok = await new_chat(bm.page)
            results.append({"worker": i, "success": ok})
    return {"results": results}


# ── Google GenAI API 相容端點 ──


def _extract_api_key(request: Request, key: str | None) -> str | None:
    """從 header 或 query string 提取 API key"""
    header_key = request.headers.get("x-goog-api-key")
    if header_key:
        return header_key
    return key


def _verify_api_key(request: Request, key: str | None):
    """驗證 API 金鑰（如果有設定 API_KEYS）"""
    if not settings.api_keys:
        return
    actual_key = _extract_api_key(request, key)
    if not actual_key or actual_key not in settings.api_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")


async def _generate_content_impl(model: str, body: dict) -> dict:
    """Google GenAI API 相容端點的核心邏輯,可被 streaming / non-streaming 共用。"""

    # 前置驗證: dump 完整 body 結構 (隱藏 base64 內容避免 log 爆炸)
    # 用來確認 openclaw 多模態請求格式 — 看 inlineData 真的是 contents[].parts[]
    # 還是被包在某個 tool call 裡
    def _redact(obj, max_depth=8, depth=0):
        if depth > max_depth:
            return "<too-deep>"
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k == "data" and isinstance(v, str) and len(v) > 100:
                    out[k] = f"<base64 {len(v)} chars>"
                elif k == "text" and isinstance(v, str) and len(v) > 200:
                    out[k] = v[:200] + f"...<{len(v)-200} more>"
                else:
                    out[k] = _redact(v, max_depth, depth + 1)
            return out
        if isinstance(obj, list):
            return [_redact(x, max_depth, depth + 1) for x in obj]
        return obj

    import json as _json_dbg
    logger.info("RAW BODY KEYS: %s", list(body.keys()))
    logger.info("RAW BODY: %s", _json_dbg.dumps(_redact(body), ensure_ascii=False)[:4000])

    # 透過 adapter 把完整 request body (含 systemInstruction / tools / 多輪歷史)
    # 攤平成單段 prompt。has_function_tools 決定後續是否要嘗試解析 tool_call。
    prompt, has_function_tools, allowed_tool_names = build_prompt(body)

    # Debug: 觀察 prompt 規模,multi-turn 累積後可能超大導致 Gemini Web 卡住
    contents_count = len(body.get("contents", []) or [])
    logger.info(
        "openclaw request: prompt=%d chars, turns=%d, tools=%d, has_tool_call=%s",
        len(prompt), contents_count, len(allowed_tool_names), has_function_tools,
    )

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="No content in request")

    tools = body.get("tools", []) or []
    has_google_search = any(
        "google_search" in t or "googleSearch" in t
        for t in tools
        if isinstance(t, dict)
    )
    if has_google_search:
        prompt = f"請搜尋最新的即時資訊來回答以下問題（{time.strftime('%Y-%m-%d')}）：\n\n{prompt}"

    gen_config = body.get("generationConfig", {})
    response_mime = gen_config.get("responseMimeType", "")
    response_modalities = gen_config.get("responseModalities", []) or []
    # 大小寫不敏感比對 (openclaw 送 "IMAGE" 全大寫,Google SDK 送 "Image",
    # 文件範例又有 "image";三種都接)
    modalities_lower = {str(m).lower() for m in response_modalities}
    is_image = (
        response_mime.lower().startswith("image/")
        or "image" in modalities_lower
    )

    # 強制 JSON 回應（模擬 responseMimeType: application/json）
    # 注意: 若已注入 tool_call 指令就不再疊加,避免兩種 JSON 規範打架。
    if response_mime == "application/json" and not is_image and not has_function_tools:
        prompt = (
            "You MUST respond in valid JSON format only. "
            "No markdown, no code blocks, no extra explanation. "
            "Output raw JSON.\n\n" + prompt
        )

    kind = "generate" if is_image else "chat"
    timeout = settings.default_timeout

    try:
        result = await worker_pool.dispatch(kind, prompt, model, timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="Queue full")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Request timeout")

    if not result.get("success"):
        return {
            "error": {
                "code": 400,
                "message": result.get("message", result.get("error", "Unknown error")),
                "status": "FAILED_PRECONDITION",
            }
        }

    # JSON 回應清理（Gemini 網頁版可能加 "JSON\n" 前綴或 code block）
    if not is_image and result.get("text") and not has_function_tools:
        import re
        text = result["text"].strip()
        # 去掉 ```json ... ``` code block
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if m:
            text = m.group(1).strip()
        # 去掉 "JSON\n" 前綴
        if text.upper().startswith("JSON"):
            text = text[4:].lstrip()
        result["text"] = text

    if is_image:
        parts: list[dict] = []
        for img_data in result.get("images", []):
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
                mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
            else:
                b64 = img_data
                mime = "image/png"
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        finish_reason = "STOP"
    else:
        parts, finish_reason = build_response_parts(
            result.get("text", ""),
            has_function_tools,
            allowed_tool_names=allowed_tool_names,
        )

    return {
        "candidates": [
            {
                "content": {
                    "parts": parts,
                    "role": "model",
                },
                "finishReason": finish_reason,
            }
        ],
        "modelVersion": result.get("actual_model", model) if is_image else model,
    }


# ── 對外 endpoints (含 /v1beta 與根路徑兩種前綴) ─────────────────────


@app.post("/v1beta/models/{model}:generateContent")
@app.post("/models/{model}:generateContent")
async def genai_generate_content(model: str, request: Request, key: str = Query(default=None)):
    """Google GenAI API 相容端點 (非串流)"""
    _verify_api_key(request, key)
    body = await request.json()
    return await _generate_content_impl(model, body)


@app.post("/v1beta/models/{model}:streamGenerateContent")
@app.post("/models/{model}:streamGenerateContent")
async def genai_stream_generate_content(
    model: str,
    request: Request,
    key: str = Query(default=None),
    alt: str = Query(default="sse"),
):
    """
    Google GenAI API 相容端點 (串流)。

    Gemini Web 本身沒有 streaming,所以這裡用「假串流」:
    1. 開一個 task 在背景跑 _generate_content_impl
    2. 主迴圈每 15 秒 yield 一個 SSE comment (keep-alive),讓 client 端
       (例如 openclaw) 不會認為 connection idle 而 abort
    3. 背景 task 完成後,yield 真正的 data chunk 並結束 stream
    """
    _verify_api_key(request, key)
    body = await request.json()

    async def event_stream():
        # 1. 立刻送一個 keep-alive comment,讓 client 知道 stream 已建立
        yield ": stream-open\n\n"

        # 2. 在背景跑核心邏輯
        task = asyncio.create_task(_generate_content_impl(model, body))

        # 3. 每 15 秒一個 SSE comment 心跳;SSE 規格中以 ":" 開頭的行是 comment,
        #    client 端會忽略內容但 TCP 層收到資料就會 reset idle timer
        try:
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                break
        except asyncio.CancelledError:
            task.cancel()
            raise

        # 4. 取結果並 yield 真正的 data
        try:
            result = task.result()
        except HTTPException as e:
            err = {"error": {"code": e.status_code, "message": e.detail, "status": "FAILED_PRECONDITION"}}
            yield f"data: {json.dumps(err)}\n\n"
            return
        except Exception as e:
            err = {"error": {"code": 500, "message": str(e), "status": "INTERNAL"}}
            yield f"data: {json.dumps(err)}\n\n"
            return

        # 5. 一次吐出完整結果。Google SSE 格式: 每筆事件都是 `data: <json>\n\n`
        yield f"data: {json.dumps(result)}\n\n"

    media_type = "text/event-stream" if alt == "sse" else "application/json"
    return StreamingResponse(event_stream(), media_type=media_type)
