"""FastAPI 應用程式入口"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from .browser import browser_manager
from .config import settings
from .gemini import chat, generate_image, new_chat
from .queue import RequestQueue, QueueFullError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

request_queue = RequestQueue(max_size=settings.queue_max_size)
_start_time = time.time()


async def _handle_generate(prompt: str, timeout: int) -> dict:
    """Worker handler：操作瀏覽器生圖 → 去水印 → 重置對話"""
    page = browser_manager.page
    if not page:
        return {"success": False, "error": "browser_error", "message": "瀏覽器未啟動"}

    result = await generate_image(page, prompt, timeout)
    # 每次生圖完重置對話
    await new_chat(page)

    # 去水印（在 thread pool 中執行，避免阻塞 event loop）
    if result.get("success") and result.get("images"):
        result["images"] = await asyncio.get_event_loop().run_in_executor(
            None, _remove_watermarks, result["images"]
        )

    return result


async def _handle_chat(prompt: str, timeout: int) -> dict:
    """Worker handler：操作瀏覽器文字對話 → 重置對話"""
    page = browser_manager.page
    if not page:
        return {"success": False, "error": "browser_error", "message": "瀏覽器未啟動"}

    result = await chat(page, prompt, timeout)
    await new_chat(page)
    return result


async def _dispatch(kind: str, prompt: str, timeout: int) -> dict:
    """根據請求類型分派到對應 handler"""
    if kind == "chat":
        return await _handle_chat(prompt, timeout)
    return await _handle_generate(prompt, timeout)


def _remove_watermarks(images: list[str]) -> list[str]:
    """對 base64 圖片列表去水印"""
    import base64
    import tempfile
    from pathlib import Path
    from .watermark import remove_watermark

    cleaned = []
    for img_data in images:
        try:
            # 解析 base64
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
            else:
                header, b64 = "data:image/png;base64", img_data

            raw_bytes = base64.b64decode(b64)

            # 根據檔案魔術數字判斷實際格式（不靠 header）
            if raw_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                ext = "png"
                actual_ct = "image/png"
            elif raw_bytes[:2] == b'\xff\xd8':
                ext = "jpg"
                actual_ct = "image/jpeg"
            else:
                ext = "png"
                actual_ct = "image/png"

            # 存到暫存檔
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name

            # 去水印（覆蓋原檔）
            out_path = remove_watermark(tmp_path)

            # 讀回 base64（用實際格式）
            raw = Path(out_path).read_bytes()
            new_b64 = base64.b64encode(raw).decode("ascii")
            cleaned.append(f"data:{actual_ct};base64,{new_b64}")

            # 清理暫存
            Path(tmp_path).unlink(missing_ok=True)
            if out_path != tmp_path:
                Path(out_path).unlink(missing_ok=True)
        except Exception as e:
            logging.getLogger(__name__).warning("去水印處理失敗：%s", e)
            cleaned.append(img_data)  # 失敗就用原圖

    return cleaned


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服務生命週期：啟動瀏覽器 + worker，結束時清理"""
    await browser_manager.start()
    worker_task = asyncio.create_task(request_queue.run_worker(_dispatch))
    logger.info("服務已啟動，port %d", settings.port)
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await browser_manager.stop()


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
        result = await request_queue.submit("generate", req.prompt, timeout=req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")

    return result


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """文字對話"""
    try:
        result = await request_queue.submit("chat", req.prompt, timeout=req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")

    return result


@app.get("/api/health")
async def api_health():
    """健康檢查"""
    alive = await browser_manager.is_alive()
    logged_in = await browser_manager.is_logged_in()
    status = "ok"
    if not alive:
        status = "down"
    elif not logged_in:
        status = "degraded"

    return {
        "status": status,
        "browser_alive": alive,
        "logged_in": logged_in,
        "queue_size": request_queue.size,
        "uptime_seconds": round(time.time() - _start_time),
    }


@app.post("/api/new-chat")
async def api_new_chat():
    """手動重置 Gemini 對話"""
    page = browser_manager.page
    if not page:
        raise HTTPException(status_code=503, detail="瀏覽器未啟動")
    ok = await new_chat(page)
    return {"success": ok}


# ── Google GenAI API 相容端點 ──


def _extract_api_key(request: Request, key: str | None) -> str | None:
    """從 header 或 query string 提取 API key"""
    # 優先：x-goog-api-key header（google-genai SDK 用這個）
    header_key = request.headers.get("x-goog-api-key")
    if header_key:
        return header_key
    # 備用：query string ?key=（舊式 REST API）
    return key


def _verify_api_key(request: Request, key: str | None):
    """驗證 API 金鑰（如果有設定 API_KEYS）"""
    if not settings.api_keys:
        return  # 沒設定 = 不驗證
    actual_key = _extract_api_key(request, key)
    if not actual_key or actual_key not in settings.api_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")


@app.post("/v1beta/models/{model}:generateContent")
async def genai_generate_content(model: str, request: Request, key: str = Query(default=None)):
    """Google GenAI API 相容端點

    支援文字對話和圖片生成，格式完全相容 google-genai SDK。
    """
    _verify_api_key(request, key)

    body = await request.json()

    # 提取 prompt（從 contents[].parts[].text 中組合）
    prompt_parts = []
    contents = body.get("contents", [])
    for content in contents:
        for part in content.get("parts", []):
            if "text" in part:
                prompt_parts.append(part["text"])
    prompt = "\n".join(prompt_parts)

    if not prompt:
        raise HTTPException(status_code=400, detail="No text content in request")

    # 偵測 google_search tool → 注入搜尋觸發詞
    tools = body.get("tools", [])
    has_google_search = any(
        "google_search" in t or "googleSearch" in t
        for t in tools
        if isinstance(t, dict)
    )
    if has_google_search:
        prompt = f"請搜尋最新的即時資訊來回答以下問題（{time.strftime('%Y-%m-%d')}）：\n\n{prompt}"

    # 判斷是圖片生成還是文字對話
    gen_config = body.get("generationConfig", {})
    response_mime = gen_config.get("responseMimeType", "")
    is_image = response_mime.startswith("image/")

    kind = "generate" if is_image else "chat"
    timeout = settings.default_timeout

    try:
        result = await request_queue.submit(kind, prompt, timeout=timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="Queue full")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Request timeout")

    if not result.get("success"):
        # 回傳 Google 格式的錯誤
        return {
            "error": {
                "code": 400,
                "message": result.get("message", result.get("error", "Unknown error")),
                "status": "FAILED_PRECONDITION",
            }
        }

    # 組裝 Google 相容 response
    if is_image:
        # 圖片回應
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
        # 文字回應
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
