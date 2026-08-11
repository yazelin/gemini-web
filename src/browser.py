"""Playwright 瀏覽器管理 — 啟動、stealth、session 持久化"""
import asyncio
import logging
import re
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

from .config import settings

logger = logging.getLogger(__name__)

# Stealth 注入腳本 — 參考 Project Golem BrowserLauncher
_STEALTH_SCRIPT = """
() => {
    // 隱藏 webdriver 標記
    Object.defineProperty(navigator, 'webdriver', { get: () => false });

    // 偽裝 languages
    Object.defineProperty(navigator, 'languages', {
        get: () => LANGUAGES_PLACEHOLDER,
    });

    // 偽裝 platform
    Object.defineProperty(navigator, 'platform', {
        get: () => 'Linux x86_64',
    });

    // 偽裝 plugins（空陣列會被偵測）
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // 偽裝 WebGL vendor/renderer
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, param);
    };
}
"""


# 只攔字體/樣式表的網址 —— 絕對不要改回 "**/*"。
#
# 用 "**/*" 攔截時,每一個網路請求都會在 Python 這側建出 Request + Route +
# Response 三個物件,而 Request 的 initializer 帶著整包 postData ——
# Gemini 每次對話的 XHR 都馱著整段對話(log 裡看得到 13KB、29KB 的 body)。
# 這些物件不會因為導航而回收:playwright driver 要累積到「每型別 10,000 個」
# 才丟掉最舊的 10%,而 4 個 worker 各有獨立連線,等於上限 ×4。
#
# 2026-08-05 實測後果:service 跑兩天 Python RSS 從 78MB 長到 1.06GB,
# 8/03 整個服務被 oom-kill。受控實驗(400 次 XHR,每次 20KB postData):
#   "**/*"        → 留下 1203 個物件、RSS +17.1MB(每請求約 42KB,一個都沒回收)
#   只攔 font/css → 留下 0 個物件、RSS +0.0MB(與完全不攔相同)
_ASSET_URL_RE = re.compile(r"\.(css|woff2?|ttf|otf|eot)(\?|$)", re.I)


class BrowserManager:
    """管理單一 Playwright 瀏覽器實例"""

    def __init__(self, headless: bool | None = None, profile_dir: str | None = None) -> None:
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._headless_override = headless  # None = 用 settings
        self._profile_dir_override = profile_dir

    @property
    def page(self) -> Page | None:
        return self._page

    async def start(self) -> None:
        """啟動瀏覽器，導航到 Gemini"""
        profile_path = str(Path(self._profile_dir_override or settings.profile_dir).resolve())
        Path(profile_path).mkdir(parents=True, exist_ok=True)

        languages = settings.stealth_language.split(",")
        stealth_js = _STEALTH_SCRIPT.replace(
            "LANGUAGES_PLACEHOLDER", str(languages)
        )

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            profile_path,
            # channel="chromium" = 完整 chromium 跑新版 headless；不給 channel 的話
            # Playwright 會用 chromium_headless_shell（精簡殼），2026-08-07 起 Gemini
            # 網頁版對它一律不出圖 —— 送得出 prompt、也秒回，但回的是
            # 「I seem to be encountering an error」之類的通用錯誤，chat 卻完全正常。
            # 實測（同帳號同 profile、prompt 都是 a simple red circle on white）：
            #   headless_shell → 三種路徑（生成圖片頁 / 重試 / chat 加前綴）全部 0 張
            #   channel=chromium → 15 秒出圖
            # 假 UA 蓋不蓋沒差（連 HeadlessChrome/145 的真 UA 也照樣出圖），
            # 所以判準是瀏覽器本體不是 UA 字串。
            channel="chromium",
            headless=self._headless_override if self._headless_override is not None else settings.headless,
            locale=languages[0] if languages else "zh-TW",
            timezone_id=settings.stealth_timezone,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        # 注入 stealth 腳本
        await self._context.add_init_script(stealth_js)

        # 取得或建立頁面
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        # 擋掉不必要的資源（只攔 font/css 的網址，理由見 _ASSET_URL_RE）
        await self._page.route(
            _ASSET_URL_RE,
            lambda route: (
                route.abort()
                if route.request.resource_type in ("font", "stylesheet")
                and "gemini" not in route.request.url
                else route.continue_()
            ),
        )

        # 導航到 Gemini
        await self._page.goto(settings.gemini_url, wait_until="domcontentloaded")
        logger.info("瀏覽器已啟動，導航至 %s", settings.gemini_url)

        # 啟動心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """關閉瀏覽器"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("瀏覽器已關閉")

    async def is_alive(self) -> bool:
        """檢查瀏覽器頁面是否還活著"""
        if not self._page:
            return False
        try:
            await self._page.evaluate("() => document.title")
            return True
        except Exception:
            return False

    async def is_logged_in(self, wait: bool = False) -> bool:
        """檢查是否已登入 Google（偵測輸入框是否存在）

        Args:
            wait: 是否等待頁面載入完成（首次檢查用）
        """
        if not self._page:
            return False
        try:
            from .selectors import SELECTORS
            if wait:
                # Gemini 是 Angular SPA，需要等 JS 渲染完成
                el = await self._page.wait_for_selector(
                    SELECTORS["input"], state="visible", timeout=15_000
                )
                return el is not None
            else:
                el = await self._page.query_selector(SELECTORS["input"])
                return el is not None
        except Exception:
            return False

    async def _heartbeat_loop(self) -> None:
        """定時心跳檢查"""
        while True:
            await asyncio.sleep(settings.heartbeat_interval)
            alive = await self.is_alive()
            if not alive:
                logger.warning("心跳檢查失敗：瀏覽器頁面無回應")
            else:
                logged_in = await self.is_logged_in()
                if not logged_in:
                    logger.warning("心跳檢查：Google 登入狀態可能已過期")


# 全域單例
browser_manager = BrowserManager()
