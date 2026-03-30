"""asyncio 請求佇列 — 確保一次只有一個請求操作瀏覽器"""
import asyncio
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """佇列已滿"""
    pass


class RequestQueue:
    """非同步請求佇列，單一 worker 消費"""

    def __init__(self, max_size: int = 10) -> None:
        self._queue: asyncio.Queue[tuple[str, str, int, asyncio.Future]] = asyncio.Queue(
            maxsize=max_size
        )
        self._max_size = max_size

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def submit(self, kind: str, prompt: str, timeout: int = 60) -> dict:
        """提交請求，等待結果回傳

        Args:
            kind: 請求類型（"generate" 或 "chat"）
            prompt: 提示文字
            timeout: 超時秒數

        Raises:
            QueueFullError: 佇列已滿
            asyncio.TimeoutError: 等待超過 timeout
        """
        if self._queue.full():
            raise QueueFullError(f"佇列已滿（{self._max_size}）")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._queue.put_nowait((kind, prompt, timeout, future))
        logger.info("請求已排隊（%s），佇列大小：%d", kind, self.size)

        return await asyncio.wait_for(future, timeout=timeout)

    async def run_worker(
        self, handler: Callable[[str, str, int], Awaitable[dict]]
    ) -> None:
        """Worker 循環：從佇列取任務 → 呼叫 handler → 設定結果"""
        logger.info("Worker 已啟動")
        while True:
            kind, prompt, timeout, future = await self._queue.get()
            if future.cancelled():
                self._queue.task_done()
                continue
            try:
                result = await handler(kind, prompt, timeout)
                if not future.cancelled():
                    future.set_result(result)
            except Exception as e:
                if not future.cancelled():
                    future.set_result(
                        {"success": False, "error": "browser_error", "message": str(e)}
                    )
                logger.exception("Worker 處理請求失敗")
            finally:
                self._queue.task_done()
