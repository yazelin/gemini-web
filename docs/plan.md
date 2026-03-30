# Gemini Image API 實作計劃

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立獨立的圖片生成 API 服務，使用 Playwright 自動化 Gemini 網頁版生成含繁體中文的圖片。

**Architecture:** FastAPI 提供 HTTP API，asyncio.Queue 排隊請求，單一 Playwright Chromium 瀏覽器實例操作 Gemini 網頁。瀏覽器使用持久化 context 保存 Google 登入狀態，stealth 模式防偵測。

**Tech Stack:** Python 3.11+, FastAPI, Playwright, uvicorn, uv + hatchling

**Spec:** `docs/design.md`

**工作目錄：** `~/SDD/gemini-web-api/`

**測試策略：** 瀏覽器互動層無法 mock，採用可測試層（config、queue、API routing）TDD + 瀏覽器層手動整合測試。

---

### Task 1: 專案骨架

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `src/__init__.py`
- Create: `src/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: 建立 pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "gemini-web-api"
version = "0.1.0"
description = "Gemini 圖片生成 API 服務"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "playwright>=1.49",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "httpx>=0.27"]

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: 建立 .env.example**

```bash
# 瀏覽器
HEADLESS=false
PROFILE_DIR=profiles
GEMINI_URL=https://gemini.google.com/app

# Stealth
STEALTH_LANGUAGE=zh-TW,zh,en-US,en
STEALTH_TIMEZONE=Asia/Taipei

# 服務
HOST=0.0.0.0
PORT=8070
QUEUE_MAX_SIZE=10
DEFAULT_TIMEOUT=60

# 心跳
HEARTBEAT_INTERVAL=300
```

- [ ] **Step 3: 建立 .gitignore**

```
__pycache__/
*.pyc
.env
profiles/
*.egg-info/
dist/
.venv/
logs/
```

- [ ] **Step 4: 建立 src/__init__.py 和 tests/__init__.py**

兩個都是空檔案。

- [ ] **Step 5: 寫 config.py 的失敗測試**

`tests/test_config.py`:

```python
"""config 模組測試"""
import os
import pytest
from src.config import Settings


def test_default_settings():
    """預設值應正確"""
    s = Settings()
    assert s.headless is False
    assert s.port == 8070
    assert s.queue_max_size == 10
    assert s.default_timeout == 60
    assert s.heartbeat_interval == 300
    assert s.gemini_url == "https://gemini.google.com/app"
    assert s.stealth_language == "zh-TW,zh,en-US,en"
    assert s.stealth_timezone == "Asia/Taipei"


def test_settings_from_env(monkeypatch):
    """應從環境變數讀取設定"""
    monkeypatch.setenv("HEADLESS", "true")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("QUEUE_MAX_SIZE", "5")
    s = Settings()
    assert s.headless is True
    assert s.port == 9090
    assert s.queue_max_size == 5
```

- [ ] **Step 6: 執行測試，確認失敗**

Run: `cd ~/SDD/gemini-web-api && uv sync --extra dev && uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.config'`

- [ ] **Step 7: 實作 config.py**

`src/config.py`:

```python
"""環境變數設定"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(val: str | None, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


class Settings:
    """服務設定，從環境變數讀取"""

    def __init__(self) -> None:
        # 瀏覽器
        self.headless: bool = _bool(os.getenv("HEADLESS"), False)
        self.profile_dir: str = os.getenv("PROFILE_DIR", "profiles")
        self.gemini_url: str = os.getenv("GEMINI_URL", "https://gemini.google.com/app")

        # Stealth
        self.stealth_language: str = os.getenv("STEALTH_LANGUAGE", "zh-TW,zh,en-US,en")
        self.stealth_timezone: str = os.getenv("STEALTH_TIMEZONE", "Asia/Taipei")

        # 服務
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = _int(os.getenv("PORT"), 8070)
        self.queue_max_size: int = _int(os.getenv("QUEUE_MAX_SIZE"), 10)
        self.default_timeout: int = _int(os.getenv("DEFAULT_TIMEOUT"), 60)

        # 心跳
        self.heartbeat_interval: int = _int(os.getenv("HEARTBEAT_INTERVAL"), 300)


settings = Settings()
```

- [ ] **Step 8: 執行測試，確認通過**

Run: `cd ~/SDD/gemini-web-api && uv run pytest tests/test_config.py -v`
Expected: 2 passed

- [ ] **Step 9: Commit**

