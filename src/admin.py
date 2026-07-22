"""Admin webui — 比照 codex-image-service 的頁面結構與登入方式，同一套操作習慣。

差異（因為 gemini-web 本身的資料模型比 codex-image-service 簡單）：
- API Keys 頁是唯讀（金鑰是 .env 裡的靜態集合，不是資料庫動態發放），要換金鑰改 .env 重啟。
- Test 頁直接同步呼叫 worker_pool.dispatch，圖片當場內嵌顯示（不落地存檔，本服務本來就不存生成的圖）。
- History 頁記錄輕量 sqlite log（見 history_db.py），只留最近 500 筆，非完整稽核軌跡。
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import history_db
from .config import settings
from .security import constant_equals, create_admin_session, verify_admin_session

router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
#
# worker_pool / _dispatch_and_log / _start_time live in .main, which imports
# this module's router at module scope — importing `.main` back at module
# scope here would be circular, so those three are always pulled in lazily
# (`from . import main as _main`) inside the functions that need them.
# ---------------------------------------------------------------------------

def _prefix(request: Request) -> str:
    return settings.admin_url_prefix or ""


def _url(request: Request, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return _prefix(request) + path


def _admin_user(request: Request) -> str | None:
    username = verify_admin_session(
        request.cookies.get("admin_session"),
        settings.admin_session_secret,
    )
    if username != settings.admin_username:
        return None
    return username


def _redirect_login(request: Request) -> RedirectResponse:
    return RedirectResponse(_url(request, "/admin/login"), status_code=303)


# ---------------------------------------------------------------------------
# auth routes
# ---------------------------------------------------------------------------

@router.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    return RedirectResponse(_url(request, "/admin"), status_code=303)


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    if _admin_user(request):
        return RedirectResponse(_url(request, "/admin"), status_code=303)
    return HTMLResponse(_login_layout(_login_form(_prefix(request))))


@router.post("/admin/login", include_in_schema=False)
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not (
        constant_equals(username, settings.admin_username)
        and constant_equals(password, settings.admin_password)
    ):
        return HTMLResponse(
            _login_layout(
                _login_form(_prefix(request), error="Invalid username or password")
            ),
            status_code=401,
        )

    remember = str(form.get("remember", "")) == "on"
    ttl_seconds = 30 * 86400 if remember else 86400

    response = RedirectResponse(_url(request, "/admin"), status_code=303)
    response.set_cookie(
        "admin_session",
        create_admin_session(username, settings.admin_session_secret, ttl_seconds=ttl_seconds),
        httponly=True,
        samesite="lax",
        max_age=ttl_seconds,
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(_url(request, "/admin/login"), status_code=303)
    response.delete_cookie("admin_session")
    return response


# ---------------------------------------------------------------------------
# page routes (GET)
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(await _overview_page(request))


@router.get("/admin/keys", response_class=HTMLResponse, include_in_schema=False)
async def keys_page(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(_keys_page(_prefix(request)))


@router.get("/admin/test", response_class=HTMLResponse, include_in_schema=False)
async def test_page(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(_test_page(_prefix(request)))


@router.get("/admin/requests", response_class=HTMLResponse, include_in_schema=False)
async def requests_page(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(_requests_page(_prefix(request)))


# ---------------------------------------------------------------------------
# mutation routes (POST)
# ---------------------------------------------------------------------------

@router.post("/admin/test-generate", response_class=HTMLResponse, include_in_schema=False)
async def admin_test_generate(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    from . import main as _main  # deferred: avoids circular import (see top of file)

    form = await request.form()
    kind = str(form.get("kind", "generate")).strip() or "generate"
    prompt = str(form.get("prompt", "")).strip()
    try:
        timeout = int(str(form.get("timeout", str(settings.default_timeout))).strip())
    except ValueError:
        timeout = settings.default_timeout

    notice: str | None = None
    error: str | None = None
    image_html = ""
    if not prompt:
        error = "Prompt is required."
    else:
        start = time.time()
        try:
            # _dispatch_and_log already writes the history row (success or
            # failure) — no need to log again here.
            result = await _main._dispatch_and_log(kind, prompt, "", timeout)
        except Exception as exc:  # queue full / timeout / worker error
            error = f"Request failed: {exc}"
        else:
            duration = time.time() - start
            if result.get("success"):
                notice = f"Done in {duration:.1f}s."
                for img in result.get("images", []) or []:
                    src = img if img.startswith("data:") else f"data:image/png;base64,{img}"
                    image_html += f"<img class='test-result-img' src='{src}'>"
                if result.get("text"):
                    image_html += f"<pre>{html.escape(result['text'])}</pre>"
            else:
                error = str(result.get("message") or result.get("error") or "Generation failed.")

    return HTMLResponse(
        _test_page(_prefix(request), notice=notice, error=error, image_html=image_html)
    )


# ---------------------------------------------------------------------------
# page renderers
# ---------------------------------------------------------------------------

async def _overview_page(request: Request) -> str:
    from . import main as _main  # deferred: avoids circular import (see top of file)

    prefix = _prefix(request)
    stats = history_db.stats()
    recent = history_db.list_recent(10)
    worker_statuses = await _main.worker_pool.worker_status()
    uptime = round(time.time() - _main._start_time)
    body = f"""
      <div class="page-head">
        <h2>Overview</h2>
        <p class="page-sub">Worker health, queue depth, and recent activity.</p>
      </div>
      <section class="stats">
        <div><strong>{stats['total']}</strong><span>Requests logged</span></div>
        <div><strong>{stats['succeeded']}</strong><span>Succeeded</span></div>
        <div><strong>{stats['failed']}</strong><span>Failed</span></div>
        <div><strong>{_format_uptime(uptime)}</strong><span>Uptime</span></div>
      </section>
      <section>
        <h2>Workers</h2>
        {_worker_table(worker_statuses)}
      </section>
      <section>
        <div class="section-title">
          <h2>Recent activity</h2>
          <a class="link" href="{prefix}/admin/requests">View all →</a>
        </div>
        {_activity_feed(recent, prefix)}
      </section>
    """
    return _shell("Overview", "overview", prefix, body)


def _keys_page(prefix: str) -> str:
    keys = sorted(settings.api_keys)
    body = f"""
      <div class="page-head">
        <h2>API Keys</h2>
        <p class="page-sub">Static keys from the <code>API_KEYS</code> env var — read-only here. Edit <code>.env</code> and restart the service to add, remove, or rotate a key.</p>
      </div>
      <section>
        {_keys_table(keys)}
      </section>
    """
    return _shell("API Keys", "keys", prefix, body)


def _test_page(
    prefix: str,
    notice: str | None = None,
    error: str | None = None,
    image_html: str = "",
) -> str:
    notice_html = f"<div class='notice'>{notice}</div>" if notice else ""
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    result_html = f"<div class='test-result'>{image_html}</div>" if image_html else ""
    body = f"""
      <div class="page-head">
        <h2>Test generation</h2>
        <p class="page-sub">Runs the same worker pool a real caller hits — the image renders here, nothing is saved to disk.</p>
      </div>
      {notice_html}
      {error_html}
      <section>
        <form method="post" action="{prefix}/admin/test-generate" class="form-grid">
          <label>Kind
            <select name="kind">
              <option value="generate" selected>generate (image)</option>
              <option value="chat">chat (text)</option>
            </select>
          </label>
          <label>Prompt
            <textarea name="prompt" rows="3" required placeholder="A minimalist orange tabby cat clock face on white"></textarea>
          </label>
          <label>Timeout (seconds)
            <input name="timeout" type="number" min="10" max="600" value="{settings.default_timeout}">
          </label>
          <div>
            <button type="submit">Run</button>
          </div>
        </form>
        {result_html}
      </section>
    """
    return _shell("Test", "test", prefix, body)


def _requests_page(prefix: str) -> str:
    requests = history_db.list_recent(200)
    body = f"""
      <div class="page-head">
        <h2>History</h2>
        <p class="page-sub">Last 500 requests (rolling log — prompts only, no images stored).</p>
      </div>
      <section>
        {_requests_table(requests)}
      </section>
    """
    return _shell("History", "requests", prefix, body)


# ---------------------------------------------------------------------------
# component helpers
# ---------------------------------------------------------------------------

def _format_uptime(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _status_chip(status: str) -> str:
    cls = {"succeeded": "chip chip-ok", "failed": "chip chip-fail"}.get(status, "chip chip-mute")
    return f"<span class='{cls}'>{html.escape(status)}</span>"


def _relative_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return html.escape(iso)
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (now - ts).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _worker_table(statuses: list[dict[str, Any]]) -> str:
    rows = []
    for s in statuses:
        alive = "<span class='chip chip-ok'>alive</span>" if s["alive"] else "<span class='chip chip-fail'>down</span>"
        logged_in = "<span class='chip chip-ok'>yes</span>" if s["logged_in"] else "<span class='chip chip-fail'>no</span>"
        busy = "<span class='chip chip-run'>busy</span>" if s["busy"] else "<span class='chip chip-mute'>idle</span>"
        rows.append(f"<tr><td>{s['id']}</td><td>{alive}</td><td>{logged_in}</td><td>{busy}</td></tr>")
    if not rows:
        rows.append("<tr><td colspan='4' class='empty'>No workers.</td></tr>")
    return (
        "<table><thead><tr><th>ID</th><th>Alive</th><th>Logged in</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _activity_feed(requests: list[dict[str, Any]], prefix: str) -> str:
    if not requests:
        return "<p class='muted'>No requests logged yet. Try <a href='" + prefix + "/admin/test'>Test</a>.</p>"
    items = []
    for item in requests:
        when = _relative_time(item.get("created_at"))
        prompt_short = item.get("prompt") or ""
        if len(prompt_short) > 80:
            prompt_short = prompt_short[:77] + "..."
        items.append(
            "<li class='activity-item'>"
            f"<div class='activity-row'>{_status_chip(item['status'])}"
            f"<code class='activity-id'>{html.escape(item['kind'])}</code>"
            f"<span class='activity-time'>{when}</span></div>"
            f"<div class='activity-meta'>{html.escape(prompt_short)}</div>"
            "</li>"
        )
    return "<ul class='activity'>" + "".join(items) + "</ul>"


def _keys_table(keys: list[str]) -> str:
    rows = []
    for key in keys:
        masked = key[:4] + "…" + key[-4:] if len(key) > 8 else "…"
        rows.append(f"<tr><td><code>{html.escape(masked)}</code></td></tr>")
    if not rows:
        rows.append(
            "<tr><td class='empty'>No keys configured — API_KEYS is empty, "
            "so the official-API fallback and Google GenAI compat endpoints are unauthenticated.</td></tr>"
        )
    return f"<table><thead><tr><th>Key (masked)</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _requests_table(requests: list[dict[str, Any]]) -> str:
    rows = []
    for item in requests:
        error = item.get("error") or ""
        if len(error) > 180:
            error = error[:177] + "..."
        duration = item.get("duration_seconds")
        duration_str = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "—"
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['kind'])}</td>"
            f"<td>{_status_chip(item['status'])}</td>"
            f"<td>{html.escape(item.get('via') or '—')}</td>"
            f"<td>{duration_str}</td>"
            f"<td>{_relative_time(item['created_at'])}</td>"
            f"<td><details><summary>Prompt</summary><pre>{html.escape(item['prompt'])}</pre></details></td>"
            f"<td><details><summary>Error</summary><pre>{html.escape(error) or '—'}</pre></details></td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='7' class='empty'>No requests logged yet.</td></tr>")
    return (
        "<table><thead><tr><th>Kind</th><th>Status</th><th>Via</th><th>Duration</th>"
        "<th>Created</th><th>Prompt</th><th>Error</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _login_form(prefix: str, error: str | None = None) -> str:
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    return f"""
    <div class="login">
      <div class="login-brand">
        <span class="brand-mark brand-mark-lg">✧</span>
        <div class="login-brand-text">
          <div class="login-brand-name">Gemini Web Service</div>
          <div class="login-brand-sub">Admin sign-in</div>
        </div>
      </div>
      {error_html}
      <form method="post" action="{prefix}/admin/login">
        <label>Username<input name="username" autocomplete="username" required></label>
        <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
        <label class="remember-row"><input type="checkbox" name="remember"> Remember me for 30 days</label>
        <button type="submit" style="width: 100%">Sign in</button>
      </form>
    </div>
    """


# ---------------------------------------------------------------------------
# layouts (shell with sidebar; minimal login shell)
# ---------------------------------------------------------------------------

NAV_ITEMS = [
    ("overview", "Overview",   "⌂", "/admin"),
    ("keys",     "API Keys",   "⌘", "/admin/keys"),
    ("test",     "Test",       "▸", "/admin/test"),
    ("requests", "History",    "☰", "/admin/requests"),
]


def _sidebar(current_nav: str, prefix: str) -> str:
    items = []
    for slug, label, ico, path in NAV_ITEMS:
        active = " active" if slug == current_nav else ""
        items.append(
            f"<a class='nav-item{active}' href='{prefix}{path}'>"
            f"<span class='nav-ico'>{ico}</span><span>{label}</span>"
            "</a>"
        )
    return "<aside class='sidebar'>" f"<nav class='nav'>{''.join(items)}</nav>" "</aside>"


def _shell(title: str, current_nav: str, prefix: str, body: str) -> str:
    return _base_layout(
        title,
        f"""
        <header class='topbar'>
          <div class='brand'>
            <span class='brand-mark'>✧</span>
            <span class='brand-name'>Gemini Web Service</span>
          </div>
          <div class='topbar-actions'>
            <span class='user-chip'>admin</span>
            <form method='post' action='{prefix}/admin/logout'>
              <button class='ghost' type='submit'>Logout</button>
            </form>
          </div>
        </header>
        <div class='layout'>
          {_sidebar(current_nav, prefix)}
          <main class='content'>{body}</main>
        </div>
        """,
    )


def _login_layout(body: str) -> str:
    return _base_layout("Admin Login", body)


def _base_layout(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{html.escape(title)}</title>
      <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23a5b4fc'/%3E%3Ctext x='50%25' y='58%25' text-anchor='middle' font-size='34' fill='white' font-family='ui-sans-serif,system-ui'%3E%E2%9C%A7%3C/text%3E%3C/svg%3E">
      <style>{_STYLES}</style>
    </head>
    <body>{body}</body>
    </html>
    """


