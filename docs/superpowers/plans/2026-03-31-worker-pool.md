# Worker Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable gemini-web to run multiple Chromium instances in parallel so requests don't queue behind each other.

**Architecture:** Replace the single global `browser_manager` with a `WorkerPool` that manages N `BrowserManager` instances. Each worker has its own Chromium, profile directory, and `asyncio.Lock`. Requests are dispatched to the first available worker; overflow waits in an `asyncio.Semaphore`-gated queue.

**Tech Stack:** Python 3.11+, asyncio, Playwright, FastAPI

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/config.py` | Modify | Add `worker_count` setting |
| `src/worker_pool.py` | Create | `WorkerPool` class — manages N workers, dispatch logic |
| `src/browser.py` | Modify | `BrowserManager.__init__` accepts `profile_dir` param |
| `src/queue.py` | Delete | Replaced by WorkerPool dispatch (semaphore-based) |
| `src/main.py` | Modify | Use WorkerPool instead of browser_manager + request_queue |
| `src/cli.py` | Modify | `login --worker N` support |
| `tests/test_config.py` | Modify | Test `worker_count` setting |
| `tests/test_worker_pool.py` | Create | Test WorkerPool dispatch logic |
| `tests/test_queue.py` | Modify | Update for new architecture |
| `tests/test_api.py` | Modify | Update mocks for WorkerPool |

---

### Task 1: Add `worker_count` to config

**Files:**
- Modify: `src/config.py:30-57`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing test for worker_count default**

Add to `tests/test_config.py`:

```python
def test_default_worker_count(monkeypatch):
    """worker_count 預設為 1"""
    monkeypatch.delenv("WORKER_COUNT", raising=False)
    s = Settings()
    assert s.worker_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_config.py::test_default_worker_count -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'worker_count'`

- [ ] **Step 3: Write failing test for worker_count from env**

Add to `tests/test_config.py`:

```python
def test_worker_count_from_env(monkeypatch):
    """應從 WORKER_COUNT 環境變數讀取"""
    monkeypatch.setenv("WORKER_COUNT", "3")
    s = Settings()
    assert s.worker_count == 3
```

- [ ] **Step 4: Implement worker_count in Settings**

In `src/config.py`, add inside `Settings.__init__` after `self.heartbeat_interval`:

```python
        # Worker pool
        self.worker_count: int = _int(os.getenv("WORKER_COUNT"), 1)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_config.py -v`
Expected: ALL PASS

- [ ] **Step 6: Add helper to get profile dir for a worker**

Add to `src/config.py` as a standalone function after the `Settings` class:

```python
def get_worker_profile_dir(worker_id: int) -> str:
    """Return the profile directory path for a given worker ID.

    worker 0 uses the base 'profiles/' dir (backward compatible).
    worker N (N>=1) uses 'profiles-N/'.
    """
    base = Path(settings.profile_dir)
    if worker_id == 0:
        return str(base)
    return str(base.parent / f"profiles-{worker_id}")
```

- [ ] **Step 7: Write test for get_worker_profile_dir**

Add to `tests/test_config.py`:

```python
from src.config import get_worker_profile_dir

def test_worker_profile_dir_zero(monkeypatch):
    """worker 0 should use base profiles/ dir"""
    monkeypatch.delenv("PROFILE_DIR", raising=False)
    path = get_worker_profile_dir(0)
    assert path.endswith("profiles")
    assert "-" not in path.split("/")[-1]

def test_worker_profile_dir_nonzero(monkeypatch):
    """worker N should use profiles-N/ dir"""
    monkeypatch.delenv("PROFILE_DIR", raising=False)
    path = get_worker_profile_dir(1)
    assert path.endswith("profiles-1")
    path2 = get_worker_profile_dir(2)
    assert path2.endswith("profiles-2")
```

- [ ] **Step 8: Run all config tests**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_config.py -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: add worker_count config and profile dir helper"
```

---

### Task 2: Make BrowserManager accept custom profile_dir

**Files:**
- Modify: `src/browser.py:44-68`

- [ ] **Step 1: Add profile_dir parameter to BrowserManager.__init__**

Modify `src/browser.py` `BrowserManager.__init__`:

