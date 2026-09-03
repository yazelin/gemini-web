#!/usr/bin/env python3
"""gemini-web 健檢 —— 沒事的時候跑一下,看服務有沒有在偷偷爛掉。

唯讀,不會動到服務或資料。只用標準函式庫,不必 uv run:

    python3 scripts/checkup.py

看的是 2026-08-05 那次事故留下來的兩個教訓（詳見 PR #28）：

  1. 記憶體洩漏 —— 舊版 `page.route("**/*")` 讓每個請求永久留住
     Request+Route+Response 三個物件,Python RSS 兩天從 78MB 長到 1.06GB,
     8/03 整個服務被 oom-kill。修好後 RSS 應該長期待在低檔。

  2. 「殼還在但 session 已死」—— 頁面輸入框都在、health 全綠,但送出去
     再也等不到回應,四個 worker 一路卡到有人發現。現在連續 2 次
     連續沒拿到結果會自動重啟 worker,所以要看的是「有沒有一直在觸發」。
"""
import json
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICE = "gemini-web-api.service"
API = "http://127.0.0.1:8070"

# 事故當時的實測值,拿來當對照組（見 PR #28）
RSS_FRESH_MB = 116      # 修好後剛重啟
RSS_LEAKY_2D_MB = 1060  # 舊版跑兩天
RSS_WARN_MB = 500       # 超過這條就該懷疑還有第二個洩漏源

G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; B = "\033[1m"; D = "\033[2m"; N = "\033[0m"
if not sys.stdout.isatty():
    G = Y = R = B = D = N = ""

OK, WARN, BAD = f"{G}✓{N}", f"{Y}!{N}", f"{R}✗{N}"


def data_dir() -> Path:
    """跟 src/config.py 同樣的規則：新目錄優先,否則沿用舊的。"""
    new, old = Path.home() / ".gemini-web", Path.home() / ".gemini-image"
    return new if new.exists() else (old if old.exists() else new)


