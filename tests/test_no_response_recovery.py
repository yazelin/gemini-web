"""「送得出去但沒回應」的偵測與恢復(連續 no_response → 重啟瀏覽器)

2026-08-05 四個 worker 從 09:25 起 100% 失敗到被人發現為止,錯誤全是
`Gemini 未回應`、耗時一律卡滿逾時。真因是瀏覽器 session 放太久(service 連續
跑 2 天)爛掉:頁面殼還在——輸入框和工具鈕都在,prompt 也貼得進去送得出去——
但 Gemini 再也不吐 model-response。

`_ensure_page_ready` 只看「輸入框/工具鈕在不在」,兩者都在所以永遠判定就緒,
三段式自癒完全不會啟動,四個 worker 就一路卡到底。這裡測補上的盲點:
連續 N 次 no_response 就重啟該 worker 的瀏覽器,並在重啟前存證留下現場。
"""
from unittest.mock import MagicMock

import pytest


def _pool(restart_ok=True):
    """建一個單 worker 的假 pool,回傳 (pool, bm, calls)。"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    pool._count = 1
    pool._idle_ids = {0}
    pool._pending_resets = [None]
    pool._no_response_streak = [0]
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
    return pool, bm, calls


@pytest.fixture
def patched(monkeypatch):
    import src.worker_pool as wp

    state = {"calls": None}

    async def fake_new_chat(page):
        state["calls"]["reset"] += 1
        return True

    async def fake_dump(page, tag, worker_id=None):
        state["calls"]["dump"].append((tag, worker_id))
        return "dump.png"

    monkeypatch.setattr(wp, "new_chat", fake_new_chat)
    monkeypatch.setattr(wp, "dump_page_state", fake_dump)
    return state


NO_RESPONSE = {"success": False, "error": "no_response", "message": "Gemini 未回應"}
OK = {"success": True, "text": "hi"}


def test_success_keeps_streak_at_zero():
    pool, _, _ = _pool()
    assert pool._note_no_response(0, OK) == 0
    assert pool._note_no_response(0, OK) == 0


def test_no_response_accumulates():
    pool, _, _ = _pool()
    assert pool._note_no_response(0, NO_RESPONSE) == 1
    assert pool._note_no_response(0, NO_RESPONSE) == 2


def test_success_resets_streak():
    """中間成功過就不算「連續」——偶發的一次逾時不該害 worker 被重啟。"""
    pool, _, _ = _pool()
    pool._note_no_response(0, NO_RESPONSE)
    assert pool._note_no_response(0, OK) == 0
    assert pool._note_no_response(0, NO_RESPONSE) == 1


def test_other_errors_do_not_count():
    """只有 no_response 算數;內容被擋、輸入不合法之類不代表 session 壞了。"""
    pool, _, _ = _pool()
    pool._note_no_response(0, NO_RESPONSE)
    assert pool._note_no_response(0, {"success": False, "error": "content_blocked"}) == 0


@pytest.mark.asyncio
async def test_first_failure_only_resets_conversation(patched):
    """第一次 no_response:先存證 + 重置對話,還不到重啟瀏覽器的程度。"""
    pool, bm, calls = _pool()
    patched["calls"] = calls

    await pool._recover_from_no_response(0, 1)

    assert calls["dump"] == [("no-response", 0)], "現場只有這時候看得到,一定要存"
    assert calls["reset"] == 1
    assert calls["start"] == 0, "第一次就重啟太躁進"


@pytest.mark.asyncio
async def test_second_consecutive_failure_restarts_browser(patched):
    """連續第 2 次 no_response → 重啟瀏覽器,這才是 08-05 卡死唯一的出路。"""
    pool, bm, calls = _pool()
    patched["calls"] = calls

    await pool._recover_from_no_response(0, 2)

    assert calls["dump"] == [("no-response", 0)]
    assert calls["stop"] == 1 and calls["start"] == 1
    assert bm.page == "page-v2"
    assert pool._no_response_streak[0] == 0, "重啟後要歸零,否則下一筆立刻又重啟"


@pytest.mark.asyncio
async def test_restart_failure_does_not_raise(patched):
    """重啟失敗也不能丟例外炸掉背景 task。"""
    pool, bm, calls = _pool(restart_ok=False)
    patched["calls"] = calls

    await pool._recover_from_no_response(0, 2)   # 不應 raise

    assert calls["start"] == 1
    assert pool._no_response_streak[0] == 0
