"""worker 卡死的偵測與恢復(_ensure_page_ready)

2026-07-30 worker 3 卡在舊對話頁:輸入框還在、composer 的工具鈕不見,所以
`logged_in` 一路回報 True,而每筆生圖都失敗、沒人知道。這裡測三段式恢復:
就緒就走 → 不就緒先重置對話 → 還救不回來就重啟該 worker 的瀏覽器。
"""
import asyncio
from unittest.mock import MagicMock

import pytest


def _pool(ready_sequence, restart_ok=True):
    """建一個單 worker 的假 pool。ready_sequence 決定每次 page_ready 的回答。"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    pool._count = 1
    pool._idle_ids = {0}
    pool._pending_resets = [None]
    pool._mode = "round-robin"
    pool._next = 0

    calls = {"reset": 0, "stop": 0, "start": 0, "dump": []}

    bm = MagicMock()
    bm.page = "page-v1"

    async def stop():
        calls["stop"] += 1
        bm.page = None

    async def start():
        calls["start"] += 1
        if not restart_ok:
            raise RuntimeError("chrome 起不來")
        bm.page = "page-v2"

    bm.stop = stop
    bm.start = start
    pool._workers = [bm]
    return pool, bm, calls, list(ready_sequence)


@pytest.fixture
def patched(monkeypatch):
    import src.worker_pool as wp

    state = {"ready_seq": [], "calls": None}

    async def fake_page_ready(page):
        if page is None:
            return {"ready": False, "missing": ["page_error"]}
        nxt = state["ready_seq"].pop(0) if state["ready_seq"] else True
        return {"ready": nxt, "missing": [] if nxt else ["tools_button"]}

    async def fake_new_chat(page):
        state["calls"]["reset"] += 1
        return True

    async def fake_dump(page, tag, worker_id=None):
        state["calls"]["dump"].append((tag, worker_id))
        return "dump.png"

    monkeypatch.setattr(wp, "page_ready", fake_page_ready)
    monkeypatch.setattr(wp, "new_chat", fake_new_chat)
    monkeypatch.setattr(wp, "dump_page_state", fake_dump)
    return state


@pytest.mark.asyncio
async def test_ready_page_untouched(patched):
    """頁面本來就就緒:不重置、不重啟、不存證。"""
    pool, bm, calls, seq = _pool([True])
    patched["ready_seq"], patched["calls"] = seq, calls

    page = await pool._ensure_page_ready(0)
    assert page == "page-v1"
    assert calls["reset"] == 0 and calls["start"] == 0
    assert calls["dump"] == []


@pytest.mark.asyncio
async def test_reset_recovers_stuck_page(patched):
    """卡住 → 重置對話就救回來,不必動到瀏覽器;而且要留下現場存證。"""
    pool, bm, calls, seq = _pool([False, True])
    patched["ready_seq"], patched["calls"] = seq, calls

    page = await pool._ensure_page_ready(0)
    assert page == "page-v1"
    assert calls["reset"] == 1
    assert calls["start"] == 0, "重置就夠了,不該重啟瀏覽器"
    assert calls["dump"] == [("not-ready", 0)], "重置會沖掉現場,必須先存證"


@pytest.mark.asyncio
async def test_restarts_browser_when_reset_fails(patched):
    """重置也救不回來 → 重啟該 worker 的瀏覽器,並回傳新的 page。"""
    pool, bm, calls, seq = _pool([False, False, True])
    patched["ready_seq"], patched["calls"] = seq, calls

    page = await pool._ensure_page_ready(0)
    assert calls["reset"] == 1
    assert calls["stop"] == 1 and calls["start"] == 1
    assert page == "page-v2", "要回傳重啟後的新 page,不能繼續用死掉的舊 page"


@pytest.mark.asyncio
async def test_restart_failure_does_not_raise(patched):
    """連重啟都失敗也不能丟例外炸掉請求(頂多這筆失敗,不要拖垮服務)。"""
    pool, bm, calls, seq = _pool([False, False], restart_ok=False)
    patched["ready_seq"], patched["calls"] = seq, calls

    page = await pool._ensure_page_ready(0)   # 不應 raise
    assert calls["start"] == 1
    assert page is None or page == bm.page
