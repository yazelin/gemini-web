"""FastAPI 應用程式入口"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from .config import settings
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


@app.post("/v1beta/models/{model}:generateContent")
async def genai_generate_content(model: str, request: Request, key: str = Query(default=None)):
    """Google GenAI API 相容端點"""
    _verify_api_key(request, key)

    body = await request.json()

    prompt_parts = []
    contents = body.get("contents", [])
    for content in contents:
        for part in content.get("parts", []):
            if "text" in part:
                prompt_parts.append(part["text"])
    prompt = "\n".join(prompt_parts)

    if not prompt:
        raise HTTPException(status_code=400, detail="No text content in request")

    tools = body.get("tools", [])
    has_google_search = any(
        "google_search" in t or "googleSearch" in t
        for t in tools
        if isinstance(t, dict)
    )
    if has_google_search:
        prompt = f"請搜尋最新的即時資訊來回答以下問題（{time.strftime('%Y-%m-%d')}）：\n\n{prompt}"

    gen_config = body.get("generationConfig", {})
    response_mime = gen_config.get("responseMimeType", "")
    response_modalities = gen_config.get("responseModalities", [])
    is_image = (
        response_mime.startswith("image/")
        or "Image" in response_modalities
        or "image" in response_modalities
    )

    # 強制 JSON 回應（模擬 responseMimeType: application/json）
    if response_mime == "application/json" and not is_image:
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
    if not is_image and result.get("text"):
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
        parts = []
        for img_data in result.get("images", []):
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
                mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
            else:
                b64 = img_data
                mime = "image/png"
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    else:
        parts = [{"text": result.get("text", "")}]

    return {
        "candidates": [
            {
                "content": {
                    "parts": parts,
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "modelVersion": model,
    }
