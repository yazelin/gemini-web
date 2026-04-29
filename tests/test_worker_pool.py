"""WorkerPool 測試"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_browser_managers():
    """Create mock BrowserManagers that don't launch real browsers"""
    managers = []
    for i in range(2):
        bm = MagicMock()
        bm.start = AsyncMock()
        bm.stop = AsyncMock()
        bm.is_alive = AsyncMock(return_value=True)
        bm.is_logged_in = AsyncMock(return_value=True)
        bm.page = MagicMock()
        managers.append(bm)
    return managers


@pytest.mark.asyncio
async def test_dispatch_uses_available_worker(mock_browser_managers):
    """Should dispatch to an available worker"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = mock_browser_managers
    pool._locks = [asyncio.Lock() for _ in mock_browser_managers]
    pool._max_waiting = 10
    pool._waiting = 0

    call_log = []

    async def fake_run(worker_id, kind, prompt, model, timeout, extra=None):
        call_log.append(kind)
        return {"success": True, "text": "ok"}

    pool._run = fake_run

    result = await asyncio.wait_for(
        pool.dispatch("chat", "hello", "", 10),
        timeout=3,
    )
    assert result["success"] is True
    assert len(call_log) == 1


@pytest.mark.asyncio
async def test_parallel_dispatch():
    """Two requests should run on two different workers concurrently"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    workers_used = []
    worker_events = [asyncio.Event(), asyncio.Event()]

    bm0 = MagicMock()
    bm0.page = MagicMock()
    bm1 = MagicMock()
    bm1.page = MagicMock()
    pool._workers = [bm0, bm1]
    pool._locks = [asyncio.Lock(), asyncio.Lock()]
    pool._max_waiting = 10
    pool._waiting = 0

    async def slow_run(worker_id, kind, prompt, model, timeout, extra=None):
        workers_used.append(worker_id)
        worker_events[worker_id].set()
        await asyncio.wait_for(worker_events[1 - worker_id].wait(), timeout=2)
        return {"success": True, "text": "ok"}

    pool._run = slow_run

    results = await asyncio.wait_for(
        asyncio.gather(
            pool.dispatch("chat", "req1", "", 5),
            pool.dispatch("chat", "req2", "", 5),
        ),
        timeout=5,
    )

    assert len(results) == 2
    assert all(r["success"] for r in results)
    assert set(workers_used) == {0, 1}


@pytest.mark.asyncio
async def test_queue_full_error():
    """Should raise QueueFullError when too many waiting"""
    from src.worker_pool import WorkerPool, QueueFullError

    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = []
    pool._locks = []
    pool._max_waiting = 0
    pool._waiting = 0

    with pytest.raises(QueueFullError):
        await pool.dispatch("chat", "hello", "", 10)
