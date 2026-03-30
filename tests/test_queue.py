"""佇列滿載測試（WorkerPool 版）"""
import asyncio
import pytest
from src.worker_pool import WorkerPool, QueueFullError


@pytest.mark.asyncio
async def test_queue_full_rejects():
    """等待數超過上限時應拋出 QueueFullError"""
    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = []
    pool._locks = []
    pool._max_waiting = 0
    pool._waiting = 0

    with pytest.raises(QueueFullError):
        await pool.dispatch("chat", "hello", "", 10)
