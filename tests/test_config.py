"""config 模組測試"""
import os
import pytest
from src.config import Settings


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
    assert s.default_timeout == 180
    assert s.heartbeat_interval == 300
    assert s.gemini_url == "https://gemini.google.com/app"
    assert s.stealth_language == "zh-TW,zh,en-US,en"
    assert s.stealth_timezone == "Asia/Taipei"
    # profile_dir 應為絕對路徑（~/.gemini-image/profiles）
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