```python
class BrowserManager:
    """管理單一 Playwright 瀏覽器實例"""

    def __init__(self, headless: bool | None = None, profile_dir: str | None = None) -> None:
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._headless_override = headless
        self._profile_dir_override = profile_dir
```

- [ ] **Step 2: Use profile_dir override in start()**

In `src/browser.py` `start()` method, change line 60-61 from:

```python
        profile_path = str(Path(settings.profile_dir).resolve())
```

to:

```python
        profile_path = str(Path(self._profile_dir_override or settings.profile_dir).resolve())
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest -v`
Expected: ALL PASS (existing behavior unchanged when profile_dir=None)

- [ ] **Step 4: Commit**

```bash
git add src/browser.py
git commit -m "feat: BrowserManager accepts custom profile_dir"
```

---

### Task 3: Create WorkerPool

**Files:**
- Create: `src/worker_pool.py`
- Create: `tests/test_worker_pool.py`

- [ ] **Step 1: Write failing test for WorkerPool basic dispatch**

Create `tests/test_worker_pool.py`:

```python
"""WorkerPool 測試"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_browser_managers():
    """Create mock BrowserManagers that don't launch real browsers"""
    managers = []
    for i in range(2):
        bm = MagicMock()
        bm.start = AsyncMock()
        bm.stop = AsyncMock()
        bm.is_alive = AsyncMock(return_value=True)
        bm.is_logged_in = AsyncMock(return_value=True)
        bm.page = MagicMock()
        managers.append(bm)
    return managers


@pytest.mark.asyncio
async def test_dispatch_uses_available_worker(mock_browser_managers):
    """Should dispatch to an available worker"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = mock_browser_managers
    pool._locks = [asyncio.Lock() for _ in mock_browser_managers]
    pool._max_waiting = 10

    call_log = []

    async def fake_handler(page, kind, prompt, model, timeout):
        call_log.append(kind)
        return {"success": True, "text": "ok"}

    pool._handler = fake_handler

    result = await asyncio.wait_for(
        pool.dispatch("chat", "hello", "", 10),
        timeout=3,
    )
    assert result["success"] is True
    assert len(call_log) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_worker_pool.py::test_dispatch_uses_available_worker -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.worker_pool'`

- [ ] **Step 3: Implement WorkerPool**

Create `src/worker_pool.py`:

