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


class TestCapabilityRouting:
    """影片請求只派給偵測到有「建立影片」的帳號

    Veo 只給付費層，而 Google One 家庭群組分享的方案從畫面上看不出來
    （帳號仍可能顯示「升級」按鈕），所以能力必須實測選單，不能用外觀判斷。
    """

    def _pool(self, count=4):
        from src.worker_pool import WorkerPool
        p = WorkerPool.__new__(WorkerPool)
        p._count = count
        p._idle_ids = set(range(count))
        p._can_video = {}
        p._mode = "spillover"
        p._next = 0
        return p

    def test_select_respects_the_allowed_set(self):
        p = self._pool()
        assert p._select({2, 3}) in (2, 3)

    def test_select_without_allowed_uses_everyone(self):
        p = self._pool()
        assert p._select() in range(4)

    def test_video_capable_ids_only_counts_true(self):
        p = self._pool()
        p._can_video = {0: False, 1: True, 2: None, 3: True}
        assert p.video_capable_ids() == {1, 3}

    def test_unknown_capability_is_not_treated_as_no(self):
        """探測失敗（頁面卡住）記 None，下次還要再試，不能當成「這帳號不行」"""
        p = self._pool()
        p._can_video = {0: None}
        assert 0 not in p.video_capable_ids()
        assert p._can_video.get(0) is not False

    @pytest.mark.asyncio
    async def test_no_capable_worker_is_503(self, mock_worker_pool):
        from src.worker_pool import NoCapableWorkerError
        mock_worker_pool.dispatch = AsyncMock(
            side_effect=NoCapableWorkerError("沒有任何帳號的工具選單裡有「建立影片」"))
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/video", json={"prompt": "x"})
        assert resp.status_code == 503
        assert "建立影片" in resp.json()["detail"]