```bash
cd ~/SDD/gemini-web-api
git add -A
git commit -m "feat: 專案骨架 — pyproject.toml、config、測試"
```

---

### Task 2: DOM Selector 管理

**Files:**
- Create: `src/selectors.py`

- [ ] **Step 1: 建立 selectors.py**

`src/selectors.py`:

```python
"""Gemini 頁面 DOM selector 集中管理

Gemini 改版時只需更新此檔案的 selector 值。
實際值需在開發時開啟 Gemini 頁面用 DevTools 確認。
"""

SELECTORS = {
    # 輸入框 — Gemini 使用 contenteditable div 或 rich text editor
    "input": "div.ql-editor[contenteditable='true']",

    # 送出按鈕
    "send": "button.send-button, button[aria-label='Send message']",

    # 回應區域 — 最後一個回應訊息容器
    "response": "message-content",

    # 生成的圖片 — 回應區域內的 img 標籤
    "images": "message-content img",

    # 新對話按鈕
    "new_chat": "button[aria-label='New chat']",

    # 停止生成按鈕（用來偵測生成是否完成）
    "stop_generating": "button[aria-label='Stop generating']",
}
```

注意：以上 selector 為初始估計值。Task 7 的整合測試階段需對照真實 Gemini DOM 更新。

- [ ] **Step 2: Commit**

```bash
cd ~/SDD/gemini-web-api
git add src/selectors.py
git commit -m "feat: DOM selector 集中管理"
```

---

### Task 3: 瀏覽器管理

**Files:**
- Create: `src/browser.py`

- [ ] **Step 1: 實作 browser.py**

`src/browser.py`:

```python
"""Playwright 瀏覽器管理 — 啟動、stealth、session 持久化"""
import asyncio
import logging
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


class BrowserManager:
    """管理單一 Playwright 瀏覽器實例"""

    def __init__(self) -> None:
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def page(self) -> Page | None:
        return self._page

    async def start(self) -> None:
        """啟動瀏覽器，導航到 Gemini"""
        profile_path = str(Path(settings.profile_dir).resolve())
        Path(profile_path).mkdir(parents=True, exist_ok=True)

        languages = settings.stealth_language.split(",")
        stealth_js = _STEALTH_SCRIPT.replace(
            "LANGUAGES_PLACEHOLDER", str(languages)
        )

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            profile_path,
            headless=settings.headless,
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

        # 擋掉不必要的資源
        await self._page.route(
            "**/*",
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

    async def is_logged_in(self) -> bool:
        """檢查是否已登入 Google（偵測輸入框是否存在）"""
        if not self._page:
            return False
        try:
            from .selectors import SELECTORS
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
```

- [ ] **Step 2: Commit**

```bash
cd ~/SDD/gemini-web-api
git add src/browser.py
git commit -m "feat: 瀏覽器管理 — stealth、session 持久化、心跳"
```

---

### Task 4: 請求佇列

**Files:**
- Create: `src/queue.py`
- Create: `tests/test_queue.py`

- [ ] **Step 1: 寫佇列的失敗測試**

`tests/test_queue.py`:

```python
"""請求佇列測試"""
import asyncio
import pytest
from src.queue import RequestQueue, QueueFullError


@pytest.fixture
def queue():
    return RequestQueue(max_size=2)


@pytest.mark.asyncio
async def test_submit_and_process(queue):
    """提交任務後 worker 應處理並回傳結果"""
    async def fake_handler(prompt: str, timeout: int) -> dict:
        return {"success": True, "images": ["base64data"], "prompt": prompt}

    worker_task = asyncio.create_task(queue.run_worker(fake_handler))
    try:
        result = await asyncio.wait_for(
            queue.submit("test prompt", timeout=5),
            timeout=3,
        )
        assert result["success"] is True
        assert result["prompt"] == "test prompt"
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_queue_full(queue):
    """佇列滿時應拋出 QueueFullError"""
    # 塞滿佇列但不啟動 worker
    for _ in range(2):
        queue._queue.put_nowait(("p", 60, asyncio.get_event_loop().create_future()))

    with pytest.raises(QueueFullError):
        await queue.submit("overflow", timeout=5)


def test_queue_size(queue):
    """應回報正確的佇列大小"""
    assert queue.size == 0
```

- [ ] **Step 2: 執行測試，確認失敗**

Run: `cd ~/SDD/gemini-web-api && uv run pytest tests/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.queue'`

