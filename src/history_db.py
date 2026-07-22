"""Admin History 頁用的請求記錄（輕量版，不存圖片本體——圖只回給呼叫端）"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_DB_PATH = Path(settings.data_dir) / "admin.db"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # CREATE TABLE IF NOT EXISTS is cheap enough to run on every connect —
    # simpler than requiring callers to remember an init_db() step first
    # (a monkeypatched _DB_PATH in tests would otherwise start table-less).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL,
            via TEXT,
            error TEXT,
            duration_seconds REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests(created_at DESC)"
    )
    return conn


def init_db() -> None:
    _connect().close()


def record(
    *,
    kind: str,
    prompt: str,
    status: str,
    via: str = "",
    error: str = "",
    duration_seconds: float = 0.0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO requests (id, kind, prompt, status, via, error, duration_seconds, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex[:12],
                kind,
                prompt,
                status,
                via,
                error,
                duration_seconds,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        # keep the table from growing forever — this is an admin log, not an audit trail
        conn.execute(
            "DELETE FROM requests WHERE id NOT IN "
            "(SELECT id FROM requests ORDER BY created_at DESC LIMIT 500)"
        )


def list_recent(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def stats() -> dict[str, int]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        succeeded = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status = 'succeeded'"
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status = 'failed'"
        ).fetchone()[0]
    return {"total": int(total), "succeeded": int(succeeded), "failed": int(failed)}
