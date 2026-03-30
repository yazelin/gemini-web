"""請求佇列測試"""
import asyncio
import pytest
from src.queue import RequestQueue, QueueFullError


@pytest.fixture
def queue():
    return RequestQueue(max_size=2)


@pytest.mark.asyncio
async def test_submit_and_process(queue):
    """提交任務後 worker 應處理並回傳結果"""
    async def fake_handler(kind: str, prompt: str, timeout: int) -> dict:
        return {"success": True, "images": ["base64data"], "prompt": prompt}

    worker_task = asyncio.create_task(queue.run_worker(fake_handler))
    try:
        result = await asyncio.wait_for(
            queue.submit("generate", "test prompt", timeout=5),
            timeout=3,
        )
        assert result["success"] is True
        assert result["prompt"] == "test prompt"
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_queue_full(queue):
    """佇列滿時應拋出 QueueFullError"""
    for _ in range(2):
        queue._queue.put_nowait(("generate", "p", 60, asyncio.get_event_loop().create_future()))

    with pytest.raises(QueueFullError):
        await queue.submit("generate", "overflow", timeout=5)


def test_queue_size(queue):
    """應回報正確的佇列大小"""
    assert queue.size == 0
