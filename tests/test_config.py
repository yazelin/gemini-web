"""config 模組測試"""
import os
import pytest
from src.config import Settings, get_worker_profile_dir


def test_default_settings(monkeypatch):
    """預設值應正確"""
    from pathlib import Path
    # 清除 .env 影響
    monkeypatch.delenv("PROFILE_DIR", raising=False)
    monkeypatch.delenv("HEADLESS", raising=False)
    monkeypatch.delenv("DEFAULT_TIMEOUT", raising=False)
    s = Settings()
    assert s.headless is False
    assert s.port == 8070
    assert s.queue_max_size == 10
    assert s.default_timeout == 240
    assert s.heartbeat_interval == 300
    assert s.gemini_url == "https://gemini.google.com/app"
    assert s.stealth_language == "zh-TW,zh,en-US,en"
    assert s.stealth_timezone == "Asia/Taipei"
    # profile_dir 應為絕對路徑（~/.gemini-web/profiles）
    assert Path(s.profile_dir).is_absolute()


def test_settings_from_env(monkeypatch):
    """應從環境變數讀取設定"""
    monkeypatch.setenv("HEADLESS", "true")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("QUEUE_MAX_SIZE", "5")
    s = Settings()
    assert s.headless is True
    assert s.port == 9090
    assert s.queue_max_size == 5


def test_default_worker_count(monkeypatch):
    """worker_count 預設為 1"""
    monkeypatch.delenv("WORKER_COUNT", raising=False)
    s = Settings()
    assert s.worker_count == 1


def test_worker_count_from_env(monkeypatch):
    """應從 WORKER_COUNT 環境變數讀取"""
    monkeypatch.setenv("WORKER_COUNT", "3")
    s = Settings()
    assert s.worker_count == 3


def test_worker_profile_dir_zero(monkeypatch):
    """worker 0 should use base profiles/ dir"""
    monkeypatch.delenv("PROFILE_DIR", raising=False)
    path = get_worker_profile_dir(0)
    assert path.endswith("profiles")
    assert "-" not in os.path.basename(path)


def test_worker_profile_dir_nonzero(monkeypatch):
    """worker N should use profiles-N/ dir"""
    monkeypatch.delenv("PROFILE_DIR", raising=False)
    path = get_worker_profile_dir(1)
    assert path.endswith("profiles-1")
    path2 = get_worker_profile_dir(2)
    assert path2.endswith("profiles-2")