- [ ] **Step 3: 實作 queue.py**

`src/queue.py`:

```python
"""asyncio 請求佇列 — 確保一次只有一個請求操作瀏覽器"""
import asyncio
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """佇列已滿"""
    pass


class RequestQueue:
    """非同步請求佇列，單一 worker 消費"""

    def __init__(self, max_size: int = 10) -> None:
        self._queue: asyncio.Queue[tuple[str, int, asyncio.Future]] = asyncio.Queue(
            maxsize=max_size
        )
        self._max_size = max_size

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def submit(self, prompt: str, timeout: int = 60) -> dict:
        """提交生圖請求，等待結果回傳

        Raises:
            QueueFullError: 佇列已滿
            asyncio.TimeoutError: 等待超過 timeout
        """
        if self._queue.full():
            raise QueueFullError(f"佇列已滿（{self._max_size}）")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._queue.put_nowait((prompt, timeout, future))
        logger.info("請求已排隊，佇列大小：%d", self.size)

        return await asyncio.wait_for(future, timeout=timeout)

    async def run_worker(
        self, handler: Callable[[str, int], Awaitable[dict]]
    ) -> None:
        """Worker 循環：從佇列取任務 → 呼叫 handler → 設定結果"""
        logger.info("Worker 已啟動")
        while True:
            prompt, timeout, future = await self._queue.get()
            if future.cancelled():
                self._queue.task_done()
                continue
            try:
                result = await handler(prompt, timeout)
                if not future.cancelled():
                    future.set_result(result)
            except Exception as e:
                if not future.cancelled():
                    future.set_result(
                        {"success": False, "error": "browser_error", "message": str(e)}
                    )
                logger.exception("Worker 處理請求失敗")
            finally:
                self._queue.task_done()
```

- [ ] **Step 4: 執行測試，確認通過**

Run: `cd ~/SDD/gemini-web-api && uv run pytest tests/test_queue.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd ~/SDD/gemini-web-api
git add src/queue.py tests/test_queue.py
git commit -m "feat: asyncio 請求佇列 + 測試"
```

---

### Task 5: Gemini 頁面互動

**Files:**
- Create: `src/gemini.py`

- [ ] **Step 1: 實作 gemini.py**

`src/gemini.py`:

```python
"""Gemini 頁面互動 — 輸入 prompt、等待生成、擷取圖片"""
import asyncio
import logging
import time

from playwright.async_api import Page

from .selectors import SELECTORS

logger = logging.getLogger(__name__)

# 瀏覽器端 JS：將 img 元素轉為 base64
_IMG_TO_BASE64_JS = """
(img) => {
    return new Promise((resolve, reject) => {
        const canvas = document.createElement('canvas');
        const naturalImg = new Image();
        naturalImg.crossOrigin = 'anonymous';
        naturalImg.onload = () => {
            canvas.width = naturalImg.naturalWidth;
            canvas.height = naturalImg.naturalHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(naturalImg, 0, 0);
            resolve(canvas.toDataURL('image/png'));
        };
        naturalImg.onerror = () => reject('圖片載入失敗');
        naturalImg.src = img.src;
    });
}
"""

# 拒絕生圖的常見文字片段
_BLOCK_PHRASES = [
    "I can't generate",
    "I'm not able to",
    "無法生成",
    "I can't create",
    "isn't something I can",
    "against my safety",
    "violates my safety",
]


async def generate_image(page: Page, prompt: str, timeout: int = 60) -> dict:
    """在 Gemini 頁面輸入 prompt 並擷取生成的圖片

    Returns:
        {"success": True, "images": [...], "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    start = time.time()

    try:
        # 1. 確認輸入框就緒
        input_el = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=10_000
        )
        if not input_el:
            return _error("browser_error", "找不到輸入框")

        # 2. 清空並輸入 prompt
        await input_el.click()
        await input_el.fill("")
        await page.keyboard.type(prompt, delay=20)
        await asyncio.sleep(0.5)

        # 3. 送出（按 Enter）
        await page.keyboard.press("Enter")
        logger.info("已送出 prompt：%s", prompt[:50])

        # 4. 等待回應完成
        #    策略：等待「停止生成」按鈕出現後再消失
        try:
            await page.wait_for_selector(
                SELECTORS["stop_generating"], state="visible", timeout=10_000
            )
        except Exception:
            pass  # 有時生成太快，按鈕瞬間出現又消失

        await page.wait_for_selector(
            SELECTORS["stop_generating"], state="hidden", timeout=timeout * 1000
        )
        # 額外等待確保圖片渲染完成
        await asyncio.sleep(2)

        # 5. 檢查是否被拒絕
        response_els = await page.query_selector_all(SELECTORS["response"])
        if response_els:
            last_response = response_els[-1]
            text = (await last_response.inner_text()).strip()
            for phrase in _BLOCK_PHRASES:
                if phrase.lower() in text.lower():
                    elapsed = round(time.time() - start, 1)
                    return {
                        "success": False,
                        "error": "content_blocked",
                        "message": text[:200],
                        "elapsed_seconds": elapsed,
                    }

        # 6. 擷取圖片
        img_els = await page.query_selector_all(SELECTORS["images"])
        if not img_els:
            # 可能回了文字而非圖片
            text = ""
            if response_els:
                text = (await response_els[-1].inner_text()).strip()
            elapsed = round(time.time() - start, 1)
            return {
                "success": False,
                "error": "no_image",
                "message": f"Gemini 未生成圖片。回應內容：{text[:200]}",
                "elapsed_seconds": elapsed,
            }

        # 7. 將圖片轉為 base64
        images = []
        for img_el in img_els:
            try:
                base64_data = await img_el.evaluate(_IMG_TO_BASE64_JS)
                if base64_data and base64_data.startswith("data:image"):
                    images.append(base64_data)
            except Exception as e:
                logger.warning("擷取圖片失敗：%s", e)

        elapsed = round(time.time() - start, 1)

        if not images:
            return _error("browser_error", "圖片元素存在但無法擷取", elapsed)

        return {
            "success": True,
            "images": images,
            "prompt": prompt,
            "elapsed_seconds": elapsed,
        }

    except asyncio.TimeoutError:
        elapsed = round(time.time() - start, 1)
        return _error("timeout", f"生成超時（{timeout}秒）", elapsed)
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        logger.exception("Gemini 互動發生錯誤")
        return _error("browser_error", str(e), elapsed)


async def new_chat(page: Page) -> bool:
    """點擊「新對話」重置 Gemini 狀態"""
    try:
        btn = await page.query_selector(SELECTORS["new_chat"])
        if btn:
            await btn.click()
            await asyncio.sleep(1)
            logger.info("已重置對話")
            return True
        # 備用：直接導航到 Gemini 首頁
        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        logger.info("已重新導航至 Gemini 首頁")
        return True
    except Exception as e:
        logger.warning("重置對話失敗：%s", e)
        return False


def _error(error: str, message: str, elapsed: float = 0) -> dict:
    return {
        "success": False,
        "error": error,
        "message": message,
        "elapsed_seconds": elapsed,
    }
```

- [ ] **Step 2: Commit**

```bash
cd ~/SDD/gemini-web-api
git add src/gemini.py
git commit -m "feat: Gemini 頁面互動 — 輸入、等待、擷取圖片"
```

---

### Task 6: FastAPI 應用程式

**Files:**
- Create: `src/main.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: 寫 API 的失敗測試**

`tests/test_api.py`:

```python
"""API 端點測試"""
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

# mock 瀏覽器，避免測試時真的啟動 Chromium
@pytest.fixture(autouse=True)
def mock_browser():
    with patch("src.main.browser_manager") as mock:
        mock.start = AsyncMock()
        mock.stop = AsyncMock()
        mock.is_alive = AsyncMock(return_value=True)
        mock.is_logged_in = AsyncMock(return_value=True)
        mock.page = AsyncMock()
        yield mock


@pytest.fixture
def mock_queue():
    with patch("src.main.request_queue") as mock:
        mock.size = 0
        yield mock


@pytest.mark.asyncio
async def test_health_endpoint(mock_browser, mock_queue):
    """GET /api/health 應回傳狀態"""
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["browser_alive"] is True
    assert data["logged_in"] is True


@pytest.mark.asyncio
async def test_generate_missing_prompt():
    """POST /api/generate 沒有 prompt 應回 422"""
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/generate", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generate_success(mock_browser, mock_queue):
    """POST /api/generate 成功時回傳圖片"""
    mock_queue.submit = AsyncMock(return_value={
        "success": True,
        "images": ["data:image/png;base64,abc"],
        "prompt": "test",
        "elapsed_seconds": 1.0,
    })
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/generate", json={"prompt": "test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert len(data["images"]) == 1
```

- [ ] **Step 2: 執行測試，確認失敗**

Run: `cd ~/SDD/gemini-web-api && uv run pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.main'`

