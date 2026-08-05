"""攔截範圍必須夠窄 —— 這是 2026-08-05 那次 OOM 的真因。

原本 `page.route("**/*", ...)` 只為了擋掉 font/stylesheet,卻讓每一個網路請求
都在 Python 這側建出 Request + Route + Response 三個物件。Request 的
initializer 帶著整包 postData,而 Gemini 每次對話的 XHR 都馱著整段對話。
這些物件不隨導航回收(driver 要累積到每型別 10,000 個才丟 10%,4 個 worker
各有獨立連線),兩天就把 Python RSS 從 78MB 養到 1.06GB,8/03 整個服務被
oom-kill。

受控實驗(400 次 XHR、每次 20KB postData):
    "**/*"        → 1203 個物件殘留、RSS +17.1MB
    只攔 font/css →    0 個物件殘留、RSS  +0.0MB

所以 pattern 收窄是這個 bug 的修法本體,不是可有可無的最佳化。
"""
import pytest

from src.browser import _ASSET_URL_RE


@pytest.mark.parametrize("url", [
    "https://fonts.gstatic.com/s/googlesans/v58/foo.woff2",
    "https://www.gstatic.com/_/mss/boq-bard-web/_/ss/k=x.css",
    "https://example.com/theme.css?v=3",
    "https://example.com/icons.ttf",
    "https://example.com/legacy.eot",
    "https://example.com/a.woff",
    "https://example.com/UPPER.CSS",
])
def test_intercepts_font_and_stylesheet_urls(url):
    assert _ASSET_URL_RE.search(url), f"該攔的沒攔到：{url}"


@pytest.mark.parametrize("url", [
    # 這幾種是量最大、postData 最肥的 —— 一旦被攔就是 08-05 那個洩漏
    "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
    "https://gemini.google.com/app",
    "https://gemini.google.com/_/BardChatUi/browserinfo",
    "https://play.google.com/log?format=json",
    "https://lh3.googleusercontent.com/image.png",
    "https://www.gstatic.com/_/boq-bard-web/_/js/k=boq.js",
    # 檔名裡出現 css/woff 字樣但不是該類資源,不能誤攔
    "https://example.com/api/csscolors",
    "https://example.com/data.json?ref=style.css.map",
])
def test_leaves_everything_else_alone(url):
    assert not _ASSET_URL_RE.search(url), f"攔太寬了，會洩漏：{url}"


def test_pattern_is_not_a_catch_all():
    """守住底線：只要有人改回 '**/*' 或等效的全攔，這裡就要紅。"""
    assert not _ASSET_URL_RE.search("https://gemini.google.com/anything")