```python
"""Worker Pool — 管理多個 BrowserManager 實例並行處理請求"""
import asyncio
import logging
import time
from typing import Any

from .browser import BrowserManager
from .config import settings, get_worker_profile_dir
from .gemini import chat, generate_image, new_chat, switch_model

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """等待佇列已滿"""
    pass


class WorkerPool:
    """管理 N 個 BrowserManager，空閒優先分配請求"""

    def __init__(self, worker_count: int | None = None, max_waiting: int = 10) -> None:
        self._count = worker_count or settings.worker_count
        self._max_waiting = max_waiting
        self._workers: list[BrowserManager] = []
        self._locks: list[asyncio.Lock] = []
        self._waiting = 0

    async def start(self) -> None:
        """啟動所有 worker 的瀏覽器"""
        for i in range(self._count):
            profile_dir = get_worker_profile_dir(i)
            bm = BrowserManager(profile_dir=profile_dir)
            await bm.start()
            self._workers.append(bm)
            self._locks.append(asyncio.Lock())
            logger.info("Worker %d 已啟動（profile: %s）", i, profile_dir)

    async def stop(self) -> None:
        """關閉所有 worker"""
        for i, bm in enumerate(self._workers):
            await bm.stop()
            logger.info("Worker %d 已關閉", i)

    async def dispatch(self, kind: str, prompt: str, model: str, timeout: int) -> dict:
        """分配請求到空閒 worker，全忙則等待

        Raises:
            QueueFullError: 等待數超過上限
            asyncio.TimeoutError: 等待超過 timeout
        """
        if self._waiting >= self._max_waiting:
            raise QueueFullError(f"等待佇列已滿（{self._max_waiting}）")

        self._waiting += 1
        try:
            return await asyncio.wait_for(
                self._acquire_and_run(kind, prompt, model, timeout),
                timeout=timeout,
            )
        finally:
            self._waiting -= 1

    async def _acquire_and_run(self, kind: str, prompt: str, model: str, timeout: int) -> dict:
        """嘗試取得任意空閒 worker 的 lock，取得後執行請求"""
        # Use an asyncio.Event per worker to get notified when one frees up
        while True:
            # Try to grab any unlocked worker
            for i, lock in enumerate(self._locks):
                if not lock.locked():
                    async with lock:
                        return await self._run(i, kind, prompt, model, timeout)

            # All busy — create a task per lock and wait for the first one
            acquire_tasks = {
                asyncio.create_task(lock.acquire()): i
                for i, lock in enumerate(self._locks)
            }
            try:
                done, pending = await asyncio.wait(
                    acquire_tasks.keys(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel the rest
                for task in pending:
                    task.cancel()
                    # Release locks that were acquired by cancelled tasks
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Use the first completed worker
                for task in done:
                    worker_id = acquire_tasks[task]
                    try:
                        return await self._run(worker_id, kind, prompt, model, timeout)
                    finally:
                        self._locks[worker_id].release()
            except Exception:
                # Clean up on error
                for task in acquire_tasks:
                    task.cancel()
                raise

    async def _run(self, worker_id: int, kind: str, prompt: str, model: str, timeout: int) -> dict:
        """在指定 worker 上執行請求"""
        bm = self._workers[worker_id]
        page = bm.page
        if not page:
            return {"success": False, "error": "browser_error", "message": f"Worker {worker_id} 瀏覽器未啟動"}

        logger.info("Worker %d 處理請求：%s", worker_id, kind)

        if model:
            await switch_model(page, model)

        if kind == "chat":
            result = await chat(page, prompt, timeout)
        else:
            result = await generate_image(page, prompt, timeout)
            # 去水印
            if result.get("success") and result.get("images"):
                result["images"] = await asyncio.get_event_loop().run_in_executor(
                    None, _remove_watermarks, result["images"]
                )

        await new_chat(page)
        return result

    async def worker_status(self) -> list[dict]:
        """回傳每個 worker 的狀態"""
        statuses = []
        for i, bm in enumerate(self._workers):
            alive = await bm.is_alive()
            logged_in = await bm.is_logged_in() if alive else False
            statuses.append({
                "id": i,
                "alive": alive,
                "logged_in": logged_in,
                "busy": self._locks[i].locked(),
            })
        return statuses

    @property
    def waiting_count(self) -> int:
        return self._waiting

    @property
    def worker_count(self) -> int:
        return self._count


def _remove_watermarks(images: list[str]) -> list[str]:
    """對 base64 圖片列表去水印（從 main.py 搬過來）"""
    import base64
    import tempfile
    from pathlib import Path
    from .watermark import remove_watermark

    cleaned = []
    for img_data in images:
        try:
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
            else:
                header, b64 = "data:image/png;base64", img_data

            raw_bytes = base64.b64decode(b64)

            if raw_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                ext, actual_ct = "png", "image/png"
            elif raw_bytes[:2] == b'\xff\xd8':
                ext, actual_ct = "jpg", "image/jpeg"
            else:
                ext, actual_ct = "png", "image/png"

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name

            out_path = remove_watermark(tmp_path)
            raw = Path(out_path).read_bytes()
            new_b64 = base64.b64encode(raw).decode("ascii")
            cleaned.append(f"data:{actual_ct};base64,{new_b64}")

            Path(tmp_path).unlink(missing_ok=True)
            if out_path != tmp_path:
                Path(out_path).unlink(missing_ok=True)
        except Exception as e:
            logging.getLogger(__name__).warning("去水印處理失敗：%s", e)
            cleaned.append(img_data)

    return cleaned
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_worker_pool.py::test_dispatch_uses_available_worker -v`
Expected: PASS

- [ ] **Step 5: Write test for parallel dispatch**

Add to `tests/test_worker_pool.py`:

```python
@pytest.mark.asyncio
async def test_parallel_dispatch():
    """Two requests should run on two different workers concurrently"""
    from src.worker_pool import WorkerPool

    pool = WorkerPool.__new__(WorkerPool)
    workers_used = []
    worker_events = [asyncio.Event(), asyncio.Event()]

    bm0 = MagicMock()
    bm0.page = MagicMock()
    bm1 = MagicMock()
    bm1.page = MagicMock()
    pool._workers = [bm0, bm1]
    pool._locks = [asyncio.Lock(), asyncio.Lock()]
    pool._max_waiting = 10
    pool._waiting = 0

    original_run = None

    async def slow_run(worker_id, kind, prompt, model, timeout):
        workers_used.append(worker_id)
        worker_events[worker_id].set()
        # Wait for both workers to be active
        await asyncio.wait_for(worker_events[1 - worker_id].wait(), timeout=2)
        return {"success": True, "text": "ok"}

    pool._run = slow_run

    results = await asyncio.wait_for(
        asyncio.gather(
            pool.dispatch("chat", "req1", "", 5),
            pool.dispatch("chat", "req2", "", 5),
        ),
        timeout=5,
    )

    assert len(results) == 2
    assert all(r["success"] for r in results)
    # Both workers should have been used
    assert set(workers_used) == {0, 1}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_worker_pool.py::test_parallel_dispatch -v`
Expected: PASS

- [ ] **Step 7: Write test for QueueFullError**

Add to `tests/test_worker_pool.py`:

```python
@pytest.mark.asyncio
async def test_queue_full_error():
    """Should raise QueueFullError when too many waiting"""
    from src.worker_pool import WorkerPool, QueueFullError

    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = []
    pool._locks = []
    pool._max_waiting = 0
    pool._waiting = 0

    with pytest.raises(QueueFullError):
        await pool.dispatch("chat", "hello", "", 10)
```

- [ ] **Step 8: Run all worker pool tests**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest tests/test_worker_pool.py -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/worker_pool.py tests/test_worker_pool.py
git commit -m "feat: add WorkerPool for multi-browser parallel dispatch"
```

---

### Task 4: Rewire main.py to use WorkerPool

**Files:**
- Modify: `src/main.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Replace browser_manager and request_queue with worker_pool in main.py**

Rewrite `src/main.py` to:

```python
"""FastAPI 應用程式入口"""
import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from .config import settings
from .worker_pool import WorkerPool, QueueFullError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

worker_pool = WorkerPool(
    worker_count=settings.worker_count,
    max_waiting=settings.queue_max_size,
)
_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服務生命週期：啟動 worker pool，結束時清理"""
    await worker_pool.start()
    logger.info("服務已啟動，%d 個 worker，port %d", settings.worker_count, settings.port)
    yield
    await worker_pool.stop()


app = FastAPI(title="Gemini Image API", lifespan=lifespan)


# ── Request / Response 模型 ──


class GenerateRequest(BaseModel):
    prompt: str
    timeout: int = settings.default_timeout


class ChatRequest(BaseModel):
    prompt: str
    timeout: int = 120


# ── 端點 ──


@app.post("/api/generate")
async def api_generate(req: GenerateRequest):
    """生成圖片"""
    try:
        result = await worker_pool.dispatch("generate", req.prompt, "", req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    return result


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """文字對話"""
    try:
        result = await worker_pool.dispatch("chat", req.prompt, "", req.timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    return result


@app.get("/api/health")
async def api_health():
    """健康檢查"""
    statuses = await worker_pool.worker_status()
    alive_count = sum(1 for s in statuses if s["alive"])
    logged_in_count = sum(1 for s in statuses if s["alive"] and s["logged_in"])
    total = len(statuses)

    if alive_count == 0:
        status = "down"
    elif logged_in_count < total:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "workers": statuses,
        "workers_available": sum(1 for s in statuses if s["alive"] and s["logged_in"] and not s["busy"]),
        "workers_total": total,
        "queue_waiting": worker_pool.waiting_count,
        "uptime_seconds": round(time.time() - _start_time),
    }


@app.post("/api/new-chat")
async def api_new_chat():
    """手動重置所有 worker 的 Gemini 對話"""
    from .gemini import new_chat
    results = []
    for i, bm in enumerate(worker_pool._workers):
        if bm.page:
            ok = await new_chat(bm.page)
            results.append({"worker": i, "success": ok})
    return {"results": results}


# ── Google GenAI API 相容端點 ──


def _extract_api_key(request: Request, key: str | None) -> str | None:
    """從 header 或 query string 提取 API key"""
    header_key = request.headers.get("x-goog-api-key")
    if header_key:
        return header_key
    return key


def _verify_api_key(request: Request, key: str | None):
    """驗證 API 金鑰（如果有設定 API_KEYS）"""
    if not settings.api_keys:
        return
    actual_key = _extract_api_key(request, key)
    if not actual_key or actual_key not in settings.api_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")


@app.post("/v1beta/models/{model}:generateContent")
async def genai_generate_content(model: str, request: Request, key: str = Query(default=None)):
    """Google GenAI API 相容端點"""
    _verify_api_key(request, key)

    body = await request.json()

    prompt_parts = []
    contents = body.get("contents", [])
    for content in contents:
        for part in content.get("parts", []):
            if "text" in part:
                prompt_parts.append(part["text"])
    prompt = "\n".join(prompt_parts)

    if not prompt:
        raise HTTPException(status_code=400, detail="No text content in request")

    tools = body.get("tools", [])
    has_google_search = any(
        "google_search" in t or "googleSearch" in t
        for t in tools
        if isinstance(t, dict)
    )
    if has_google_search:
        prompt = f"請搜尋最新的即時資訊來回答以下問題（{time.strftime('%Y-%m-%d')}）：\n\n{prompt}"

    gen_config = body.get("generationConfig", {})
    response_mime = gen_config.get("responseMimeType", "")
    response_modalities = gen_config.get("responseModalities", [])
    is_image = (
        response_mime.startswith("image/")
        or "Image" in response_modalities
        or "image" in response_modalities
    )

    kind = "generate" if is_image else "chat"
    timeout = settings.default_timeout

    try:
        result = await worker_pool.dispatch(kind, prompt, model, timeout)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="Queue full")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Request timeout")

    if not result.get("success"):
        return {
            "error": {
                "code": 400,
                "message": result.get("message", result.get("error", "Unknown error")),
                "status": "FAILED_PRECONDITION",
            }
        }

    if is_image:
        parts = []
        for img_data in result.get("images", []):
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
                mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
            else:
                b64 = img_data
                mime = "image/png"
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    else:
        parts = [{"text": result.get("text", "")}]

    return {
        "candidates": [
            {
                "content": {
                    "parts": parts,
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "modelVersion": model,
    }
```

Note: Don't forget to add `from contextlib import asynccontextmanager` at the top imports.

- [ ] **Step 2: Update test_api.py mocks**

Rewrite `tests/test_api.py`:

```python
"""API 端點測試"""
import pytest
from unittest.mock import AsyncMock, patch, PropertyMock
from httpx import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def mock_worker_pool():
    with patch("src.main.worker_pool") as mock:
        mock.start = AsyncMock()
        mock.stop = AsyncMock()
        mock.waiting_count = 0
        mock.worker_count = 1
        mock.worker_status = AsyncMock(return_value=[
            {"id": 0, "alive": True, "logged_in": True, "busy": False}
        ])
        mock._workers = []
        yield mock


@pytest.mark.asyncio
async def test_health_endpoint(mock_worker_pool):
    """GET /api/health 應回傳狀態"""
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["workers_total"] == 1
    assert data["workers_available"] == 1


@pytest.mark.asyncio
async def test_health_degraded(mock_worker_pool):
    """部分 worker 未登入應回傳 degraded"""
    mock_worker_pool.worker_status = AsyncMock(return_value=[
        {"id": 0, "alive": True, "logged_in": True, "busy": False},
        {"id": 1, "alive": True, "logged_in": False, "busy": False},
    ])
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    data = resp.json()
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_generate_missing_prompt():
    """POST /api/generate 沒有 prompt 應回 422"""
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/generate", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generate_success(mock_worker_pool):
    """POST /api/generate 成功時回傳圖片"""
    mock_worker_pool.dispatch = AsyncMock(return_value={
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

- [ ] **Step 3: Run all tests**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/main.py tests/test_api.py
git commit -m "feat: rewire main.py to use WorkerPool instead of single browser"
```

---

### Task 5: Remove old queue.py and update test_queue.py