- [ ] **Step 3: 實作 main.py**

`src/main.py`:

```python
"""FastAPI 應用程式入口"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .browser import browser_manager
from .config import settings
from .gemini import generate_image, new_chat
from .queue import RequestQueue, QueueFullError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

request_queue = RequestQueue(max_size=settings.queue_max_size)
_start_time = time.time()


async def _handle_request(prompt: str, timeout: int) -> dict:
    """Worker handler：操作瀏覽器生圖 → 重置對話"""
    page = browser_manager.page
    if not page:
        return {"success": False, "error": "browser_error", "message": "瀏覽器未啟動"}

    result = await generate_image(page, prompt, timeout)
    # 每次生圖完重置對話
    await new_chat(page)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服務生命週期：啟動瀏覽器 + worker，結束時清理"""
    await browser_manager.start()
    worker_task = asyncio.create_task(request_queue.run_worker(_handle_request))
    logger.info("服務已啟動，port %d", settings.port)
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await browser_manager.stop()


app = FastAPI(title="Gemini Image API", lifespan=lifespan)


# ── Request / Response 模型 ──


class GenerateRequest(BaseModel):
    prompt: str
    timeout: int = settings.default_timeout


# ── 端點 ──


@app.post("/api/generate")
async def api_generate(req: GenerateRequest):
    """生成圖片"""
    try:
        result = await request_queue.submit(req.prompt, timeout=req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")

    if not result.get("success"):
        error = result.get("error", "unknown")
        status_map = {
            "content_blocked": 200,  # 正常回應，只是被拒絕
            "no_image": 200,
            "timeout": 408,
            "browser_error": 502,
            "not_logged_in": 503,
        }
        status = status_map.get(error, 500)
        if status >= 400:
            raise HTTPException(status_code=status, detail=result.get("message", ""))
    return result


@app.get("/api/health")
async def api_health():
    """健康檢查"""
    alive = await browser_manager.is_alive()
    logged_in = await browser_manager.is_logged_in()
    status = "ok"
    if not alive:
        status = "down"
    elif not logged_in:
        status = "degraded"

    return {
        "status": status,
        "browser_alive": alive,
        "logged_in": logged_in,
        "queue_size": request_queue.size,
        "uptime_seconds": round(time.time() - _start_time),
    }


@app.post("/api/new-chat")
async def api_new_chat():
    """手動重置 Gemini 對話"""
    page = browser_manager.page
    if not page:
        raise HTTPException(status_code=503, detail="瀏覽器未啟動")
    ok = await new_chat(page)
    return {"success": ok}
```

- [ ] **Step 4: 執行測試，確認通過**

Run: `cd ~/SDD/gemini-web-api && uv run pytest tests/test_api.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd ~/SDD/gemini-web-api
git add src/main.py tests/test_api.py
git commit -m "feat: FastAPI 端點 — generate、health、new-chat + 測試"
```

---

### Task 7: DOM Selector 校準（手動整合測試）

**Files:**
- Modify: `src/selectors.py`

此步驟需要實際開啟瀏覽器對照 Gemini DOM。

- [ ] **Step 1: 以 headed 模式啟動服務**

```bash
cd ~/SDD/gemini-web-api
cp .env.example .env
# 確認 .env 中 HEADLESS=false
uv run playwright install chromium
uv run uvicorn src.main:app --port 8070
```

- [ ] **Step 2: 在彈出的瀏覽器中手動登入 Google**

登入 Google 帳號，確認進入 `gemini.google.com/app` 頁面。

- [ ] **Step 3: 用 DevTools 校準 selector**

在瀏覽器中按 F12 開 DevTools，逐一確認並更新 `src/selectors.py` 中的每個 selector：

1. 找到輸入框元素 → 更新 `SELECTORS["input"]`
2. 找到送出按鈕 → 更新 `SELECTORS["send"]`
3. 手動輸入 prompt 讓 Gemini 生圖，觀察回應 DOM 結構：
   - 回應容器 → 更新 `SELECTORS["response"]`
   - 生成的圖片 `<img>` → 更新 `SELECTORS["images"]`
   - 停止生成按鈕 → 更新 `SELECTORS["stop_generating"]`
4. 找到新對話按鈕 → 更新 `SELECTORS["new_chat"]`

- [ ] **Step 4: 用 curl 測試 API**

