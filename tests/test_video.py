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
        p._can = {}
        p._mode = "spillover"
        p._next = 0
        return p

    def test_select_respects_the_allowed_set(self):
        p = self._pool()
        assert p._select({2, 3}) in (2, 3)

    def test_select_without_allowed_uses_everyone(self):
        p = self._pool()
        assert p._select() in range(4)

    def test_capable_ids_only_counts_true(self):
        p = self._pool()
        p._can = {"video": {0: False, 1: True, 2: None, 3: True}}
        assert p.capable_ids("video") == {1, 3}

    def test_capabilities_are_tracked_per_kind(self):
        """影片與音樂是兩件事，四個帳號都有音樂但只有一個有影片"""
        p = self._pool()
        p._can = {"video": {0: False, 2: True}, "music": {0: True, 2: True}}
        assert p.capable_ids("video") == {2}
        assert p.capable_ids("music") == {0, 2}

    def test_unknown_capability_is_not_treated_as_no(self):
        """探測失敗（頁面卡住）記 None，下次還要再試，不能當成「這帳號不行」"""
        p = self._pool()
        p._can = {"video": {0: None}}
        assert 0 not in p.capable_ids("video")
        assert p._can["video"].get(0) is not False

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


class TestMusicEndpoint:
    """創作音樂 —— 跟影片走同一條通用流程，只有選單項與結果元素不同"""

    def test_create_music_selector_covers_the_menu_label(self):
        from src.selectors import SELECTORS
        assert "創作音樂" in SELECTORS["create_music"]
        assert ".cdk-overlay-container" in SELECTORS["create_music"]

    def test_audio_result_is_the_player_button_not_an_audio_tag(self):
        """Gemini 給的是自訂專輯卡片，頁面上一個 <audio> 都沒有（2026-08-22 實測）"""
        from src.selectors import SELECTORS
        assert "播放音樂" in SELECTORS["audios"]

    def test_download_uses_the_real_aria_label(self):
        from src.selectors import SELECTORS
        assert "下載歌曲" in SELECTORS["download_audio"]

    @pytest.mark.asyncio
    async def test_missing_prompt_is_422(self):
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/music", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_success_returns_base64_audio(self, mock_worker_pool):
        mock_worker_pool.dispatch = AsyncMock(return_value={
            "success": True, "audio": "AAAA", "mime": "audio/mpeg",
            "prompt": "輕快的烏克麗麗", "elapsed_seconds": 42.0,
        })
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/music", json={"prompt": "輕快的烏克麗麗"})
        assert resp.status_code == 200
        d = resp.json()
        assert d["success"] is True and d["mime"] == "audio/mpeg"
        assert mock_worker_pool.dispatch.await_args.args[0] == "music"

    @pytest.mark.asyncio
    async def test_no_capable_worker_is_503_and_names_the_menu_item(self, mock_worker_pool):
        from src.worker_pool import NoCapableWorkerError
        mock_worker_pool.dispatch = AsyncMock(
            side_effect=NoCapableWorkerError("沒有任何帳號的工具選單裡有「創作音樂」"))
        from src.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/music", json={"prompt": "x"})
        assert resp.status_code == 503
        assert "創作音樂" in resp.json()["detail"]


class TestDiagnosticMargin:
    """內層等待要比外層 wait_for 早收工，否則診斷永遠跑不到

    2026-08-22 音樂那次等滿 600 秒被外層在同一秒取消，截圖與節點清單全部沒留下，
    等於白等十分鐘。
    """

    def test_margin_is_positive(self):
        from src.gemini import _DIAGNOSTIC_MARGIN
        assert _DIAGNOSTIC_MARGIN > 0

    def test_budget_leaves_room_for_diagnostics(self):
        from src.gemini import _DIAGNOSTIC_MARGIN
        for outer in (120, 600, 900):
            assert max(30, outer - _DIAGNOSTIC_MARGIN) < outer

    def test_short_timeouts_still_get_a_usable_budget(self):
        """外層設很短時不要算成負數"""
        from src.gemini import _DIAGNOSTIC_MARGIN
        assert max(30, 10 - _DIAGNOSTIC_MARGIN) == 30


class TestMusicDownloadMenu:
    """點「下載歌曲」會再開一個格式選單，要再點「僅音訊」才會真的下載

    2026-08-22 實測：不點第二下的話 expect_download 空等 300 秒逾時。
    診斷裡看得到那兩個選項：「影片／附封面圖片的音訊」與「僅音訊／MP3 音軌」。
    """

    def test_format_menu_selector_prefers_audio_only(self):
        from src.selectors import SELECTORS
        sel = SELECTORS["download_audio_format"]
        assert "僅音訊" in sel and "MP3" in sel
        assert "影片" not in sel      # 別誤點成「附封面圖片的音訊」

    def test_music_passes_the_menu_key(self):
        """影片不需要第二段，音樂需要 —— 差異要出現在呼叫端"""
        import inspect
        from src import gemini
        src = inspect.getsource(gemini.generate_music)
        assert "download_menu_key" in src
        assert "download_audio_format" in src

    def test_video_does_not_pass_a_menu_key(self):
        import inspect
        from src import gemini
        assert "download_menu_key" not in inspect.getsource(gemini.generate_video)
