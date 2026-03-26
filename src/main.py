"""FastAPI 應用程式入口"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .browser import browser_manager
from .config import settings
from .gemini import generate_image, new_chat
from .queue import RequestQueue, QueueFullError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

request_queue = RequestQueue(max_size=settings.queue_max_size)
_start_time = time.time()


async def _handle_request(prompt: str, timeout: int) -> dict:
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

            ext = "jpg" if "jpeg" in header or "jpg" in header else "png"

            # 存到暫存檔
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(base64.b64decode(b64))
                tmp_path = tmp.name

            # 去水印（覆蓋原檔）
            out_path = remove_watermark(tmp_path)

            # 讀回 base64
            raw = Path(out_path).read_bytes()
            new_b64 = base64.b64encode(raw).decode("ascii")
            cleaned.append(f"{header},{new_b64}")

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
    worker_task = asyncio.create_task(request_queue.run_worker(_handle_request))
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


# ── 端點 ──


@app.post("/api/generate")
async def api_generate(req: GenerateRequest):
    """生成圖片"""
    try:
        result = await request_queue.submit(req.prompt, timeout=req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")

    # 統一回傳 JSON 格式（不丟 HTTPException），方便呼叫端統一處理
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