```bash
# 健康檢查
curl http://localhost:8070/api/health

# 生成圖片
curl -X POST http://localhost:8070/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "畫一張台北101的海報，標題寫「歡迎來到台北」"}'
```

確認回傳 `{"success": true, "images": [...]}` 且圖片 base64 有效。

- [ ] **Step 5: 驗證圖片內容**

將回傳的 base64 存成檔案確認：

```bash
# 用 python 快速驗證
python3 -c "
import json, base64, sys
data = json.load(sys.stdin)
if data.get('success') and data.get('images'):
    b64 = data['images'][0].split(',', 1)[1]
    with open('/tmp/test-image.png', 'wb') as f:
        f.write(base64.b64decode(b64))
    print('已存檔：/tmp/test-image.png')
else:
    print('失敗：', data)
" < /tmp/api-response.json
```

- [ ] **Step 6: Commit 校準後的 selector**

```bash
cd ~/SDD/gemini-web-api
git add src/selectors.py
git commit -m "fix: 校準 Gemini DOM selector"
```

---

### Task 8: 部署腳本與文件

**Files:**
- Create: `scripts/install-service.sh`
- Create: `README.md`

- [ ] **Step 1: 建立 systemd 安裝腳本**

`scripts/install-service.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="gemini-web-api"
WORK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER="$(whoami)"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Gemini Image API
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${WORK_DIR}
ExecStart=${WORK_DIR}/.venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8070
Restart=on-failure
RestartSec=10
Environment=HEADLESS=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}
echo "✓ ${SERVICE_NAME} 服務已安裝並啟動"
```

- [ ] **Step 2: 建立 README.md**

`README.md`:

```markdown
# Gemini Image API

使用 Playwright 自動化 Gemini 網頁版生成含繁體中文文字的圖片，提供 HTTP API 供內部系統呼叫。

## 快速開始

### 1. 安裝

```bash
uv sync --extra dev
uv run playwright install chromium
cp .env.example .env
```

### 2. 首次啟動（手動登入 Google）

```bash
# HEADLESS=false 會開啟瀏覽器視窗
HEADLESS=false uv run uvicorn src.main:app --port 8070
```

在彈出的瀏覽器中登入 Google 帳號，確認進入 Gemini 頁面。

### 3. 正式運行

修改 `.env` 中 `HEADLESS=true`，然後：

```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8070
```

### 4. 安裝為 systemd 服務（可選）

```bash
sudo bash scripts/install-service.sh
```

## API

### POST /api/generate

生成圖片。

```bash
curl -X POST http://localhost:8070/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "畫一張台北101海報，標題寫「歡迎來到台北」"}'
```

回傳：

```json
{
  "success": true,
  "images": ["data:image/png;base64,..."],
  "prompt": "...",
  "elapsed_seconds": 12.3
}
```

### GET /api/health

健康檢查。

### POST /api/new-chat

手動重置 Gemini 對話。

## 環境變數

見 `.env.example`。

## 已知限制

- 一次只能處理一個生圖請求（其他排隊）
- Google 登入過期需手動重新登入
- Gemini 改版可能導致 DOM selector 失效，需手動更新 `src/selectors.py`
- 違反 Google 服務條款，帳號有被封風險
```

- [ ] **Step 3: Commit**

```bash
cd ~/SDD/gemini-web-api
chmod +x scripts/install-service.sh
git add scripts/install-service.sh README.md
git commit -m "docs: README + systemd 部署腳本"
```

---

### Task 9: 全部測試通過確認

- [ ] **Step 1: 執行全部測試**

```bash
cd ~/SDD/gemini-web-api && uv run pytest -v
```

Expected: 5 passed（test_config x2, test_queue x3, test_api x3 = 8 tests）

- [ ] **Step 2: 最終 commit**

```bash
cd ~/SDD/gemini-web-api
git log --oneline
```

預期 commit 歷史：
1. `feat: 專案骨架 — pyproject.toml、config、測試`
2. `feat: DOM selector 集中管理`
3. `feat: 瀏覽器管理 — stealth、session 持久化、心跳`
4. `feat: asyncio 請求佇列 + 測試`
5. `feat: Gemini 頁面互動 — 輸入、等待、擷取圖片`
6. `feat: FastAPI 端點 — generate、health、new-chat + 測試`
7. `fix: 校準 Gemini DOM selector`
8. `docs: README + systemd 部署腳本`
