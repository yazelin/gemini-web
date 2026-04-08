"""Worker Pool — 管理多個 BrowserManager 實例並行處理請求"""
import asyncio
import base64
import logging
import tempfile
from pathlib import Path
from typing import Any

from .browser import BrowserManager
from .config import settings, get_worker_profile_dir
from .gemini import chat, generate_image, new_chat, switch_model

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """等待佇列已滿"""
    pass


class WorkerPool:
    """管理 N 個 BrowserManager，空閒優先分配請求"""

    def __init__(self, worker_count: int | None = None, max_waiting: int = 10) -> None:
        self._count = worker_count or settings.worker_count
        self._max_waiting = max_waiting
        self._workers: list[BrowserManager] = []
        self._locks: list[asyncio.Lock] = []
        # 每個 worker 的「待完成 reset」task。下次請求進來時必須先 await
        # 這個 task,確保上一次的對話已經乾淨重置才開始新請求。
        # 但 _run 不會 block 在 reset 上 — 它會提前 return result,讓 client
        # (例如 openclaw) 在 60 秒 timeout 內收到回應。
        self._pending_resets: list[asyncio.Task | None] = []
        self._waiting = 0

    async def start(self) -> None:
        """啟動所有 worker 的瀏覽器"""
        for i in range(self._count):
            profile_dir = get_worker_profile_dir(i)
            bm = BrowserManager(profile_dir=profile_dir)
            await bm.start()
            self._workers.append(bm)
            self._locks.append(asyncio.Lock())
            self._pending_resets.append(None)
            logger.info("Worker %d 已啟動（profile: %s）", i, profile_dir)

    async def stop(self) -> None:
        """關閉所有 worker"""
        for i, bm in enumerate(self._workers):
            await bm.stop()
            logger.info("Worker %d 已關閉", i)

    async def dispatch(self, kind: str, prompt: str, model: str, timeout: int) -> dict:
        """分配請求到空閒 worker，全忙則等待

        Raises:
            QueueFullError: 等待數超過上限
            asyncio.TimeoutError: 等待超過 timeout
        """
        if self._waiting >= self._max_waiting:
            raise QueueFullError(f"等待佇列已滿（{self._max_waiting}）")

        self._waiting += 1
        try:
            return await asyncio.wait_for(
                self._acquire_and_run(kind, prompt, model, timeout),
                timeout=timeout,
            )
        finally:
            self._waiting -= 1

    async def _acquire_and_run(self, kind: str, prompt: str, model: str, timeout: int) -> dict:
        """嘗試取得任意空閒 worker 的 lock，取得後執行請求"""
        while True:
            # Try to grab any unlocked worker
            for i, lock in enumerate(self._locks):
                if not lock.locked():
                    async with lock:
                        return await self._run(i, kind, prompt, model, timeout)

            # All busy — create a task per lock and wait for the first one
            acquire_tasks = {
                asyncio.create_task(lock.acquire()): i
                for i, lock in enumerate(self._locks)
            }
            try:
                done, pending = await asyncio.wait(
                    acquire_tasks.keys(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel the rest
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Use the first completed worker
                for task in done:
                    worker_id = acquire_tasks[task]
                    try:
                        return await self._run(worker_id, kind, prompt, model, timeout)
                    finally:
                        self._locks[worker_id].release()
            except Exception:
                for task in acquire_tasks:
                    task.cancel()
                raise

    async def _run(self, worker_id: int, kind: str, prompt: str, model: str, timeout: int) -> dict:
        """在指定 worker 上執行請求"""
        bm = self._workers[worker_id]
        page = bm.page
        if not page:
            return {"success": False, "error": "browser_error", "message": f"Worker {worker_id} 瀏覽器未啟動"}

        logger.info("Worker %d 處理請求：%s", worker_id, kind)

        # 上一次的 reset 還沒做完?先等它完成,保證頁面狀態乾淨
        prev_reset = self._pending_resets[worker_id]
        if prev_reset is not None and not prev_reset.done():
            try:
                await prev_reset
            except Exception as e:
                logger.warning("Worker %d 上次 reset 失敗: %s", worker_id, e)
        self._pending_resets[worker_id] = None

        if model:
            await switch_model(page, model)

        if kind == "chat":
            result = await chat(page, prompt, timeout)
        else:
            result = await generate_image(page, prompt, timeout)
            if result.get("success") and result.get("images"):
                result["images"] = await asyncio.get_event_loop().run_in_executor(
                    None, _remove_watermarks, result["images"]
                )

        # Fire-and-forget reset:return result 後在背景重置對話頁面。
        # 下次 _run 進來時會 await 這個 task,確保乾淨狀態。
        # 對 image gen 特別重要 — openclaw 對 image gen 有 60 秒硬編碼 timeout。
        self._pending_resets[worker_id] = asyncio.create_task(new_chat(page))
        return result

    async def worker_status(self) -> list[dict]:
        """回傳每個 worker 的狀態"""
        statuses = []
        for i, bm in enumerate(self._workers):
            alive = await bm.is_alive()
            logged_in = await bm.is_logged_in() if alive else False
            statuses.append({
                "id": i,
                "alive": alive,
                "logged_in": logged_in,
                "busy": self._locks[i].locked(),
            })
        return statuses

    @property
    def waiting_count(self) -> int:
        return self._waiting

    @property
    def worker_count(self) -> int:
        return self._count


def _remove_watermarks(images: list[str]) -> list[str]:
    """對 base64 圖片列表去水印（從 main.py 搬過來）"""
    from .watermark import remove_watermark

    cleaned = []
    for img_data in images:
        try:
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
            else:
                header, b64 = "data:image/png;base64", img_data

            raw_bytes = base64.b64decode(b64)

            if raw_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                ext, actual_ct = "png", "image/png"
            elif raw_bytes[:2] == b'\xff\xd8':
                ext, actual_ct = "jpg", "image/jpeg"
            else:
                ext, actual_ct = "png", "image/png"

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name

            out_path = remove_watermark(tmp_path)
            raw = Path(out_path).read_bytes()
            new_b64 = base64.b64encode(raw).decode("ascii")
            cleaned.append(f"data:{actual_ct};base64,{new_b64}")

            Path(tmp_path).unlink(missing_ok=True)
            if out_path != tmp_path:
                Path(out_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("去水印處理失敗：%s", e)
            cleaned.append(img_data)

    return cleaned
