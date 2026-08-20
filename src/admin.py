"""Admin webui — 比照 codex-image-service 的頁面結構與登入方式，同一套操作習慣。

- API Keys 頁可以現場發新 key/停用/刪除（admin_db 的 api_keys 表），.env 的
  API_KEYS 靜態集合仍然有效，顯示在頁面下半當唯讀區塊。
- Test 頁直接同步呼叫 worker_pool.dispatch，支援 generate/edit/chat 三種
  kind（edit 要另外上傳一張參考圖），圖片當場內嵌顯示，也跟真實流量一樣落地
  存檔、寫進 History。
- History 頁記錄輕量 sqlite log（見 admin_db.py），只留最近 500 筆，非完整稽核
  軌跡；每筆記錄的圖片存在 GENERATED_DIR，靠 image_store.sweep_old 定期清。
"""
from __future__ import annotations

import base64
import html
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import admin_db, image_store
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
    # Any session cookie set before the Path scoping fix landed defaulted to
    # Path=/ (root) — it still lives in returning browsers and coexists with
    # the new Path-scoped one (different Path = different cookie in the jar),
    # so the browser sends both and whichever one the server happens to read
    # can be the stale/wrong one. Clear the old root-path cookie explicitly
    # so only the correctly-scoped one survives.
    response.delete_cookie("admin_session", path="/")
    response.set_cookie(
        "admin_session",
        create_admin_session(username, settings.admin_session_secret, ttl_seconds=ttl_seconds),
        httponly=True,
        samesite="lax",
        max_age=ttl_seconds,
        # Without an explicit path, Starlette defaults to "/" — behind the
        # shared ching-tech.ddns.net domain that collides with any other
        # admin webui on the same host (e.g. codex-image-service's), since
        # both use the same cookie name. Scope it to this service's own prefix.
        path=_prefix(request) or "/",
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(_url(request, "/admin/login"), status_code=303)
    response.delete_cookie("admin_session", path=_prefix(request) or "/")
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
    # One-shot reveal: read+immediately clear the flash cookie the create-key
    # POST set, so a page refresh never shows the raw key a second time.
    new_key = request.cookies.get("_new_api_key")
    body = _keys_page(_prefix(request), new_api_key=new_key)
    response = HTMLResponse(body)
    if new_key:
        response.delete_cookie("_new_api_key", path=_url(request, "/admin/keys"))
    return response


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


@router.post("/admin/cleanup", response_class=HTMLResponse, include_in_schema=False)
async def cleanup(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    deleted = image_store.sweep_old(settings.image_retention_days)
    return HTMLResponse(_requests_page(_prefix(request), cleanup_result=deleted))


@router.post("/admin/dispatch-mode", include_in_schema=False)
async def set_dispatch_mode(request: Request) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    from . import main as _main  # deferred: circular import

    form = await request.form()
    mode = str(form.get("mode") or "")
    _main.worker_pool.set_mode(mode)          # 立即生效（跑中不用重啟）
    admin_db.set_setting("dispatch_mode", _main.worker_pool.mode)  # 存檔，重啟後續用
    return RedirectResponse(_url(request, "/admin"), status_code=303)


@router.post("/admin/requests/{request_id}/delete", include_in_schema=False)
async def delete_request(request: Request, request_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    row = admin_db.get_request(request_id)
    if row:
        image_store.delete_files(row.get("image_paths") or [])
        admin_db.delete_request(request_id)
    return RedirectResponse(_url(request, "/admin/requests"), status_code=303)


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
    api_key_choice = str(form.get("api_key_choice", "")).strip()
    try:
        timeout = int(str(form.get("timeout", str(settings.default_timeout))).strip())
    except ValueError:
        timeout = settings.default_timeout

    # Resolve the "attribute to" choice into the api_key_name that gets
    # written to History — mirrors what _identify_caller would do for a real
    # request, but picked explicitly instead of parsed off headers, and
    # bumps that key's usage stats same as a real call would.
    api_key_name = ""
    if api_key_choice == "static":
        api_key_name = "static"
    elif api_key_choice:
        row = next((k for k in admin_db.list_api_keys() if k["id"] == api_key_choice), None)
        if row:
            admin_db.mark_api_key_used(row["id"])
            api_key_name = row["name"]

    notice: str | None = None
    error: str | None = None
    image_html = ""
    extra: dict | None = None

    if kind == "edit":
        upload = form.get("reference_image")
        if upload is None or not getattr(upload, "filename", ""):
            error = "Edit mode needs a reference image upload."
        else:
            raw = await upload.read()
            mime = upload.content_type or "image/png"
            b64 = base64.b64encode(raw).decode("ascii")
            extra = {"reference_image": f"data:{mime};base64,{b64}"}

    if not prompt:
        error = "Prompt is required."

    if not error:
        start = time.time()
        try:
            # _dispatch_and_log already writes the history row (success or
            # failure) — no need to log again here.
            result = await _main._dispatch_and_log(
                kind, prompt, "", timeout, extra=extra, api_key_name=api_key_name
            )
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


@router.post("/admin/api-keys", include_in_schema=False)
async def create_api_key(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    form = await request.form()
    name = str(form.get("name", ""))
    _, raw_key = admin_db.create_api_key(name)
    # PRG: stash the raw key in a short-lived flash cookie, then redirect.
    # The GET handler reads it once and immediately clears the cookie.
    response = RedirectResponse(_url(request, "/admin/keys"), status_code=303)
    response.set_cookie(
        "_new_api_key",
        raw_key,
        httponly=True,
        samesite="lax",
        max_age=60,
        path=_url(request, "/admin/keys"),
    )
    return response


@router.post("/admin/api-keys/{key_id}/disable", include_in_schema=False)
async def disable_api_key(request: Request, key_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    admin_db.disable_api_key(key_id)
    return RedirectResponse(_url(request, "/admin/keys"), status_code=303)


@router.post("/admin/api-keys/{key_id}/delete", include_in_schema=False)
async def delete_api_key(request: Request, key_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    admin_db.delete_api_key(key_id)
    return RedirectResponse(_url(request, "/admin/keys"), status_code=303)


# ---------------------------------------------------------------------------
# page renderers
# ---------------------------------------------------------------------------

async def _overview_page(request: Request) -> str:
    from . import main as _main  # deferred: avoids circular import (see top of file)

    prefix = _prefix(request)
    stats = admin_db.stats()
    recent = admin_db.list_recent(10)
    worker_statuses = await _main.worker_pool.worker_status(include_account=True)
    mode = _main.worker_pool.mode
    uptime = round(time.time() - _main._start_time)
    # 排隊中 + 正在跑。以前這個數字只在 /api/health 的 JSON 裡，儀表板看不到，
    # 但「是不是塞車」正是看儀表板的人第一個想知道的事（codex-image-service
    # 那邊早就有這格）。
    queued = _main.worker_pool.waiting_count + sum(1 for w in worker_statuses if w["busy"])
    body = f"""
      <div class="page-head">
        <h2>Overview</h2>
        <p class="page-sub">Worker health, queue depth, and recent activity.</p>
      </div>
      <section class="stats">
        <div><strong>{stats['total']}</strong><span>Requests logged</span></div>
        <div><strong>{stats['succeeded']}</strong><span>Succeeded</span></div>
        <div><strong>{stats['failed']}</strong><span>Failed</span></div>
        <div><strong>{queued}</strong><span>Queued / running</span></div>
        <div><strong>{_format_uptime(uptime)}</strong><span>Uptime</span></div>
      </section>
      <section>
        <div class="section-title">
          <h2>Workers</h2>
          {_dispatch_mode_form(prefix, mode)}
        </div>
        {_worker_table(worker_statuses, admin_db.worker_success(24))}
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


def _keys_page(prefix: str, new_api_key: str | None = None) -> str:
    keys = admin_db.list_api_keys()
    notice = ""
    if new_api_key:
        notice = (
            "<div class='notice notice-prominent'>"
            "<strong>New API key created.</strong> Copy it now — refresh or "
            "leave this page and the raw value is gone forever "
            "(only the sha256 hash stays on the server)."
            "<div class='key-reveal-row'>"
            f"<code class='key-reveal' id='new-key-value'>{html.escape(new_api_key)}</code>"
            "<button class='copy-btn' type='button' data-copy-target='new-key-value'>Copy</button>"
            "</div>"
            "</div>"
        )
    static_keys = sorted(settings.api_keys)
    body = f"""
      <div class="page-head">
        <h2>API Keys</h2>
        <p class="page-sub">Issue bearer keys for each caller. The raw <code>gmw_&lt;random-token&gt;</code> value is only shown once at creation; the server stores a sha256 hash.</p>
      </div>
      {notice}
      <section>
        <h2>Create a new key</h2>
        <form class="inline" method="post" action="{prefix}/admin/api-keys">
          <input name="name" placeholder="Caller / project name (e.g. line-sticker-studio)" required>
          <button type="submit">Create key</button>
        </form>
      </section>
      <section>
        <div class="section-title">
          <h2>Issued keys</h2>
        </div>
        <p class="muted" style="margin: -4px 0 14px; font-size: 13px;">
          <strong>Heads up:</strong> the <em>Handle</em> column below is the admin reference
          ID (<code>key_&lt;last-12-chars&gt;</code>), <strong>not</strong> the bearer key.
          Callers must use the original <code>gmw_&lt;random-token&gt;</code> from creation time.
        </p>
        {_keys_table(keys, prefix)}
      </section>
      <section>
        <h2>Static keys ({len(static_keys)})</h2>
        <p class="muted" style="margin: -4px 0 14px; font-size: 13px;">
          From the <code>API_KEYS</code> env var — still valid, but read-only here. Edit <code>.env</code> and restart the service to add, remove, or rotate one.
        </p>
        {_static_keys_table(static_keys)}
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
    key_options = "".join(
        f"<option value='{html.escape(k['id'])}'>{html.escape(k['name'])}</option>"
        for k in admin_db.list_api_keys()
        if k["enabled"]
    )
    body = f"""
      <div class="page-head">
        <h2>Test generation</h2>
        <p class="page-sub">Runs the same worker pool a real caller hits — the image renders here, and (like real traffic) also gets saved to History.</p>
      </div>
      {notice_html}
      {error_html}
      <section>
        <form method="post" action="{prefix}/admin/test-generate" enctype="multipart/form-data" class="form-grid">
          <label>Kind
            <select name="kind">
              <option value="generate" selected>generate — text-to-image, no reference</option>
              <option value="edit">edit — image-to-image, needs a reference upload below</option>
              <option value="chat">chat — text only</option>
            </select>
          </label>
          <label>Prompt
            <textarea name="prompt" rows="3" required placeholder="A minimalist orange tabby cat clock face on white"></textarea>
          </label>
          <label>Reference image (only used when Kind = edit)
            <input name="reference_image" type="file" accept="image/*">
          </label>
          <label>Attribute to API key (optional — for testing which key shows up in History)
            <select name="api_key_choice">
              <option value="">none (anonymous, like an unauthenticated caller)</option>
              <option value="static">static (.env API_KEYS)</option>
              {key_options}
            </select>
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


def _requests_page(prefix: str, cleanup_result: int | None = None) -> str:
    requests = admin_db.list_recent(200)
    cleanup_html = ""
    if cleanup_result is not None:
        cleanup_html = f"<div class='notice'>Cleanup complete — deleted {cleanup_result} image file(s) older than {settings.image_retention_days}d.</div>"
    body = f"""
      <div class="page-head">
        <h2>History</h2>
        <p class="page-sub">Last 500 requests (rolling log), with which key and worker handled each one and a link to its output image(s).</p>
      </div>
      {cleanup_html}
      <section>
        <div class="section-title">
          <h2>Requests</h2>
          <form method="post" action="{prefix}/admin/cleanup"><button class="ghost" type="submit">Run cleanup</button></form>
        </div>
        {_requests_table(requests, prefix)}
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
    if delta < 0:  # 未來（例如圖片到期時間）
        # 無條件進位：剩 2.9 天講「in 2d」會讓人以為明天就被掃掉。
        ahead = -delta
        if ahead < 3600:
            return f"in {math.ceil(ahead / 60)}m"
        if ahead < 86400:
            return f"in {math.ceil(ahead / 3600)}h"
        return f"in {math.ceil(ahead / 86400)}d"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


_MODE_LABELS = {
    "round-robin": "Round-robin（輪流分攤配額）",
    "spillover": "Spillover（worker 0 主力，其餘備援）",
}


def _dispatch_mode_form(prefix: str, current: str) -> str:
    opts = "".join(
        f"<option value='{m}'{' selected' if m == current else ''}>{html.escape(label)}</option>"
        for m, label in _MODE_LABELS.items()
    )
    return (
        f"<form method='post' action='{prefix}/admin/dispatch-mode' class='mode-form'>"
        "<label>Dispatch mode "
        f"<select name='mode'>{opts}</select></label> "
        "<button type='submit'>Apply</button></form>"
    )


def _success_cell(counts: dict[str, int] | None) -> str:
    """近 24h 成功率。沒流量就留白（0/0 不是 0%，別誤報成掛掉）。"""
    if not counts:
        return "<span class='chip chip-mute'>no traffic</span>"
    ok, bad = counts.get("succeeded", 0), counts.get("failed", 0)
    total = ok + bad
    if total == 0:
        return "<span class='chip chip-mute'>no traffic</span>"
    rate = round(100 * ok / total)
    # 全綠 100%、有失敗但還在跑 = 警示、全滅 = 紅。全滅通常代表這個帳號撞到
    # 只有它會遇到的 UI 變體，不是服務掛了（服務會被付費 fallback 蓋過去）。
    cls = "chip-ok" if rate == 100 else ("chip-fail" if ok == 0 else "chip-run")
    return f"<span class='chip {cls}'>{rate}%</span> <span class='muted'>{ok}/{total}</span>"


def _worker_table(statuses: list[dict[str, Any]], success: dict[int, dict[str, int]] | None = None) -> str:
    success = success or {}
    rows = []
    for s in statuses:
        alive = "<span class='chip chip-ok'>alive</span>" if s["alive"] else "<span class='chip chip-fail'>down</span>"
        logged_in = "<span class='chip chip-ok'>yes</span>" if s["logged_in"] else "<span class='chip chip-fail'>no</span>"
        busy = "<span class='chip chip-run'>busy</span>" if s["busy"] else "<span class='chip chip-mute'>idle</span>"
        account = html.escape(s.get("account") or "—")
        rows.append(
            f"<tr><td>{s['id']}</td><td>{account}</td><td>{alive}</td>"
            f"<td>{logged_in}</td><td>{busy}</td>"
            f"<td>{_success_cell(success.get(s['id']))}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6' class='empty'>No workers.</td></tr>")
    return (
        "<div class='table-wrap'><table><thead><tr><th>ID</th><th>Account</th><th>Alive</th>"
        "<th>Logged in</th><th>Status</th><th>24h success</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
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
        key_name = html.escape(item.get("api_key_name") or "—")
        worker_str = item.get("worker_id")
        worker_str = str(worker_str) if worker_str is not None else "—"
        items.append(
            "<li class='activity-item'>"
            f"<div class='activity-row'>{_status_chip(item['status'])}"
            f"<code class='activity-id'>{html.escape(item['kind'])}</code>"
            f"<span class='activity-time'>{when}</span></div>"
            f"<div class='activity-meta'>key: {key_name} · worker: {worker_str} · {html.escape(prompt_short)}</div>"
            "</li>"
        )
    return "<ul class='activity'>" + "".join(items) + "</ul>"


def _keys_table(keys: list[dict[str, Any]], prefix: str) -> str:
    rows = []
    for key in keys:
        enabled = (
            "<span class='chip chip-ok'>enabled</span>"
            if key["enabled"]
            else "<span class='chip chip-mute'>disabled</span>"
        )
        action_forms = []
        if key["enabled"]:
            action_forms.append(
                f"<form method='post' action='{prefix}/admin/api-keys/{html.escape(key['id'])}/disable' style='display:inline'>"
                "<button class='ghost' type='submit'>Disable</button></form>"
            )
        action_forms.append(
            f"<form method='post' action='{prefix}/admin/api-keys/{html.escape(key['id'])}/delete' style='display:inline;margin-left:6px'"
            " onsubmit=\"return confirm('Delete this API key permanently?');\">"
            "<button class='danger' type='submit'>Delete</button></form>"
        )
        action = "".join(action_forms)
        rows.append(
            "<tr>"
            f"<td><code class='handle'>{html.escape(key['id'])}</code></td>"
            f"<td><strong class='key-name'>{html.escape(key['name'])}</strong></td>"
            f"<td>{enabled}</td>"
            f"<td>{html.escape(str(key['requests_count']))}</td>"
            f"<td>{_relative_time(key['last_used_at']) or '—'}</td>"
            f"<td class='actions'>{action}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='6' class='empty'>No issued keys yet. Use the form above to create one.</td></tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th title='Admin reference ID — not the bearer key.'>Handle</th>"
        "<th>Name</th><th>Status</th><th>Requests</th>"
        "<th>Last used</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _static_keys_table(keys: list[str]) -> str:
    rows = []
    for key in keys:
        masked = key[:4] + "…" + key[-4:] if len(key) > 8 else "…"
        rows.append(f"<tr><td><code>{html.escape(masked)}</code></td></tr>")
    if not rows:
        rows.append("<tr><td class='empty'>None configured.</td></tr>")
    return f"<table><thead><tr><th>Key (masked)</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _peek(text: str, limit: int = 90) -> str:
    """摘要用的單行預覽：換行壓掉再截斷。History 每筆都要點開 <details> 才知道
    是哪一張圖、哪一類失敗，太浪費點擊。"""
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _requests_table(requests: list[dict[str, Any]], prefix: str) -> str:
    rows = []
    for item in requests:
        error = item.get("error") or ""
        if len(error) > 180:
            error = error[:177] + "..."
        duration = item.get("duration_seconds")
        duration_str = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "—"
        worker_id = item.get("worker_id")
        worker_str = str(worker_id) if worker_id is not None else "—"
        links = [
            f"<a href='{prefix}/generated/{html.escape(Path(p).name)}' target='_blank'>image</a>"
            for p in (item.get("image_paths") or [])
        ]
        # 落地圖片會在生圖時被順手掃掉（image_store.sweep_old），到期時間就是
        # created_at + IMAGE_RETENTION_DAYS。沒有圖的那幾筆沒東西可過期。
        expires_str = "—"
        if links:
            try:
                created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                expiry = created + timedelta(days=settings.image_retention_days)
                expires_str = (
                    _relative_time(expiry.isoformat())
                    if expiry > datetime.now(timezone.utc)
                    else "<span class='muted'>swept</span>"
                )
            except (ValueError, KeyError):
                expires_str = "—"
        # 文字路（chat / chat-file）的產出就是這段字；出圖路沒有，顯示 —
        response_text = item.get("response_text") or ""
        delete_form = (
            f"<form method='post' action='{prefix}/admin/requests/{html.escape(item['id'])}/delete' style='display:inline'"
            " onsubmit=\"return confirm('Delete this history row and its image(s)?');\">"
            "<button class='danger' type='submit'>Delete</button></form>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['kind'])}</td>"
            f"<td>{_status_chip(item['status'])}</td>"
            f"<td>{html.escape(item.get('api_key_name') or '—')}</td>"
            f"<td>{worker_str}</td>"
            f"<td>{html.escape(item.get('via') or '—')}</td>"
            f"<td>{duration_str}</td>"
            f"<td>{_relative_time(item['created_at'])}</td>"
            f"<td title='Images are swept {settings.image_retention_days}d after creation, on the next generate'>{expires_str}</td>"
            f"<td>{', '.join(links) or '—'}</td>"
            f"<td class='cell-peek'><details><summary>{html.escape(_peek(item['prompt'])) or 'Prompt'}</summary>"
            f"<pre>{html.escape(item['prompt'])}</pre></details></td>"
            + (
                f"<td class='cell-peek'><details><summary>{html.escape(_peek(response_text))}</summary>"
                f"<pre>{html.escape(response_text)}</pre></details></td>"
                if response_text else "<td class='cell-peek'>—</td>"
            ) + (
                f"<td class='cell-peek'><details><summary class='summary-error'>{html.escape(_peek(error))}</summary>"
                f"<pre>{html.escape(error)}</pre></details></td>"
                if error else "<td class='cell-peek'>—</td>"
            ) +
            f"<td>{delete_form}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='13' class='empty'>No requests logged yet.</td></tr>")
    return (
        "<div class='table-wrap'><table><thead><tr><th>Kind</th><th>Status</th><th>Key</th><th>Worker</th><th>Via</th><th>Duration</th>"
        "<th>Created</th>"
        f"<th title='Output images are deleted {settings.image_retention_days}d after creation by the sweep'>Expires</th>"
        "<th>Images</th><th>Prompt</th><th>Response</th><th>Error</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
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
    <body>{body}
      <script>{_COPY_SCRIPT}</script>
    </body>
    </html>
    """


_COPY_SCRIPT = """
document.addEventListener('click', function(e) {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  let value = btn.getAttribute('data-copy-value');
  if (!value) {
    const targetId = btn.getAttribute('data-copy-target');
    if (targetId) {
      const el = document.getElementById(targetId);
      if (el) value = el.textContent.trim();
    }
  }
  if (!value) return;
  const done = () => {
    const original = btn.textContent;
    btn.textContent = 'Copied ✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1400);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = value; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } finally { document.body.removeChild(ta); }
    });
  } else {
    const ta = document.createElement('textarea');
    ta.value = value; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } finally { document.body.removeChild(ta); }
  }
});
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

  /* History 有 12 欄，1180px 塞不下就整片爆出版面。放寬到 1600 並保留在超寬螢幕
     上不無限拉長的上限；真的還是不夠時由 .table-wrap 接手橫向捲動。 */
  .content { padding: 32px 36px 64px; max-width: 1600px; }
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
    /* 寫死 4 欄的話，第 5 格（Uptime）會被擠到下一行自己佔滿整排。改成
       auto-fit：容器放寬後五格一列排得下，以後增減格子也不必再改這裡。 */
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
  button.danger { background: var(--danger); }
  button.danger:hover { filter: brightness(1.08); }

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
  td.actions { white-space: nowrap; }
  td.empty { text-align: center; color: var(--muted); padding: 32px 12px; }
  /* Dispatch mode 切換器塞在 section 標題列。沒有這段的話會掉進全域的
     label{display:grid} 與 select{width:100%}，整個表單被擠成一團。 */
  .mode-form { display: flex; align-items: center; gap: 10px; margin: 0; }
  .mode-form label { display: flex; align-items: center; gap: 8px; margin: 0; white-space: nowrap; }
  .mode-form select { width: auto; min-width: 240px; }
  .mode-form button { padding: 6px 16px; font-size: 12.5px; box-shadow: none; }

  /* 表格一律包一層可橫向捲動的容器：欄位再多也只有表格自己捲，版面不會被撐破 */
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .table-wrap table { min-width: 100%; }

  /* 表格裡的按鈕用小一號的尺寸：沿用全域 pill 會佔掉兩行高，而那一格只有一顆
     按鈕，空的那行純浪費。 */
  td button { padding: 5px 14px; font-size: 12.5px; box-shadow: none; }
  td form { margin: 0; }

  /* History 的 Prompt / Error 欄：摘要行直接顯示前 90 字，要全文再展開 */
  td.cell-peek { max-width: 320px; }
  td.cell-peek summary {
    cursor: pointer; color: var(--ink-soft);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  td.cell-peek summary:hover { color: var(--ink); }
  td.cell-peek summary.summary-error { color: var(--danger); }
  td.cell-peek pre {
    white-space: pre-wrap; word-break: break-word;
    margin: 8px 0 0; font-size: 12px;
  }
  code.handle { background: transparent; color: var(--muted); padding: 0; font-size: 11.5px; }
  .key-name { color: var(--ink); font-weight: 600; font-size: 14px; }

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
