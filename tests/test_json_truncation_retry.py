"""JSON 回應被截斷時要重跑一次(而不是把半截陣列當成功回去)

2026-09-18:line-sticker-studio 的「✨ 生成 9 句」三次有兩次失敗在
`no phrases`。gemini-web 這邊每一次都是 HTTP 200、status succeeded,只是
response_text 停在 `..."action":"striking a pose"},` —— 陣列沒有收尾。

真因在 gemini.chat:網頁版沒有「生成結束」事件,它用「文字連續不變 + stop
按鈕不在」推完成,而長 JSON 在串流中會凍結在開頭片段,推論就提早成立。

呼叫端送了 responseMimeType: application/json 的時候,我們有一個比按鈕可靠
的完成訊號:解得開。這裡測「解不開就重跑一次、重跑成功就回完整的那份」。
"""
import asyncio

import pytest

from src import main as m


TRUNCATED = '[\n{"phrase":"我乃關雲長！","action":"striking a heroic pose"},'
COMPLETE = '[{"phrase":"我乃關雲長！","action":"striking a heroic pose"}]'


def _run(model, body, dispatch, monkeypatch):
    calls = []

    async def fake_dispatch(kind, prompt, model_, timeout, **kw):
        calls.append(prompt)
        return dispatch(len(calls))

    monkeypatch.setattr(m, "_dispatch_and_log", fake_dispatch)
    out = asyncio.run(m._generate_content_impl(model, body))
    return out, calls


def _body(mime="application/json"):
    return {
        "contents": [{"role": "user", "parts": [{"text": "給我 JSON"}]}],
        "generationConfig": {"responseMimeType": mime},
    }


def _text(out):
    return out["candidates"][0]["content"]["parts"][0]["text"]


def test_truncated_json_is_retried_once(monkeypatch):
    out, calls = _run(
        "gemini-2.5-flash", _body(),
        lambda n: {"success": True, "text": TRUNCATED if n == 1 else COMPLETE},
        monkeypatch,
    )
    assert len(calls) == 2
    assert _text(out) == COMPLETE


def test_complete_json_does_not_retry(monkeypatch):
    out, calls = _run(
        "gemini-2.5-flash", _body(),
        lambda n: {"success": True, "text": COMPLETE},
        monkeypatch,
    )
    assert len(calls) == 1
    assert _text(out) == COMPLETE


def test_second_truncation_is_returned_as_is(monkeypatch):
    """重跑也壞就照原樣回去 —— 不要無限盧,也不要假裝成功以外的東西。"""
    out, calls = _run(
        "gemini-2.5-flash", _body(),
        lambda n: {"success": True, "text": TRUNCATED},
        monkeypatch,
    )
    assert len(calls) == 2
    assert _text(out) == TRUNCATED


def test_plain_text_request_never_retried(monkeypatch):
    """沒點名要 JSON 的一般對話,回什麼都算數,不能因為解不開就多跑一次。"""
    out, calls = _run(
        "gemini-2.5-flash", _body(mime=""),
        lambda n: {"success": True, "text": "今天天氣不錯"},
        monkeypatch,
    )
    assert len(calls) == 1
    assert _text(out) == "今天天氣不錯"


def test_code_block_wrapper_still_stripped(monkeypatch):
    out, calls = _run(
        "gemini-2.5-flash", _body(),
        lambda n: {"success": True, "text": "```json\n" + COMPLETE + "\n```"},
        monkeypatch,
    )
    assert len(calls) == 1
    assert _text(out) == COMPLETE
