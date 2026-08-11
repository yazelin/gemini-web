"""非同步 job 的 SQLite 儲存。

出圖要 30 到 300 秒，同步端點撐不過中間任何一層代理（Cloudflare Worker、
nginx 預設值都會先斷）。消費端改成「送出拿 id、之後輪詢」，job 狀態就得
有地方存。

存在 admin.db（跟請求記錄同一個檔）而不是記憶體：這個服務會重啟，記憶體
版一重啟所有進行中的 job 就人間蒸發，消費端會永遠輪詢一個不存在的東西。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_DB_PATH = Path(settings.data_dir) / "admin.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            model TEXT NOT NULL,
            body_json TEXT NOT NULL,
            response_json TEXT,
            error TEXT,
            api_key_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    return conn


def create(model: str, body: dict, api_key_name: str | None) -> str:
    job_id = f"job_{uuid.uuid4().hex}"
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, model, body_json, api_key_name, created_at, updated_at)"
            " VALUES (?, 'queued', ?, ?, ?, ?, ?)",
            (job_id, model, json.dumps(body), api_key_name, now, now),
        )
    return job_id


def get(job_id: str, api_key_name: str | None) -> dict[str, Any] | None:
    """只有送出這個 job 的 key 查得到。查不到就是 None，呼叫端一律回 404，
    不要洩漏「這個 id 存在但不是你的」。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND api_key_name IS ?", (job_id, api_key_name)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["response"] = json.loads(d["response_json"]) if d["response_json"] else None
    return d


def _set(job_id: str, **cols: Any) -> int:
    cols["updated_at"] = _now()
    assigns = ", ".join(f"{k} = ?" for k in cols)
    with _connect() as conn:
        cur = conn.execute(f"UPDATE jobs SET {assigns} WHERE id = ?", (*cols.values(), job_id))
        return cur.rowcount


def mark_running(job_id: str) -> None:
    _set(job_id, status="running")


def finish(job_id: str, response: dict) -> None:
    _set(job_id, status="succeeded", response_json=json.dumps(response), error=None)


def fail(job_id: str, error: str) -> None:
    _set(job_id, status="failed", error=str(error)[:500], response_json=None)


def fail_stale_running() -> int:
    """啟動時呼叫。上一輪跑到一半的 job 不可能自己完成，標成 failed。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, updated_at = ?"
            " WHERE status = 'running'",
            ("服務重啟，這筆生成沒有完成", _now()),
        )
        return cur.rowcount
