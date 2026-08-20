"""API 端點測試"""
import pytest
from unittest.mock import AsyncMock, patch, PropertyMock
from httpx import AsyncClient, ASGITransport

from src.config import settings


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


@pytest.mark.asyncio
@pytest.mark.parametrize("path,payload", [
    ("/api/generate", {"prompt": "test"}),
    ("/api/chat", {"prompt": "test"}),
    ("/api/edit", {"prompt": "test", "reference_image": "data:image/png;base64,abc"}),
    ("/api/chat-file", {"prompt": "test", "file": "data:audio/mpeg;base64,abc", "filename": "a.mp3"}),
])
async def test_browser_endpoints_require_key(mock_worker_pool, monkeypatch, path, payload):
    """設過 key 之後，瀏覽器路的三個端點沒帶 key 就要 403（免得公開網址被拿去
    燒訂閱配額），帶對的 key 照舊放行。"""
    monkeypatch.setattr(settings, "api_keys", ["secret"])
    mock_worker_pool.dispatch = AsyncMock(return_value={
        "success": True,
        "images": ["data:image/png;base64,abc"],
        "prompt": "test",
        "elapsed_seconds": 1.0,
    })
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post(path, json=payload)).status_code == 403
        ok = await client.post(path, json=payload, headers={"x-goog-api-key": "secret"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_chat_file_dispatches_with_filename(mock_worker_pool, monkeypatch):
    """/api/chat-file 要把 file 與 filename 一起丟給 worker。

    filename 不是裝飾品:Gemini 靠副檔名判型別,掉了的話音訊會被當成不明檔案,
    回來的是「請提供音檔」那種假成功——最難查的一種。
    """
    monkeypatch.setattr(settings, "api_keys", [])
    mock_worker_pool.dispatch = AsyncMock(return_value={
        "success": True, "text": "沒有人聲", "prompt": "test", "elapsed_seconds": 3.0,
    })
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat-file", json={
            "prompt": "有沒有人聲?", "file": "data:audio/mpeg;base64,abc", "filename": "song.mp3",
        })
    assert resp.status_code == 200
    assert resp.json()["text"] == "沒有人聲"
    kind, _prompt, _model, _timeout = mock_worker_pool.dispatch.call_args[0]
    assert kind == "chat_file"
    extra = mock_worker_pool.dispatch.call_args[1]["extra"]
    assert extra["filename"] == "song.mp3"
    assert extra["file"].endswith("abc")


@pytest.mark.asyncio
async def test_chat_file_rejects_empty_file(mock_worker_pool, monkeypatch):
    """沒帶 file 就要 400,不要送進去空跑一輪。"""
    monkeypatch.setattr(settings, "api_keys", [])
    from src.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat-file", json={"prompt": "x", "file": ""})
    assert resp.status_code == 400