def sh(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def rss_mb(pid: str | int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def head(title: str) -> None:
    print(f"\n{B}{title}{N}")


def section_service() -> tuple[str, float]:
    """回傳 (main_pid, uptime_days)。"""
    head("服務")
    props = dict(
        line.split("=", 1)
        for line in sh("systemctl", "show", SERVICE,
                       "-p", "MainPID", "-p", "NRestarts",
                       "-p", "ActiveEnterTimestamp", "-p", "ActiveState").splitlines()
        if "=" in line
    )
    pid = props.get("MainPID", "0")
    state = props.get("ActiveState", "unknown")
    started_raw = props.get("ActiveEnterTimestamp", "")
    restarts = props.get("NRestarts", "0")

    days = 0.0
    started_txt = started_raw or "?"
    m = re.search(r"(\w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", started_raw)
    if m:
        started = datetime.strptime(m.group(1)[4:], "%Y-%m-%d %H:%M:%S")
        days = (datetime.now() - started).total_seconds() / 86400
        started_txt = f"{m.group(1)[4:]}（{days:.1f} 天前）"

    mark = OK if state == "active" else BAD
    print(f"  {mark} 狀態 {state}   啟動 {started_txt}")
    if restarts not in ("0", ""):
        print(f"  {WARN} systemd 重啟過 {restarts} 次 {D}(NRestarts；oom-kill 也算在內){N}")
    return pid, days


def section_memory(pid: str, days: float) -> None:
    head("記憶體 — 洩漏有沒有回來")
    py = rss_mb(pid)
    if not py:
        print(f"  {BAD} 讀不到主進程 RSS（pid={pid}）")
        return

    if py >= RSS_WARN_MB:
        mark, note = BAD, "太高了,可能還有第二個洩漏源 → 上 tracemalloc 抓"
    elif py >= RSS_WARN_MB * 0.6:
        mark, note = WARN, "偏高,再觀察"
    else:
        mark, note = OK, "正常"
    print(f"  {mark} Python 主進程 {B}{py:.0f} MB{N}   {D}{note}{N}")

    if days >= 0.5:
        leaky = RSS_FRESH_MB + (RSS_LEAKY_2D_MB - RSS_FRESH_MB) * min(days, 2) / 2
        print(f"    {D}對照：舊版跑 {min(days, 2.0):.1f} 天約 {leaky:.0f} MB"
              f"（起點 {RSS_FRESH_MB} MB）{N}")
    else:
        print(f"    {D}才剛重啟不久,數字要跑滿一兩天才有意義（起點約 {RSS_FRESH_MB} MB）{N}")

    cg = sh("systemctl", "show", SERVICE, "-p", "MemoryCurrent", "--value")
    if cg.isdigit():
        print(f"    cgroup 總計 {int(cg) / 1024**3:.2f} GB"
              f" {D}(含 4 個 chrome + 4 個 playwright driver){N}")

    for label, pat in (("chrome renderer", "chrome-headless-shell --type=renderer"),
                       ("playwright driver", "playwright/driver/node")):
        pids = [p for p in sh("pgrep", "-f", pat).split() if rss_mb(p) > 10]
        if pids:
            tot = sum(rss_mb(p) for p in pids)
            print(f"    {label} ×{len(pids)}：{tot:.0f} MB")


def section_workers() -> None:
    head("Worker")
    try:
        with urllib.request.urlopen(f"{API}/api/health", timeout=20) as r:
            h = json.load(r)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        print(f"  {BAD} 打不到 {API}/api/health：{e}")
        return

    ws = h.get("workers", [])
    bad = [w for w in ws if not (w.get("alive") and w.get("logged_in") and w.get("ready"))]
    mark = OK if not bad else BAD
    print(f"  {mark} status={h.get('status')}   "
          f"{len(ws) - len(bad)}/{len(ws)} 正常   "
          f"佇列等待 {h.get('queue_waiting')}")
    for w in ws:
        flags = "".join(c if w.get(k) else "-" for k, c in
                        (("alive", "A"), ("logged_in", "L"), ("ready", "R")))
        busy = " busy" if w.get("busy") else ""
        m = OK if flags == "ALR" else BAD
        print(f"    {m} w{w.get('id')} [{flags}]{busy}")
    print(f"    {D}A=活著 L=已登入 R=頁面就緒。注意：三個全綠也不保證真的會回應"
          f"——08-05 就是全綠但全滅。要看下面的成功率。{N}")


def section_requests() -> None:
    head("近 7 天請求（唯一能證明它真的在工作的指標）")
    db = data_dir() / "admin.db"
    if not db.exists():
        print(f"  {WARN} 找不到 {db}")
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select date(created_at) d,"
            "       sum(status='succeeded') ok,"
            "       sum(status='failed') ng "
            "from requests where created_at > datetime('now','-7 days') "
            "group by d order by d"
        ).fetchall()
        fails = con.execute(
            "select created_at, kind, api_key_name, worker_id, "
            "       round(duration_seconds,1) secs, substr(error,1,60) err "
            "from requests where status='failed' "
            "order by created_at desc limit 5"
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  {BAD} 讀 admin.db 失敗：{e}")
        return

    if not rows:
        print(f"  {D}近 7 天沒有任何請求{N}")
    for r in rows:
        ok, ng = r["ok"] or 0, r["ng"] or 0
        total = ok + ng
        rate = ok / total * 100 if total else 0
        mark = OK if rate >= 95 else (WARN if rate >= 80 else BAD)
        bar = "█" * round(rate / 5) + "·" * (20 - round(rate / 5))
        print(f"  {mark} {r['d']}  {bar} {rate:5.1f}%   成功 {ok:3}  失敗 {ng:3}")

    if fails:
        print(f"\n  {D}最近 5 筆失敗（時間是 UTC，+8 才是本地）：{N}")
        for f in fails:
            print(f"    {f['created_at'][:19]}  w{f['worker_id']}  {f['kind']:8}"
                  f"  {f['secs'] or 0:6}s  key={f['api_key_name'] or '-'}"
                  f"  {(f['err'] or '(無錯誤訊息)')}")


def section_selfheal() -> None:
    head("自癒 — 有沒有一直在救火")
    log = sh("journalctl", "-u", SERVICE, "--no-pager", "--since", "3 days ago", "-o", "short-iso")
    if not log:
        print(f"  {D}讀不到 journal（權限不足？）{N}")
    else:
        pats = {
            # 兩種寫法都留:0903 之前的 journal 記的是舊的「no_response」字樣。
            "連續失敗 → 重啟瀏覽器": r"次 ?(沒拿到結果|no_response),重啟瀏覽器",
            "連續失敗（尚未到重啟門檻）": r"(沒拿到結果|no_response)\(連續",
            "頁面未就緒 → 重置對話": r"頁面未就緒",
            "重啟瀏覽器失敗": r"瀏覽器重啟失敗",
        }
        hit = False
        for label, pat in pats.items():
            n = len(re.findall(pat, log))
            if n:
                hit = True
                print(f"  {WARN} {label}：近 3 天 {n} 次")
        if not hit:
            print(f"  {OK} 近 3 天沒有觸發過自癒")
        print(f"    {D}偶爾觸發是正常的（那正是它該做的事）；每天都在觸發就代表"
              f"底下還有沒修的問題。{N}")

    diag = data_dir() / "diagnostics"
    shots = sorted(diag.glob("*no-response*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    recent = [p for p in shots
              if datetime.fromtimestamp(p.stat().st_mtime) > datetime.now() - timedelta(days=3)]
    if recent:
        print(f"\n  {WARN} 近 3 天有 {len(recent)} 張 no-response 現場截圖，最新："
              f"\n    {recent[0]}"
              f"\n    {D}打開來看卡在哪（登入頁？錯誤訊息？空白？）——"
              f"這正是 08-05 當時最缺的東西。{N}")


def main() -> None:
    print(f"{B}gemini-web 健檢{N}  {datetime.now():%Y-%m-%d %H:%M:%S}")
    pid, days = section_service()
    if pid and pid != "0":
        section_memory(pid, days)
    section_workers()
    section_requests()
    section_selfheal()
    print(f"\n{D}詳情見 PR #28。真的壞掉時：sudo systemctl restart {SERVICE}{N}")


if __name__ == "__main__":
    main()
