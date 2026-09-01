"""影片與音樂的產出要落地，History 才連得到檔案

原本它們只以 base64 回給呼叫端、伺服器上不留檔，History 看得到「跑過、成功、
耗時多久」卻拿不回檔案（2026-08-22 使用者回報「只有圖片出現」就是這件事）。
"""
import base64
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_generated(tmp_path, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path))


class TestSaveMedia:
    def test_writes_the_bytes_with_the_given_extension(self, tmp_path):
        from src.image_store import save_media
        name = save_media(base64.b64encode(b"fake-mp4-bytes").decode(), "mp4")
        assert name.endswith(".mp4")
        assert (tmp_path / name).read_bytes() == b"fake-mp4-bytes"

    def test_audio_extension(self, tmp_path):
        from src.image_store import save_media
        assert save_media(base64.b64encode(b"x").decode(), "mp3").endswith(".mp3")

    def test_bad_base64_returns_none(self):
        from src.image_store import save_media
        assert save_media("!!!not base64!!!", "mp4") is None

    def test_empty_payload_returns_none(self):
        from src.image_store import save_media
        assert save_media("", "mp4") is None

    def test_names_do_not_collide(self, tmp_path):
        from src.image_store import save_media
        b = base64.b64encode(b"same").decode()
        assert save_media(b, "mp4") != save_media(b, "mp4")

    def test_sweep_also_clears_media(self, tmp_path):
        """媒體跟圖片放同一個目錄，所以保留天數一起管"""
        import os, time
        from src.image_store import save_media, sweep_old
        name = save_media(base64.b64encode(b"x").decode(), "mp4")
        old = time.time() - 40 * 86400
        os.utime(tmp_path / name, (old, old))
        assert sweep_old(30) == 1
        assert not (tmp_path / name).exists()


class TestHistoryLinkLabels:
    def test_label_follows_the_extension(self):
        from src.admin import _requests_table
        rows = [{"id": "a", "kind": "video", "status": "succeeded", "prompt": "x",
                 "created_at": "2026-08-22T00:00:00", "image_paths": ["abc.mp4"],
                 "duration_seconds": 1.0}]
        html = _requests_table(rows, "")
        assert ">video</a>" in html

    def test_audio_label(self):
        from src.admin import _requests_table
        rows = [{"id": "a", "kind": "music", "status": "succeeded", "prompt": "x",
                 "created_at": "2026-08-22T00:00:00", "image_paths": ["abc.mp3"],
                 "duration_seconds": 1.0}]
        assert ">audio</a>" in _requests_table(rows, "")

    def test_images_still_say_image(self):
        from src.admin import _requests_table
        rows = [{"id": "a", "kind": "generate", "status": "succeeded", "prompt": "x",
                 "created_at": "2026-08-22T00:00:00", "image_paths": ["abc.png"],
                 "duration_seconds": 1.0}]
        assert ">image</a>" in _requests_table(rows, "")


class TestNoStaleHandles:
    """媒體卡片在歌曲收尾時會重繪，抱著 ElementHandle 會變成 not attached

    2026-08-22 實測：重點那次直接掛在
    `ElementHandle.scroll_into_view_if_needed: Element is not attached to the DOM`。
    """

    def test_download_uses_page_click_not_a_handle(self):
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._generate_media)
        assert "page.click(SELECTORS[download_key]" in src
        assert "btn.click()" not in src
        assert "scroll_into_view_if_needed" not in src

    def test_menu_click_is_retried(self):
        """選單偶爾只吃到 hover，要能重點"""
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._generate_media)
        assert "for attempt in range(3)" in src


class TestSubmitVerification:
    """Enter 有時不觸發送出，要驗證並退回點送出鈕

    2026-08-22 實測：音樂模式的範本牆頁面上按 Enter 沒送出，prompt 一直躺在
    輸入框裡，然後等滿 555 秒逾時——截圖顯示字還在框裡、送出鈕是亮的。

    2026-08-31 影片又中一次（空等 535 秒），於是這條驗證抽成共用的
    submit_prompt()，四個送出點都走它。行為細節釘在 test_prompt_submit.py，
    這裡只確認 _generate_media 確實有接上去、沒有自己另寫一套。
    """

    def test_submit_goes_through_the_verified_helper(self):
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._generate_media)
        assert "submit_prompt(" in src
        # 不可以自己按 Enter 繞過驗證
        assert 'keyboard.press("Enter")' not in src

    def test_not_sent_aborts_instead_of_waiting_out_the_timeout(self):
        """送不出去要立刻收工，不能再讓使用者等滿 580 秒"""
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._generate_media)
        assert "send_err" in src and "return _error(" in src


class TestToolMenuClick:
    """工具選單項也不能抱 handle 點

    選單變長之後（多了「更多上傳選項」「更多工具」）那一項可能要捲動才點得到，
    抱著 ElementHandle 點會直接 Timeout 5000ms。2026-08-22 影片連兩次卡在這裡。
    """

    def test_uses_page_click_with_fallback(self):
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._enter_tool_mode)
        assert "page.click(SELECTORS[selector_key]" in src
        assert "force=True" in src
        assert "create_img_btn.click" not in src


class TestVideoDualPath:
    """Gemini 兩版並行，影片要兩條路都試

    2026-08-22 實測：worker 2 的側欄有「影片」「即時通訊」「Spark BETA」，
    worker 0 沒有；而舊版工具選單裡的「建立影片」點了模式不黏（結果生出一張
    靜態圖、輸入框退回普通聊天）。
    """

    def test_sidebar_first_then_tool_menu(self):
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._ensure_video_mode)
        assert "_enter_video_via_sidebar" in src
        assert "_ensure_tool_mode" in src
        assert src.index("_enter_video_via_sidebar") < src.index("_ensure_tool_mode")

    def test_only_video_takes_the_dual_path(self):
        """音樂沒改版，還是走工具選單"""
        import inspect
        from src import gemini
        src = inspect.getsource(gemini._generate_media)
        assert 'mode_key == "create_video"' in src

    def test_sidebar_selector_scoped_to_nav(self):
        """不加 nav scope 會誤抓內文裡的「影片」字樣"""
        from src.selectors import SELECTORS
        assert "nav" in SELECTORS["sidebar_video"]