_STYLES = """
  :root {
    color-scheme: light;
    --ink: #2d3142;
    --ink-soft: #4a5072;
    --muted: #7d839b;
    --bg-1: #f7f8fd;
    --bg-2: #eef2ff;
    --bg-3: #f0fdfa;
    --accent-1: #818cf8;
    --accent-2: #a5b4fc;
    --accent-3: #86efac;
    --accent-4: #fcd34d;
    --card: #ffffffcc;
    --card-edge: #e3e6f5;
    --danger: #e11d48;
    --shadow: 0 6px 30px -8px rgba(60,70,140,.16);
    --shadow-sm: 0 2px 8px -2px rgba(60,70,140,.10);
    --radius: 18px;
    --sidebar-w: 232px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font: 15px/1.6 "Noto Sans TC", Inter, ui-sans-serif, system-ui, "PingFang TC", "Helvetica Neue", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(ellipse 1200px 600px at 10% -10%, var(--bg-2) 0%, transparent 60%),
      radial-gradient(ellipse 1100px 500px at 95% 5%, var(--bg-3) 0%, transparent 55%),
      var(--bg-1);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  h2 { font-size: 18px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 16px; color: var(--ink); }
  p { margin: 0 0 12px; color: var(--ink-soft); }
  p.muted { color: var(--muted); }
  a { color: var(--accent-1); text-decoration: none; }
  a:hover { text-decoration: underline; }
  a.link { font-size: 13.5px; font-weight: 500; }

  .topbar {
    position: sticky; top: 0; z-index: 5;
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 24px;
    background: #ffffffd8;
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--card-edge);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-mark {
    width: 30px; height: 30px; border-radius: 9px;
    background: var(--accent-1); color: white;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 17px;
  }
  .brand-name { font-weight: 700; font-size: 16px; letter-spacing: -0.01em; }
  .topbar-actions { display: flex; align-items: center; gap: 12px; }
  .user-chip {
    padding: 6px 12px; border-radius: 999px;
    background: #eef1ff; color: var(--ink-soft);
    font-size: 13px; font-weight: 500;
  }

  .layout {
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr;
    gap: 0;
    min-height: calc(100vh - 60px);
  }
  .sidebar {
    border-right: 1px solid var(--card-edge);
    background: #ffffff9c;
    backdrop-filter: blur(10px);
    padding: 20px 14px;
  }
  .nav { display: flex; flex-direction: column; gap: 4px; }
  .nav-item {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 14px; border-radius: 12px;
    color: var(--ink-soft);
    font-size: 14px; font-weight: 500;
    text-decoration: none;
    transition: background .12s ease, color .12s ease;
    border-left: 3px solid transparent;
  }
  .nav-item:hover { background: #f5f6ff; color: var(--ink); text-decoration: none; }
  .nav-item.active {
    background: #e8eaff;
    color: var(--ink);
    border-left-color: var(--accent-1);
    font-weight: 600;
  }
  .nav-ico {
    width: 22px; height: 22px; border-radius: 7px;
    background: #eef1ff; color: var(--accent-1);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 13px;
  }
  .nav-item.active .nav-ico { background: var(--accent-1); color: white; }

  .content { padding: 32px 36px 64px; max-width: 1180px; }
  .page-head { margin-bottom: 24px; }
  .page-head h2 { font-size: 26px; margin: 0 0 6px; }
  .page-sub { margin: 0; color: var(--muted); font-size: 14.5px; }

  section {
    background: var(--card);
    backdrop-filter: blur(8px);
    border: 1px solid var(--card-edge);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 24px 26px;
    margin: 0 0 22px;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
    background: transparent;
    border: 0;
    backdrop-filter: none;
    box-shadow: none;
    padding: 0;
    margin: 0 0 22px;
  }
  .stats > div {
    background: white;
    border: 1px solid var(--card-edge);
    border-radius: 14px;
    padding: 18px 20px;
    box-shadow: var(--shadow-sm);
  }
  .stats strong { display: block; font-size: 30px; font-weight: 700; color: var(--ink); line-height: 1.1; }
  .stats span { color: var(--muted); font-size: 13px; }

  .section-title { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
  .section-title h2 { margin: 0; }

  .form-grid { display: grid; gap: 14px; max-width: 720px; }

  input, select, textarea {
    width: 100%;
    padding: 11px 14px;
    border: 1px solid var(--card-edge);
    border-radius: 10px;
    font: inherit;
    color: var(--ink);
    background: white;
    transition: border-color .12s ease, box-shadow .12s ease;
  }
  input:focus, select:focus, textarea:focus {
    outline: none;
    border-color: var(--accent-1);
    box-shadow: 0 0 0 3px #818cf833;
  }
  textarea { resize: vertical; min-height: 80px; font-family: inherit; }
  label {
    display: grid; gap: 6px;
    margin: 0;
    font-size: 13px;
    color: var(--ink-soft);
    font-weight: 500;
  }

  button {
    padding: 10px 22px;
    border: 0; border-radius: 999px;
    background: var(--accent-1); color: white;
    font: inherit; font-size: 14px; font-weight: 600;
    cursor: pointer; white-space: nowrap;
    box-shadow: var(--shadow-sm);
    transition: transform .12s ease, filter .12s ease;
  }
  button:hover { transform: translateY(-1px); }
  button:focus-visible { outline: 2px solid var(--accent-2); outline-offset: 2px; }
  button.ghost {
    background: white; color: var(--ink-soft);
    border: 1px solid var(--card-edge); box-shadow: none;
  }
  button.ghost:hover { background: #f5f6ff; }

  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th, td {
    padding: 12px 12px;
    border-bottom: 1px solid #e9ebf7;
    text-align: left; vertical-align: top;
  }
  th {
    color: var(--muted); font-weight: 600;
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  tr:last-child td { border-bottom: 0; }
  tbody tr:hover { background: #f8f9ff; }
  td.empty { text-align: center; color: var(--muted); padding: 32px 12px; }

  .chip {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px; font-weight: 600;
    letter-spacing: .01em;
  }
  .chip-ok    { background: #d1fae5; color: #047857; }
  .chip-run   { background: #dbeafe; color: #1d4ed8; }
  .chip-queue { background: #fef3c7; color: #b45309; }
  .chip-fail  { background: #fee2e2; color: #b91c1c; }
  .chip-mute  { background: #f1f5f9; color: #64748b; }

  .activity { list-style: none; padding: 0; margin: 0; }
  .activity-item {
    padding: 12px 14px;
    border-bottom: 1px solid #e9ebf7;
  }
  .activity-item:last-child { border-bottom: 0; }
  .activity-row { display: flex; align-items: center; gap: 10px; }
  .activity-id { font-size: 12.5px; }
  .activity-time { color: var(--muted); font-size: 12.5px; margin-left: auto; }
  .activity-meta { color: var(--muted); font-size: 13px; margin-top: 4px; }

  code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
  code {
    background: #eef1ff; color: var(--ink);
    padding: 2px 7px; border-radius: 6px;
    font-size: 12.5px;
  }
  pre {
    white-space: pre-wrap;
    max-width: 420px;
    background: #f8f9ff;
    color: var(--ink);
    border: 1px solid var(--card-edge);
    padding: 10px 12px;
    border-radius: 10px;
    font-size: 12.5px;
    margin: 0;
    line-height: 1.55;
  }
  details { margin: 0; }
  details summary {
    cursor: pointer; color: var(--ink-soft);
    font-weight: 500; font-size: 13px;
    list-style: none;
  }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "▸ "; color: var(--muted); }
  details[open] summary::before { content: "▾ "; }
  details[open] summary { margin-bottom: 8px; }

  .notice {
    margin: 0 0 18px;
    padding: 14px 18px;
    background: var(--card);
    border: 1px solid var(--card-edge);
    border-radius: 14px;
    box-shadow: var(--shadow-sm);
    color: var(--ink);
  }
  .error {
    margin: 0 0 18px;
    padding: 14px 18px;
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 14px;
    color: #b91c1c;
    font-size: 14px; font-weight: 500;
  }

  .test-result { margin-top: 20px; }
  .test-result-img {
    max-width: 100%; border-radius: 14px;
    border: 1px solid var(--card-edge);
    box-shadow: var(--shadow-sm);
    margin-bottom: 12px;
    display: block;
  }

  .login {
    max-width: 420px;
    margin: 100px auto;
    padding: 36px 32px;
    background: var(--card);
    backdrop-filter: blur(8px);
    border: 1px solid var(--card-edge);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
  }
  .login label { margin-bottom: 14px; }
  .login label.remember-row {
    display: flex; flex-direction: row; align-items: center; gap: 8px;
    font-size: 13.5px; color: var(--ink-soft); font-weight: 500;
  }
  .login label.remember-row input { width: auto; }
  .login-brand {
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 26px;
  }
  .brand-mark-lg {
    width: 44px; height: 44px; border-radius: 12px;
    font-size: 24px;
  }
  .login-brand-text { display: flex; flex-direction: column; }
  .login-brand-name {
    font-size: 17px; font-weight: 700; color: var(--ink);
    letter-spacing: -0.01em;
  }
  .login-brand-sub {
    font-size: 13px; color: var(--muted);
  }

  @media (max-width: 900px) {
    .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 760px) {
    .layout { grid-template-columns: 1fr; }
    .sidebar { border-right: 0; border-bottom: 1px solid var(--card-edge); padding: 12px; }
    .nav { flex-direction: row; overflow-x: auto; gap: 6px; }
    .nav-item { border-left: 0; border-bottom: 3px solid transparent; }
    .nav-item.active { border-left: 0; border-bottom-color: var(--accent-1); }
    .content { padding: 24px 20px 48px; }
    table { display: block; overflow-x: auto; }
  }
"""