**Files:**
- Delete: `src/queue.py`
- Modify: `tests/test_queue.py`

- [ ] **Step 1: Check no remaining imports of old queue module**

Run: `cd /home/ct/SDD/gemini-image && grep -r "from .queue\|from src.queue\|import queue" src/ tests/`

Verify only `tests/test_queue.py` references it. If `src/main.py` still imports it, fix that first.

- [ ] **Step 2: Delete src/queue.py**

```bash
rm src/queue.py
```

- [ ] **Step 3: Rewrite tests/test_queue.py to test WorkerPool's QueueFullError**

Replace `tests/test_queue.py` with:

```python
"""佇列滿載測試（WorkerPool 版）"""
import asyncio
import pytest
from src.worker_pool import WorkerPool, QueueFullError


@pytest.mark.asyncio
async def test_queue_full_rejects():
    """等待數超過上限時應拋出 QueueFullError"""
    pool = WorkerPool.__new__(WorkerPool)
    pool._workers = []
    pool._locks = []
    pool._max_waiting = 0
    pool._waiting = 0

    with pytest.raises(QueueFullError):
        await pool.dispatch("chat", "hello", "", 10)
```

- [ ] **Step 4: Run all tests**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove old queue.py, replaced by WorkerPool"
```

---

### Task 6: Add --worker flag to CLI login

**Files:**
- Modify: `src/cli.py`

- [ ] **Step 1: Add --worker argument to login parser**

In `src/cli.py`, change the login parser (around line 196):

```python
    # login
    login_parser = sub.add_parser("login", help="開啟瀏覽器登入 Google")
    login_parser.add_argument(
        "-w", "--worker", type=int, default=0,
        help="Worker 編號（預設 0）",
    )
```

- [ ] **Step 2: Update _do_login to accept worker_id**

Change `_do_login` function:

```python
async def _do_login(worker_id: int = 0):
    """開啟瀏覽器讓用戶手動登入 Google"""
    from .browser import BrowserManager
    from .config import get_worker_profile_dir

    profile_dir = get_worker_profile_dir(worker_id)
    bm = BrowserManager(headless=False, profile_dir=profile_dir)
    await bm.start()

    print(f"\nWorker {worker_id} 瀏覽器已開啟（profile: {profile_dir}）")
    print("請登入 Google 帳號。登入完成後按 Enter 關閉瀏覽器...")
    await asyncio.get_event_loop().run_in_executor(None, input)

    await bm.stop()
    print(f"Worker {worker_id} 登入狀態已儲存。")
```

- [ ] **Step 3: Update login command handler to pass worker_id**

In `src/cli.py`, change the login handler (around line 229):

```python
    elif args.command == "login":
        asyncio.run(_do_login(args.worker))
```

- [ ] **Step 4: Run all tests to verify no regression**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/cli.py
git commit -m "feat: login --worker N to setup multiple accounts"
```

---

### Task 7: Update .env.example and docs

**Files:**
- Modify: `.env` (add comment for WORKER_COUNT)

- [ ] **Step 1: Add WORKER_COUNT to .env**

Add after the `HEARTBEAT_INTERVAL` line in `.env`:

```
# Worker Pool（多帳號並行）
WORKER_COUNT=1
```

- [ ] **Step 2: Run full test suite**

Run: `cd /home/ct/SDD/gemini-image && uv run python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add .env
git commit -m "docs: add WORKER_COUNT to .env"
```

---

### Task 8: Integration smoke test

- [ ] **Step 1: Verify the service starts with WORKER_COUNT=1**

Run: `cd /home/ct/SDD/gemini-image && timeout 10 uv run python -m uvicorn src.main:app --port 18070 2>&1 || true`

Verify output contains: "1 個 worker" and "port 18070"

- [ ] **Step 2: Verify health endpoint works**

In a separate terminal:
```bash
curl -s http://localhost:18070/api/health | python -m json.tool
```

Expected: JSON with `workers` array, `workers_total: 1`

- [ ] **Step 3: Final commit with version bump**

Update `pyproject.toml` version from `1.0.2` to `1.1.0` (new feature):

```bash
git add pyproject.toml
git commit -m "feat: bump version to 1.1.0 — worker pool support"
```
