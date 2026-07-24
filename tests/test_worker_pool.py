"""WorkerPool 測試"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


def _bare_pool(n: int, mode: str = "round-robin") -> "WorkerPool":
    """建一個不啟動真瀏覽器的 pool,idle 池填 0..n-1。"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    pool._count = n
    pool._workers = [MagicMock(page=MagicMock()) for _ in range(n)]
    pool._idle_ids = set(range(n))
    pool._free = asyncio.Event()
    pool._free.set()
    pool._max_waiting = 10
    pool._waiting = 0
    pool._mode = mode
    pool._next = 0
    return pool


# ── _select：分配策略(純同步)──


def test_select_round_robin_spreads():
    """round-robin:每次挑完立即還回 → 序列應輪流 0,1,0,1。"""
    pool = _bare_pool(2, "round-robin")
    seen = []
    for _ in range(4):
        wid = pool._select()
        seen.append(wid)
        pool._release(wid)
    assert seen == [0, 1, 0, 1]


def test_select_spillover_prefers_zero():
    """spillover:worker 0 空閒就永遠回 0。"""
    pool = _bare_pool(2, "spillover")
    seen = []
    for _ in range(3):
        wid = pool._select()
        seen.append(wid)
        pool._release(wid)
    assert seen == [0, 0, 0]


def test_select_removes_from_idle():
    """_select 會把挑中的 worker 從 idle 池取走。"""
    pool = _bare_pool(2)
    wid = pool._select()
    assert wid not in pool._idle_ids
    assert len(pool._idle_ids) == 1


# ── dispatch：正常派工 ──


@pytest.mark.asyncio
async def test_dispatch_uses_available_worker():
    pool = _bare_pool(2)
    called = []

    async def fake_run(worker_id, kind, prompt, model, timeout, extra=None):
        called.append(worker_id)
        return {"success": True}

    pool._run = fake_run
    result = await asyncio.wait_for(pool.dispatch("chat", "hi", "", 10), timeout=3)
    assert result["success"] is True
    assert len(called) == 1
    # 用完的 worker 要還回池子
    assert pool._idle_ids == {0, 1}


@pytest.mark.asyncio
async def test_parallel_dispatch_uses_both_workers():
    """兩個併發請求應各自落在不同 worker。"""
    pool = _bare_pool(2)
    used = []
    ev = [asyncio.Event(), asyncio.Event()]

    async def slow_run(worker_id, kind, prompt, model, timeout, extra=None):
        used.append(worker_id)
        ev[worker_id].set()
        await asyncio.wait_for(ev[1 - worker_id].wait(), timeout=2)
        return {"success": True}

    pool._run = slow_run
    results = await asyncio.wait_for(
        asyncio.gather(pool.dispatch("chat", "a", "", 5), pool.dispatch("chat", "b", "", 5)),
        timeout=5,
    )
    assert all(r["success"] for r in results)
    assert set(used) == {0, 1}
    assert pool._idle_ids == {0, 1}


# ── 回歸測試:逾時 / 出錯不可卡死 worker ──


@pytest.mark.asyncio
async def test_timeout_returns_worker_to_pool():
    """一筆請求逾時被 cancel 後,那個 worker 必須還回池子,
    否則下一筆會永遠排隊(這正是把 pool 卡死的 bug)。"""
    pool = _bare_pool(1)

    async def hang(worker_id, kind, prompt, model, timeout, extra=None):
        await asyncio.sleep(10)  # 永遠不回 → 觸發外層 wait_for 逾時

    pool._run = hang
    with pytest.raises(asyncio.TimeoutError):
        await pool.dispatch("chat", "hi", "", timeout=0.1)

    # 關鍵斷言:worker 沒被卡住
    assert pool._idle_ids == {0}
    assert pool._waiting == 0

    # 逾時後還能正常派下一筆
    async def ok(worker_id, kind, prompt, model, timeout, extra=None):
        return {"success": True}

    pool._run = ok
    result = await asyncio.wait_for(pool.dispatch("chat", "hi2", "", 5), timeout=3)
    assert result["success"] is True


@pytest.mark.asyncio
async def test_error_returns_worker_to_pool():
    """_run 拋錯時 worker 一樣要還回池子。"""
    pool = _bare_pool(1)

    async def boom(worker_id, kind, prompt, model, timeout, extra=None):
        raise RuntimeError("worker exploded")

    pool._run = boom
    with pytest.raises(RuntimeError):
        await pool.dispatch("chat", "hi", "", 5)
    assert pool._idle_ids == {0}


@pytest.mark.asyncio
async def test_all_busy_then_freed_serves_waiter():
    """全忙時後來的請求要能等到 worker 空出來再跑,不能自己卡住。"""
    pool = _bare_pool(1)
    gate = asyncio.Event()
    order = []

    async def run(worker_id, kind, prompt, model, timeout, extra=None):
        order.append(prompt)
        if prompt == "first":
            await gate.wait()  # 佔住唯一的 worker
        return {"success": True, "p": prompt}

    pool._run = run
    t1 = asyncio.create_task(pool.dispatch("chat", "first", "", 5))
    await asyncio.sleep(0.05)          # 確保 first 先搶到 worker
    t2 = asyncio.create_task(pool.dispatch("chat", "second", "", 5))
    await asyncio.sleep(0.05)
    assert not t2.done()               # second 還在等(worker 被佔)
    gate.set()                         # 放掉 first
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=3)
    assert order == ["first", "second"]
    assert pool._idle_ids == {0}


@pytest.mark.asyncio
async def test_queue_full_error():
    from src.worker_pool import WorkerPool, QueueFullError

    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = []
    pool._max_waiting = 0
    pool._waiting = 0
    with pytest.raises(QueueFullError):
        await pool.dispatch("chat", "hello", "", 10)
