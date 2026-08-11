"""Gemini 回通用錯誤時要立刻收工，不能空等滿逾時。

2026-08 那次出圖全滅，Gemini 其實 3 秒就回了「I seem to be encountering an
error」，但程式等的是 generated-image 元素，於是每筆都卡滿 400 秒才放棄，
而且那句錯誤原文從沒被記進 DB —— 從外面看就只是「服務卡死」。
"""
import asyncio

import pytest

from src.gemini import _wait_for_image_or_error


class _El:
    def __init__(self, text=""):
        self._text = text

    async def inner_text(self):
        return self._text


class _Page:
    """依序播放每次輪詢看到的畫面：(有沒有圖, 回應文字)。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self.polls = 0

    def _now(self):
        return self._frames[min(self.polls, len(self._frames) - 1)]

    async def query_selector(self, _sel):
        has_img, _ = self._now()
        self.polls += 1
        return _El() if has_img else None

    async def query_selector_all(self, _sel):
        _, text = self._now()
        return [_El(text)] if text else []


@pytest.mark.asyncio
async def test_returns_error_text_as_soon_as_gemini_errors():
    page = _Page([(False, ""), (False, "I seem to be encountering an error")])
    err = await _wait_for_image_or_error(page, 60_000)
    assert err == "I seem to be encountering an error"


@pytest.mark.asyncio
async def test_image_wins_over_error_text():
    """圖出來了就是成功，不要因為旁邊有錯誤字樣就判失敗。"""
    page = _Page([(True, "Something went wrong")])
    assert await _wait_for_image_or_error(page, 60_000) is None


@pytest.mark.asyncio
async def test_plain_text_reply_is_not_treated_as_error():
    """出圖時 Gemini 也可能先吐一段說明，那不是錯誤，要繼續等。"""
    page = _Page([(False, "Sure, here's the illustration you asked for")])
    assert await _wait_for_image_or_error(page, 300) is None


@pytest.mark.asyncio
async def test_gives_up_at_deadline_without_error():
    """沒圖也沒錯誤字樣就等到逾時，交回呼叫端照原本邏輯處理。"""
    page = _Page([(False, "")])
    started = asyncio.get_event_loop().time()
    assert await _wait_for_image_or_error(page, 300) is None
    assert asyncio.get_event_loop().time() - started < 5
