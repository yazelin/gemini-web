"""影片生成（Veo）— 端點與選擇器

實際的瀏覽器流程沒辦法在單元測試裡跑（要登入態與真的 Gemini 頁面），
所以這裡只測「端點形狀」與「選擇器涵蓋範圍」；瀏覽器那半靠實跑驗收。
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_worker_pool():
    """同 test_api.py 那份；那邊是 autouse 且限於該檔，這裡自己備一份"""
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


class TestSelectors:
    def test_create_video_covers_the_menu_label(self):
        """使用者實際看到的是「建立影片」"""
        from src.selectors import SELECTORS
        assert "建立影片" in SELECTORS["create_video"]

    def test_create_video_covers_english_and_variants(self):
        from src.selectors import SELECTORS
        sel = SELECTORS["create_video"]
        for want in ("Create video", "製作影片", "menuitemcheckbox"):
            assert want in sel

    def test_create_video_is_scoped_to_the_overlay(self):
        """不加 scope 會誤抓 composer 上的按鈕——圖片那條就是這樣踩過"""
        from src.selectors import SELECTORS
        assert ".cdk-overlay-container" in SELECTORS["create_video"]

    def test_video_result_selectors_exist(self):
        from src.selectors import SELECTORS
        assert "videos" in SELECTORS and "video" in SELECTORS["videos"]
        assert "download_video" in SELECTORS


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_missing_prompt_is_422(self):
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/video", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_success_returns_base64_video(self, mock_worker_pool):
        mock_worker_pool.dispatch = AsyncMock(return_value={
            "success": True, "video": "AAAA", "mime": "video/mp4",
            "prompt": "一隻貓在跳舞", "elapsed_seconds": 180.0,
        })
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/video", json={"prompt": "一隻貓在跳舞"})
        assert resp.status_code == 200
        d = resp.json()
        assert d["success"] is True and d["mime"] == "video/mp4"
        assert mock_worker_pool.dispatch.await_args.args[0] == "video"

    @pytest.mark.asyncio
    async def test_default_timeout_is_generous(self, mock_worker_pool):
        """Veo 要數分鐘，預設 60 秒一定不夠"""
        mock_worker_pool.dispatch = AsyncMock(return_value={"success": True, "video": ""})
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/api/video", json={"prompt": "x"})
        assert mock_worker_pool.dispatch.await_args.args[3] >= 300
