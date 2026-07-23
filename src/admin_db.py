"""Admin webui 的 sqlite 儲存：請求記錄（輕量版，不存圖片本體）+ 動態發放的 API keys。

靜態的 .env API_KEYS 集合仍然有效（見 config.py／main.py 的 _is_valid_api_key）—
這裡的 api_keys 表是疊加上去的第二個來源，讓 admin webui 能像 codex-image-service
一樣現場發新 key，不用改 .env 重啟。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .security import generate_api_key, hash_api_key

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
    # Columns added after the table already existed in production — plain
    # CREATE TABLE IF NOT EXISTS above won't retrofit a live db, so add them
    # by hand if missing.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)").fetchall()}
    for col, ddl in (
        ("image_paths", "ALTER TABLE requests ADD COLUMN image_paths TEXT NOT NULL DEFAULT '[]'"),
        ("api_key_name", "ALTER TABLE requests ADD COLUMN api_key_name TEXT"),
        ("worker_id", "ALTER TABLE requests ADD COLUMN worker_id INTEGER"),
    ):
        if col not in existing_cols:
            conn.execute(ddl)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            requests_count INTEGER NOT NULL DEFAULT 0
        )
        """
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
    image_paths: list[str] | None = None,
    api_key_name: str = "",
    worker_id: int | None = None,
) -> str:
    request_id = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO requests
                (id, kind, prompt, status, via, error, duration_seconds, created_at,
                 image_paths, api_key_name, worker_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                kind,
                prompt,
                status,
                via,
                error,
                duration_seconds,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(image_paths or []),
                api_key_name,
                worker_id,
            ),
        )
        # keep the table from growing forever — this is an admin log, not an audit trail
        conn.execute(
            "DELETE FROM requests WHERE id NOT IN "
            "(SELECT id FROM requests ORDER BY created_at DESC LIMIT 500)"
        )
    return request_id


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["image_paths"] = json.loads(item.get("image_paths") or "[]")
    except json.JSONDecodeError:
        item["image_paths"] = []
    return item


def list_recent(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_decode(row) for row in rows]


def get_request(request_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return _decode(row) if row else None


def delete_request(request_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))


# ── dynamically-issued API keys ──


def create_api_key(name: str) -> tuple[dict[str, Any], str]:
    """Returns (row, raw_key). The raw key is only ever available here —
    only its sha256 hash is stored."""
    raw_key = generate_api_key()
    key_id = f"key_{raw_key[-12:]}"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO api_keys (id, name, key_hash, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
            (key_id, name.strip() or "Unnamed key", hash_api_key(raw_key), datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return dict(row), raw_key


def list_api_keys() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def get_api_key_by_token(token: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (hash_api_key(token),)
        ).fetchone()
    return dict(row) if row else None


def mark_api_key_used(key_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ?, requests_count = requests_count + 1 WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), key_id),
        )


def disable_api_key(key_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE api_keys SET enabled = 0 WHERE id = ?", (key_id,))


def delete_api_key(key_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))


# ── generic settings (dispatch mode 等) ──


def get_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def has_any_dynamic_key() -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone() is not None


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
