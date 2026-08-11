"""generateContent 帶圖時必須真的把圖送進去。

原本 `build_prompt` 會把 inlineData 換成字串 "[inline_data:image/png]"，圖的
位元組在那一步就沒了，於是「送圖 + 要圖」變成「純文字 prompt 生圖」，而且是
HTTP 200 不報錯 —— ai-brain-site 的格莉奇日記整季的插畫都沒吃到角色設定圖，
從外面完全看不出來。

修法是在 dispatch 前把圖撈出來改走 edit（image-to-image），這裡釘住撈圖那步。
"""
import pytest

from src.main import _first_inline_image

PNG = "iVBORw0KGgoAAAANSUhEUg=="


def test_picks_up_inline_image():
    body = {"contents": [{"parts": [
        {"text": "畫一隻貓"},
        {"inlineData": {"mimeType": "image/png", "data": PNG}},
    ]}]}
    assert _first_inline_image(body) == PNG


def test_accepts_snake_case_spelling():
    """手刻 JSON 的呼叫端常送 inline_data，不能只認 SDK 的駝峰寫法。"""
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "image/jpeg", "data": PNG}},
    ]}]}
    assert _first_inline_image(body) == PNG


def test_takes_the_latest_turn():
    """多輪對話要用最後一張，不是第一張。"""
    body = {"contents": [
        {"parts": [{"inlineData": {"mimeType": "image/png", "data": "OLD"}}]},
        {"parts": [{"inlineData": {"mimeType": "image/png", "data": "NEW"}}]},
    ]}
    assert _first_inline_image(body) == "NEW"


@pytest.mark.parametrize("body", [
    {},
    {"contents": []},
    {"contents": [{"parts": [{"text": "純文字"}]}]},
    # 非圖片的 inlineData（例如 PDF）不能被當成參考圖送去 edit
    {"contents": [{"parts": [{"inlineData": {"mimeType": "application/pdf",
                                             "data": PNG}}]}]},
])
def test_returns_none_when_no_image(body):
    assert _first_inline_image(body) is None
