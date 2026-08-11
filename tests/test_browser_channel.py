"""瀏覽器本體必須是完整 chromium，不能退回 headless_shell。

2026-08-07 起 Gemini 網頁版對 chromium_headless_shell 一律不出圖：prompt 送得
出去、也秒回，但回的是「I seem to be encountering an error」這類通用錯誤，而純
文字 chat 完全正常，所以症狀是「出圖全滅、聊天沒事」。程式端沒看出來是因為
generate_image 等的是圖片元素出現，於是每筆都空等滿逾時（實測 400.005s）。

同帳號同 profile、prompt 都是 "a simple red circle on white background"：
    headless_shell   → 生成圖片頁 / 重試 / chat 加前綴，三條路全部 0 張
    channel=chromium → 15 秒出圖

Playwright 的 headless=True 不給 channel 就會挑 headless_shell，所以這個
kwarg 是修法本體。有人拿掉的話這裡要紅。
"""
import inspect

from src import browser


def _launch_kwargs_source() -> str:
    src = inspect.getsource(browser.BrowserManager.start)
    start = src.index("launch_persistent_context(")
    return src[start:]


def test_launches_full_chromium_not_headless_shell():
    assert 'channel="chromium"' in _launch_kwargs_source(), (
        "launch_persistent_context 少了 channel=\"chromium\"，"
        "會退回 headless_shell —— Gemini 對它不出圖"
    )
