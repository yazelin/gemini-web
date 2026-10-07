"""影片／音樂的等待迴圈：Gemini 回了字卻一直不給媒體，不能等滿逾時。

2026-09-22 三筆音樂請求 Gemini 都回了一段說明（「Here is a minimalist
instrumental piece…」）卻沒長出專輯卡片，各空等 248 秒以上才回 timeout，
而且 timeout 會算進 worker 重啟計數。額度用完的提示多半也是「只給字不給卡片」
這個形狀，字樣還沒人看過，所以先守形狀。
"""
import asyncio

import pytest

from src.gemini import _wait_for_media_or_text


class _El:
    def __init__(self, text=""):
        self._text = text

    async def inner_text(self):
        return self._text


class _Page:
    """依序播放每次輪詢看到的畫面：(有沒有媒體, 回應文字)。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self.polls = 0

    def _now(self):
        return self._frames[min(self.polls, len(self._frames) - 1)]

    async def query_selector(self, _sel):
        has_media, _ = self._now()
        self.polls += 1
        return _El() if has_media else None

    async def query_selector_all(self, _sel):
        _, text = self._now()
        # 模擬答案後面那個空的引用晶片：最後一個元素是空的
        return [_El(text), _El("")] if text else []


async def _run(page, budget=5, stall=0.3):
    return await _wait_for_media_or_text(page, "audios", budget,
                                         stall_seconds=stall, poll_seconds=0.05)


@pytest.mark.asyncio
async def test_media_wins_immediately():
    el, said = await _run(_Page([(True, "Here is your track")]))
    assert el is not None and said == ""


@pytest.mark.asyncio
async def test_known_error_phrase_returns_at_once():
    page = _Page([(False, ""), (False, "I seem to be encountering an error")])
    el, said = await _run(page)
    assert el is None and "encountering an error" in said
    assert page.polls <= 3


@pytest.mark.asyncio
async def test_plain_text_keeps_waiting_until_media_arrives():
    """成功時 Gemini 也會先吐說明再給卡片：說明期間不能收工。"""
    frames = [(False, "Here is a minimalist instrumental piece")] * 3 + [(True, "Here is…")]
    el, said = await _run(_Page(frames))
    assert el is not None and said == ""


@pytest.mark.asyncio
async def test_plain_text_without_media_gives_up_after_stall():
    """回了字、過了 stall 秒數還沒媒體 → 帶原文收工，不等滿預算。"""
    page = _Page([(False, "你今天的音樂生成次數已達上限")])
    started = asyncio.get_event_loop().time()
    el, said = await _run(page, budget=30, stall=0.3)
    assert el is None and said == "你今天的音樂生成次數已達上限"
    assert asyncio.get_event_loop().time() - started < 3


@pytest.mark.asyncio
async def test_silence_waits_out_the_budget():
    """沒字也沒媒體就等到預算用完，兩個都空，交回呼叫端走 timeout。"""
    el, said = await _run(_Page([(False, "")]), budget=0.3)
    assert el is None and said == ""
