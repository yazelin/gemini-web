"""音樂的「長度／人聲／類型」三顆選項 — 端點形狀與對照表

瀏覽器那半（真的點鈕）沒辦法在單元測試跑，靠實跑驗收；這裡守住：
值的驗證在 API 層擋掉、合法的值會原樣交給 worker_pool、對照表沒有子字串撞名。
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
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


class TestOptionTable:
    def test_known_values_are_valid(self):
        from src.selectors import invalid_music_option
        assert invalid_music_option({"length": "short", "vocals": "instrumental", "genre": "lofi"}) is None
        assert invalid_music_option({"length": "", "vocals": "", "genre": ""}) is None
        assert invalid_music_option(None) is None

    def test_unknown_value_names_the_allowed_list(self):
        from src.selectors import invalid_music_option
        msg = invalid_music_option({"genre": "polka"})
        assert msg and "polka" in msg and "lofi" in msg

    def test_labels_match_the_probed_ui(self):
        """2026-10-07 worker 2 實測抓到的字；改這裡要附新截圖"""
        from src.selectors import MUSIC_OPTIONS
        assert MUSIC_OPTIONS["length"][1]["short"] == "簡短"
        assert MUSIC_OPTIONS["vocals"][1]["instrumental"] == "純音樂"
        assert len(MUSIC_OPTIONS["genre"][1]) == 17

    def test_substring_collision_exists_so_click_must_be_exact(self):
        """「流行」是「韓國流行樂」的子字串；點法用 role+exact name，別改回 has-text"""
        from src.selectors import MUSIC_OPTIONS
        genre = MUSIC_OPTIONS["genre"][1]
        assert genre["pop"] in genre["kpop"]


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_bad_option_is_422_before_dispatch(self, mock_worker_pool):
        from src.main import app
        mock_worker_pool.dispatch = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/music", json={"prompt": "x", "vocals": "loud"})
        assert resp.status_code == 422
        assert "loud" in resp.json()["detail"]
        mock_worker_pool.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_options_reach_dispatch_only_when_given(self, mock_worker_pool):
        from src.main import app
        mock_worker_pool.dispatch = AsyncMock(return_value={"success": True, "audio": "QUJD", "mime": "audio/mpeg"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/music", json={"prompt": "x", "length": "short", "genre": "lofi"})
            assert resp.status_code == 200
            extra = mock_worker_pool.dispatch.call_args.kwargs["extra"]
            assert extra == {"options": {"length": "short", "genre": "lofi"}}

            await c.post("/api/music", json={"prompt": "x"})
            assert mock_worker_pool.dispatch.call_args.kwargs["extra"] is None
