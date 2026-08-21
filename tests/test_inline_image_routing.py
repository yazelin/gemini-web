"""generateContent 帶圖時必須真的把圖送進去,而且一張都不能少。

原本 `build_prompt` 會把 inlineData 換成字串 "[inline_data:image/png]"，圖的
位元組在那一步就沒了，於是「送圖 + 要圖」變成「純文字 prompt 生圖」，而且是
HTTP 200 不報錯 —— ai-brain-site 的格莉奇日記整季的插畫都沒吃到角色設定圖，
從外面完全看不出來。

修法是在 dispatch 前把圖撈出來改走 edit（image-to-image），這裡釘住撈圖那步。

2026-08-21 起撈的是**全部**參考圖而不是一張:呼叫端(ai-comic-starter 的漫畫
產線)一頁要送畫風錨、對話框圖表、角色、場景一整疊,而且 prompt 裡是照
`image 1 / image 2 / ...` 的順序指名的。只取一張的話,模型收到的圖跟 prompt
講的對不起來,角色就漂了。實測 Gemini 網頁版一次收得下多張、也認得順序
(丟三張純色形狀圖進去,把送出順序跟檔名字母序故意錯開,它照送出順序答對)。
"""
import pytest

from src.main import _inline_images

PNG = "iVBORw0KGgoAAAANSUhEUg=="


def test_picks_up_inline_image():
    body = {"contents": [{"parts": [
        {"text": "畫一隻貓"},
        {"inlineData": {"mimeType": "image/png", "data": PNG}},
    ]}]}
    assert _inline_images(body) == [(PNG, "image/png")]


def test_accepts_snake_case_spelling():
    """手刻 JSON 的呼叫端常送 inline_data，不能只認 SDK 的駝峰寫法。"""
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "image/jpeg", "data": PNG}},
    ]}]}
    assert _inline_images(body) == [(PNG, "image/jpeg")]


def test_keeps_every_image_in_order():
    """一則訊息裡的多張圖要全部收,而且照 parts 的順序——prompt 是照
    「image 1 是畫風、image 2 是角色」指名的,順序錯了模型就對不上是誰。"""
    body = {"contents": [{"parts": [
        {"text": "REFERENCE IMAGES: image 1 畫風, image 2 角色, image 3 場景"},
        {"inlineData": {"mimeType": "image/webp", "data": "STYLE"}},
        {"inlineData": {"mimeType": "image/webp", "data": "CHAR"}},
        {"inlineData": {"mimeType": "image/webp", "data": "SCENE"}},
    ]}]}
    assert [d for d, _ in _inline_images(body)] == ["STYLE", "CHAR", "SCENE"]


def test_takes_the_latest_turn():
    """多輪對話要用最後一則的圖，不是第一則的。"""
    body = {"contents": [
        {"parts": [{"inlineData": {"mimeType": "image/png", "data": "OLD"}}]},
        {"parts": [{"inlineData": {"mimeType": "image/png", "data": "NEW1"}},
                   {"inlineData": {"mimeType": "image/png", "data": "NEW2"}}]},
    ]}
    assert [d for d, _ in _inline_images(body)] == ["NEW1", "NEW2"]


def test_skips_non_image_parts_between_images():
    """夾在圖中間的文字或 PDF 不能佔掉一個位置,也不能中斷後面的圖。"""
    body = {"contents": [{"parts": [
        {"inlineData": {"mimeType": "image/png", "data": "A"}},
        {"text": "中間插一段字"},
        {"inlineData": {"mimeType": "application/pdf", "data": "PDF"}},
        {"inlineData": {"mimeType": "image/png", "data": "B"}},
    ]}]}
    assert [d for d, _ in _inline_images(body)] == ["A", "B"]


@pytest.mark.parametrize("body", [
    {},
    {"contents": []},
    {"contents": [{"parts": [{"text": "純文字"}]}]},
    # 非圖片的 inlineData（例如 PDF）不能被當成參考圖送去 edit
    {"contents": [{"parts": [{"inlineData": {"mimeType": "application/pdf",
                                             "data": PNG}}]}]},
])
def test_returns_empty_when_no_image(body):
    assert _inline_images(body) == []
