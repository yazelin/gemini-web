"""Persists generated images to disk for the admin History page.

gemini-web's actual API endpoints never needed this — a request hands its
base64 images straight back to the caller and moves on, nothing is written
anywhere. This module exists purely so /admin/requests can show a link,
mirroring codex-image-service. Every successful generate/edit call also gets
a copy written under GENERATED_DIR; the response the caller receives is
unchanged.
"""
from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path

from .config import settings


def _generated_dir() -> Path:
    d = Path(settings.generated_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_images(images: list[str]) -> list[str]:
    """Decode data-URL/base64 strings, write each under GENERATED_DIR, return filenames."""
    d = _generated_dir()
    filenames = []
    for img in images:
        if img.startswith("data:") and "," in img:
            header, b64 = img.split(",", 1)
            ext = "jpg" if "jpeg" in header else "png"
        else:
            b64, ext = img, "png"
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        filename = f"{uuid.uuid4().hex}.{ext}"
        (d / filename).write_bytes(raw)
        filenames.append(filename)
    return filenames


def save_media(b64: str, ext: str) -> str | None:
    """把一段 base64 媒體存進 GENERATED_DIR，回檔名；解不開回 None。

    影片與音樂跟圖片放同一個目錄，所以 sweep_old 的保留天數一起管，History 頁
    也用同一組連結邏輯。它們原本只以 base64 回給呼叫端、伺服器上不留檔，
    History 看得到「跑過、成功、耗時多久」卻拿不回檔案。
    """
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    (_generated_dir() / filename).write_bytes(raw)
    return filename


def delete_files(filenames: list[str]) -> None:
    d = _generated_dir()
    for name in filenames:
        (d / name).unlink(missing_ok=True)


def sweep_old(retention_days: int) -> int:
    """Delete files older than retention_days, return count deleted.

    ponytail: runs inline (called after each save) instead of as a scheduled
    background service — simplest thing that works at gemini-web's traffic
    volume. Upgrade to a real interval task if that stops being true.
    """
    d = _generated_dir()
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for f in d.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            deleted += 1
    return deleted
