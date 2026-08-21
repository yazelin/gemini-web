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
