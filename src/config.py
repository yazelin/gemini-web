"""環境變數設定"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 預設資料目錄：~/.gemini-image/
_DEFAULT_DATA_DIR = str(Path.home() / ".gemini-image")


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(val: str | None, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


class Settings:
    """服務設定，從環境變數讀取"""

    def __init__(self) -> None:
        # 瀏覽器
        self.headless: bool = _bool(os.getenv("HEADLESS"), False)
        self.profile_dir: str = os.getenv(
            "PROFILE_DIR", str(Path(_DEFAULT_DATA_DIR) / "profiles")
        )
        self.gemini_url: str = os.getenv("GEMINI_URL", "https://gemini.google.com/app")

        # Stealth
        self.stealth_language: str = os.getenv("STEALTH_LANGUAGE", "zh-TW,zh,en-US,en")
        self.stealth_timezone: str = os.getenv("STEALTH_TIMEZONE", "Asia/Taipei")

        # 服務
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = _int(os.getenv("PORT"), 8070)
        self.queue_max_size: int = _int(os.getenv("QUEUE_MAX_SIZE"), 10)
        self.default_timeout: int = _int(os.getenv("DEFAULT_TIMEOUT"), 240)

        # 心跳
        self.heartbeat_interval: int = _int(os.getenv("HEARTBEAT_INTERVAL"), 300)


settings = Settings()
