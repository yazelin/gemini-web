#!/usr/bin/env python3
"""每天真的生一張圖，確認瀏覽器路還活著；壞了就開 GitHub issue。

為什麼需要這支：2026-08-07 Gemini 停止對 headless_shell 出圖，出圖從此全滅，
但**五天沒人發現**。原因有三層，每一層都讓故障看起來像正常：

  1. `/api/generate` 失敗會被付費官方 API 靜默頂替 —— curl 照樣 200 拿到圖。
  2. `/api/health` 只看「輸入框和工具鈕在不在」，四個 worker 全綠。
  3. 消費端（ai-brain-site 的格莉奇日記）出圖失敗會安靜退回文字日記，
     workflow 仍然 success。

所以健康與否不能看 API 回應、不能看 health、也不能看消費端有沒有報錯，
**只能看 admin.db 那筆的 `via` 是不是 `browser`**。這支就是在做這件事。

排程放在早上，比日記那班（22:10）早，壞掉當天就來得及處理。

    python3 scripts/canary.py            # 檢查，壞了才開 issue
    python3 scripts/canary.py --dry-run  # 只檢查，不開 issue
"""
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "http://127.0.0.1:8070"
REPO = "yazelin/gemini-web"
LABEL = "canary"
PROMPT = "a single green apple on a plain white background, flat illustration"
# app 的 DEFAULT_TIMEOUT 是 400s；比它短一點，讓 app 自己的錯誤先浮出來
TIMEOUT = 380


def data_dir() -> Path:
    """跟 src/config.py 同樣的規則：新目錄優先，否則沿用舊的。"""
    new, old = Path.home() / ".gemini-web", Path.home() / ".gemini-image"
    return new if (new / "admin.db").exists() else old


def api_key() -> str:
    """從 .env 撈一把 key。沒設 API_KEYS 的話服務本來就不驗，回空字串即可。"""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return ""
    for line in env.read_text("utf-8").splitlines():
        if line.startswith("API_KEYS="):
            return line.split("=", 1)[1].strip().strip('"').split(",")[0].strip()
    return ""


def generate() -> tuple[bool, str]:
    body = json.dumps({"prompt": PROMPT, "timeout": TIMEOUT}).encode()
    headers = {"Content-Type": "application/json"}
    key = api_key()
    if key:
        headers["x-goog-api-key"] = key
    req = urllib.request.Request(f"{API}/api/generate", body, headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT + 30) as f:
            payload = json.load(f)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:                       # 連不上、逾時都算壞
        return False, f"{type(e).__name__}: {e}"
    if not payload.get("success"):
        return False, str(payload.get("message") or payload.get("error"))[:300]
    return True, ""


def last_request() -> dict | None:
    """最後一筆 image 請求。判死活的唯一依據是它的 via，不是 API 回應。"""
    db = data_dir() / "admin.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_at, kind, status, via, worker_id, duration_seconds, error"
        " FROM requests WHERE kind IN ('generate', 'edit')"
        " ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def is_fresh(created_at: str) -> bool:
    """確定看到的是這次跑出來的那筆，不是幾天前的舊資料。"""
    try:
        ts = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts < timedelta(minutes=15)


def has_open_issue() -> bool:
    out = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--label", LABEL,
         "--state", "open", "--json", "number"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        print(f"! 查不到既有 issue（{out.stderr.strip()[:120]}），照樣開一張")
        return False
    return bool(json.loads(out.stdout or "[]"))


def open_issue(reason: str, row: dict | None) -> None:
    if has_open_issue():
        print("已經有開著的 canary issue，不重複開")
        return
    detail = json.dumps(row, ensure_ascii=False, indent=2) if row else "（沒有對應的 DB 記錄）"
    body = (
        f"金絲雀在 {datetime.now():%Y-%m-%d %H:%M} 發現瀏覽器路出圖不正常。\n\n"
        f"**原因**：{reason}\n\n"
        f"**admin.db 最後一筆**：\n```json\n{detail}\n```\n\n"
        "注意 `/api/generate` 可能還是回 200 —— 付費官方 API 會靜默頂替，"
        "`/api/health` 也會全綠。判死活只能看上面那筆的 `via` 是不是 `browser`。\n\n"
        "查法（依序）：\n"
        "1. `journalctl -u gemini-web-api --since '1 hour ago' | grep -v 'RAW BODY:'`\n"
        "2. `ls -t ~/.gemini-image/diagnostics/` —— 卡住現場的截圖\n"
        "3. 還是看不出來就複製一份 profile 到 /tmp 另開瀏覽器重現"
        "（不搶 prod 的 profile 鎖），直接看畫面\n"
    )
    out = subprocess.run(
        ["gh", "issue", "create", "--repo", REPO, "--label", LABEL,
         "--title", "gemini-web 出圖壞了（金絲雀偵測）", "--body", body],
        capture_output=True, text=True, timeout=60,
    )
    print(out.stdout.strip() or out.stderr.strip()[:300])


def main() -> int:
    dry = "--dry-run" in sys.argv
    print(f"金絲雀 {datetime.now():%Y-%m-%d %H:%M:%S}")

    ok, err = generate()
    row = last_request()

    if not ok:
        reason = f"出圖請求失敗：{err}"
    elif row is None:
        reason = "admin.db 查不到任何 image 請求"
    elif not is_fresh(row["created_at"]):
        reason = f"最後一筆是 {row['created_at']}，這次的請求根本沒被記錄"
    elif row["status"] != "succeeded" or row["via"] != "browser":
        # 這就是 08-07 那次的樣子：API 回 200（付費頂替），DB 裡是 failed
        reason = (f"瀏覽器路沒成功：status={row['status']} via={row['via'] or '(空)'}"
                  f" error={str(row['error'])[:120]}")
    else:
        print(f"  ok  via=browser worker={row['worker_id']} "
              f"{row['duration_seconds']:.0f}s")
        return 0

    print(f"  壞了：{reason}")
    if dry:
        print("  --dry-run，不開 issue")
    else:
        open_issue(reason, row)
    return 1


if __name__ == "__main__":
    sys.exit(main())
