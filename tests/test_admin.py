"""Admin webui 測試：session 簽章、history db、登入流程、test-generate"""
import time

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

from src import admin_db, image_store
from src.config import settings
from src.security import create_admin_session, verify_admin_session

# 1x1 transparent PNG, base64-encoded — smallest valid PNG for round-trip tests.
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
_TINY_PNG_DATA_URL = f"data:image/png;base64,{_TINY_PNG_B64}"


# ── security.py ──


def test_session_roundtrip():
    token = create_admin_session("admin", "secret", ttl_seconds=60)
    assert verify_admin_session(token, "secret") == "admin"


def test_session_wrong_secret_rejected():
    token = create_admin_session("admin", "secret", ttl_seconds=60)
    assert verify_admin_session(token, "other-secret") is None


def test_session_expired_rejected():
    token = create_admin_session("admin", "secret", ttl_seconds=-1)
    assert verify_admin_session(token, "secret") is None


def test_session_remember_me_ttl_longer():
    default_token = create_admin_session("admin", "secret", ttl_seconds=86400)
    remember_token = create_admin_session("admin", "secret", ttl_seconds=30 * 86400)
    assert verify_admin_session(default_token, "secret") == "admin"
    assert verify_admin_session(remember_token, "secret") == "admin"
    # remember-me payload carries a later expiry than the default session
    import json
    from src.security import _b64decode

    default_exp = json.loads(_b64decode(default_token.split(".")[0]))["exp"]
    remember_exp = json.loads(_b64decode(remember_token.split(".")[0]))["exp"]
    assert remember_exp > default_exp


# ── admin_db.py ──


@pytest.fixture
def temp_admin_db(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_db, "_DB_PATH", tmp_path / "admin.db")
    admin_db.init_db()
    return admin_db


def test_history_record_and_list(temp_admin_db):
    temp_admin_db.record(kind="generate", prompt="a cat", status="succeeded", via="browser", duration_seconds=1.5)
    temp_admin_db.record(kind="chat", prompt="hi", status="failed", error="boom", duration_seconds=0.2)
    rows = temp_admin_db.list_recent(10)
    assert len(rows) == 2
    assert rows[0]["kind"] == "chat"  # most recent first
    assert rows[1]["prompt"] == "a cat"


def test_history_stats(temp_admin_db):
    temp_admin_db.record(kind="generate", prompt="x", status="succeeded")
    temp_admin_db.record(kind="generate", prompt="y", status="failed")
    s = temp_admin_db.stats()
    assert s == {"total": 2, "succeeded": 1, "failed": 1}


def test_history_caps_at_500(temp_admin_db):
    for i in range(510):
        temp_admin_db.record(kind="generate", prompt=f"p{i}", status="succeeded")
    assert temp_admin_db.stats()["total"] == 500


def test_create_api_key_roundtrip(temp_admin_db):
    row, raw_key = temp_admin_db.create_api_key("test caller")
    assert raw_key.startswith("gmw_")
    assert row["name"] == "test caller"
    assert row["enabled"] == 1
    listed = temp_admin_db.list_api_keys()
    assert len(listed) == 1
    assert listed[0]["id"] == row["id"]


def test_get_api_key_by_token(temp_admin_db):
    _, raw_key = temp_admin_db.create_api_key("caller")
    found = temp_admin_db.get_api_key_by_token(raw_key)
    assert found is not None
    assert temp_admin_db.get_api_key_by_token("not-a-real-key") is None


def test_disable_and_delete_api_key(temp_admin_db):
    row, raw_key = temp_admin_db.create_api_key("caller")
    temp_admin_db.disable_api_key(row["id"])
    assert temp_admin_db.get_api_key_by_token(raw_key)["enabled"] == 0

    temp_admin_db.delete_api_key(row["id"])
    assert temp_admin_db.get_api_key_by_token(raw_key) is None


def test_mark_api_key_used(temp_admin_db):
    row, _ = temp_admin_db.create_api_key("caller")
    temp_admin_db.mark_api_key_used(row["id"])
    updated = temp_admin_db.list_api_keys()[0]
    assert updated["requests_count"] == 1
    assert updated["last_used_at"] is not None


def test_has_any_dynamic_key(temp_admin_db):
    assert temp_admin_db.has_any_dynamic_key() is False
    temp_admin_db.create_api_key("caller")
    assert temp_admin_db.has_any_dynamic_key() is True


# ── auth: static env keys + dynamic admin-issued keys ──


def test_is_valid_api_key_accepts_static_env_key(monkeypatch, temp_admin_db):
    from src.main import _is_valid_api_key

    monkeypatch.setattr(settings, "api_keys", {"static-123"})
    assert _is_valid_api_key("static-123") is True
    assert _is_valid_api_key("wrong") is False


def test_is_valid_api_key_accepts_dynamic_key(monkeypatch, temp_admin_db):
    from src.main import _is_valid_api_key

    monkeypatch.setattr(settings, "api_keys", set())
    _, raw_key = temp_admin_db.create_api_key("caller")
    assert _is_valid_api_key(raw_key) is True


def test_is_valid_api_key_rejects_disabled_dynamic_key(monkeypatch, temp_admin_db):
    from src.main import _is_valid_api_key

    monkeypatch.setattr(settings, "api_keys", set())
    row, raw_key = temp_admin_db.create_api_key("caller")
    temp_admin_db.disable_api_key(row["id"])
    assert _is_valid_api_key(raw_key) is False


def test_is_valid_api_key_none_configured_rejects_everything(monkeypatch, temp_admin_db):
    from src.main import _is_valid_api_key

    monkeypatch.setattr(settings, "api_keys", set())
    assert _is_valid_api_key("anything") is False
    assert _is_valid_api_key(None) is False


def _fake_request(headers=None):
    from starlette.requests import Request

    return Request(scope={"type": "http", "headers": headers or [], "query_string": b""})


def test_verify_api_key_open_when_nothing_configured(monkeypatch, temp_admin_db):
    from src.main import _verify_api_key

    monkeypatch.setattr(settings, "api_keys", set())
    _verify_api_key(_fake_request(), None)  # must not raise


def test_verify_api_key_rejects_wrong_key_when_static_configured(monkeypatch, temp_admin_db):
    from fastapi import HTTPException

    from src.main import _verify_api_key

    monkeypatch.setattr(settings, "api_keys", {"good"})
    with pytest.raises(HTTPException):
        _verify_api_key(_fake_request(), "bad")


def test_verify_api_key_accepts_dynamic_key(monkeypatch, temp_admin_db):
    from src.main import _verify_api_key

    monkeypatch.setattr(settings, "api_keys", set())
    _, raw_key = temp_admin_db.create_api_key("caller")
    _verify_api_key(_fake_request(), raw_key)  # must not raise


def test_verify_api_key_rejects_when_only_dynamic_key_configured_and_wrong(monkeypatch, temp_admin_db):
    from fastapi import HTTPException

    from src.main import _verify_api_key

    monkeypatch.setattr(settings, "api_keys", set())
    temp_admin_db.create_api_key("caller")
    with pytest.raises(HTTPException):
        _verify_api_key(_fake_request(), "not-the-issued-key")


# ── admin routes ──


@pytest.fixture(autouse=True)
def admin_creds(monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "test-pass")
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    monkeypatch.setattr(settings, "admin_url_prefix", "")


@pytest.fixture
def mock_worker_pool():
    with patch("src.main.worker_pool") as mock:
        mock.start = AsyncMock()
        mock.stop = AsyncMock()
        mock.worker_status = AsyncMock(return_value=[{"id": 0, "alive": True, "logged_in": True, "busy": False}])
        yield mock


@pytest.mark.asyncio
async def test_admin_root_redirects_to_login_when_unauthenticated():
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_admin_login_wrong_password_rejected():
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/admin/login", data={"username": "admin", "password": "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_login_success_sets_cookie_and_allows_overview(mock_worker_pool):
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login", data={"username": "admin", "password": "test-pass"}, follow_redirects=False
        )
        assert login_resp.status_code == 303
        assert "admin_session" in login_resp.cookies

        overview_resp = await client.get("/admin")
    assert overview_resp.status_code == 200
    assert "Overview" in overview_resp.text


@pytest.mark.asyncio
async def test_admin_login_remember_me_extends_cookie_lifetime():
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        default_resp = await client.post(
            "/admin/login", data={"username": "admin", "password": "test-pass"}, follow_redirects=False
        )
        remember_resp = await client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-pass", "remember": "on"},
            follow_redirects=False,
        )
    default_max_age = _cookie_max_age(_session_set_cookie(default_resp.headers))
    remember_max_age = _cookie_max_age(_session_set_cookie(remember_resp.headers))
    assert remember_max_age > default_max_age
    assert remember_max_age == 30 * 86400


@pytest.mark.asyncio
async def test_admin_session_cookie_scoped_to_url_prefix(monkeypatch):
    """Regression: without an explicit Path, the cookie defaults to "/" and
    collides with any other admin webui sharing the same domain (e.g.
    codex-image-service) — both use the cookie name "admin_session"."""
    monkeypatch.setattr(settings, "admin_url_prefix", "/gemini-web")
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/login", data={"username": "admin", "password": "test-pass"}, follow_redirects=False
        )
    assert _cookie_path(_session_set_cookie(resp.headers)) == "/gemini-web"


@pytest.mark.asyncio
async def test_admin_login_clears_stale_root_path_cookie():
    """Login must also emit a Set-Cookie that deletes any pre-fix, unscoped
    (Path=/) admin_session cookie still sitting in the browser — otherwise
    the browser sends both and the server can end up reading the stale one."""
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/login", data={"username": "admin", "password": "test-pass"}, follow_redirects=False
        )
    set_cookie_headers = resp.headers.get_list("set-cookie")
    root_clears = [h for h in set_cookie_headers if _cookie_path(h) == "/" and 'admin_session=""' in h]
    assert len(root_clears) == 1


def _session_set_cookie(headers) -> str:
    """Pick the Set-Cookie header that actually carries the session token
    (non-empty value) — login also emits a same-named deletion cookie for
    the old root path, see test_admin_login_clears_stale_root_path_cookie."""
    for h in headers.get_list("set-cookie"):
        if h.startswith("admin_session=") and not h.startswith('admin_session=""'):
            return h
    raise AssertionError(f"no session-bearing Set-Cookie found: {headers.get_list('set-cookie')}")


def _cookie_path(set_cookie_header: str) -> str:
    for part in set_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "path":
            return value
    raise AssertionError(f"no Path in Set-Cookie: {set_cookie_header}")


def _cookie_max_age(set_cookie_header: str) -> int:
    for part in set_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "max-age":
            return int(value)
    raise AssertionError(f"no Max-Age in Set-Cookie: {set_cookie_header}")


@pytest.mark.asyncio
async def test_admin_test_generate_logs_history(mock_worker_pool, temp_admin_db):
    mock_worker_pool.dispatch = AsyncMock(return_value={"success": True, "images": ["data:image/png;base64,abc"]})

    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        resp = await client.post("/admin/test-generate", data={"kind": "generate", "prompt": "a cat", "timeout": "30"})

    assert resp.status_code == 200
    assert "test-result-img" in resp.text
    rows = temp_admin_db.list_recent(10)
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["prompt"] == "a cat"


@pytest.mark.asyncio
async def test_admin_create_api_key_flow(temp_admin_db):
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})

        create_resp = await client.post(
            "/admin/api-keys", data={"name": "line-sticker-studio"}, follow_redirects=True
        )
        assert create_resp.status_code == 200  # followed the redirect
        assert "New API key created" in create_resp.text
        assert "gmw_" in create_resp.text

        # one-shot reveal: a second visit to the keys page must NOT show the raw key again
        again_resp = await client.get("/admin/keys")
    assert "New API key created" not in again_resp.text
    assert "line-sticker-studio" in again_resp.text

    keys = temp_admin_db.list_api_keys()
    assert len(keys) == 1
    assert keys[0]["name"] == "line-sticker-studio"


@pytest.mark.asyncio
async def test_admin_disable_and_delete_api_key(temp_admin_db):
    row, _ = temp_admin_db.create_api_key("caller")
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})

        disable_resp = await client.post(f"/admin/api-keys/{row['id']}/disable", follow_redirects=True)
        assert disable_resp.status_code == 200
        assert temp_admin_db.list_api_keys()[0]["enabled"] == 0

        delete_resp = await client.post(f"/admin/api-keys/{row['id']}/delete", follow_redirects=True)
        assert delete_resp.status_code == 200
    assert temp_admin_db.list_api_keys() == []


# ── image_store.py ──


def test_save_images_writes_files_and_returns_filenames(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images([_TINY_PNG_DATA_URL])
    assert len(filenames) == 1
    assert (tmp_path / "generated" / filenames[0]).is_file()
    assert filenames[0].endswith(".png")


def test_save_images_skips_undecodable_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images(["not base64 at all!!"])
    assert filenames == []


def test_delete_files_removes_named_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images([_TINY_PNG_DATA_URL])
    image_store.delete_files(filenames)
    assert not (tmp_path / "generated" / filenames[0]).exists()


def test_sweep_old_deletes_files_past_retention(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images([_TINY_PNG_DATA_URL])
    old_path = tmp_path / "generated" / filenames[0]
    old_time = time.time() - 10 * 86400  # 10 days old
    os.utime(old_path, (old_time, old_time))

    deleted = image_store.sweep_old(retention_days=7)
    assert deleted == 1
    assert not old_path.exists()


def test_sweep_old_keeps_recent_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images([_TINY_PNG_DATA_URL])
    deleted = image_store.sweep_old(retention_days=7)
    assert deleted == 0
    assert (tmp_path / "generated" / filenames[0]).exists()


# ── admin_db.py: image_paths / api_key_name / worker_id ──


def test_record_and_get_request_roundtrip(temp_admin_db):
    request_id = temp_admin_db.record(
        kind="generate",
        prompt="a cat",
        status="succeeded",
        image_paths=["abc123.png"],
        api_key_name="line-sticker-studio",
        worker_id=1,
    )
    row = temp_admin_db.get_request(request_id)
    assert row["image_paths"] == ["abc123.png"]
    assert row["api_key_name"] == "line-sticker-studio"
    assert row["worker_id"] == 1

    listed = temp_admin_db.list_recent(10)
    assert listed[0]["image_paths"] == ["abc123.png"]


def test_delete_request_removes_row(temp_admin_db):
    request_id = temp_admin_db.record(kind="generate", prompt="x", status="succeeded")
    temp_admin_db.delete_request(request_id)
    assert temp_admin_db.get_request(request_id) is None


def test_get_request_missing_returns_none(temp_admin_db):
    assert temp_admin_db.get_request("does-not-exist") is None


# ── main.py: _identify_caller + image persistence wired into _dispatch_and_log ──


def test_identify_caller_no_key_present():
    from src.main import _identify_caller

    assert _identify_caller(None) == ""


def test_identify_caller_static_key(monkeypatch, temp_admin_db):
    from src.main import _identify_caller

    monkeypatch.setattr(settings, "api_keys", {"static-123"})
    req = _fake_request(headers=[(b"x-goog-api-key", b"static-123")])
    assert _identify_caller(req) == "static"


def test_identify_caller_dynamic_key_bumps_usage(monkeypatch, temp_admin_db):
    from src.main import _identify_caller

    monkeypatch.setattr(settings, "api_keys", set())
    row, raw_key = temp_admin_db.create_api_key("line-sticker-studio")
    req = _fake_request(headers=[(b"x-goog-api-key", raw_key.encode())])
    assert _identify_caller(req) == "line-sticker-studio"
    assert temp_admin_db.list_api_keys()[0]["requests_count"] == 1


def test_identify_caller_unknown_key():
    from src.main import _identify_caller

    req = _fake_request(headers=[(b"x-goog-api-key", b"totally-made-up")])
    assert _identify_caller(req) == "unknown key"


@pytest.mark.asyncio
async def test_dispatch_and_log_saves_images_and_records_worker_id(mock_worker_pool, temp_admin_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    mock_worker_pool.dispatch = AsyncMock(
        return_value={"success": True, "images": [_TINY_PNG_DATA_URL], "worker_id": 1}
    )
    from src.main import _dispatch_and_log

    result = await _dispatch_and_log("generate", "a cat", "", 30)
    assert result["worker_id"] == 1

    rows = temp_admin_db.list_recent(1)
    assert rows[0]["worker_id"] == 1
    assert len(rows[0]["image_paths"]) == 1
    assert (tmp_path / "generated" / rows[0]["image_paths"][0]).is_file()


@pytest.mark.asyncio
async def test_dispatch_and_log_api_key_name_override(mock_worker_pool, temp_admin_db):
    mock_worker_pool.dispatch = AsyncMock(return_value={"success": True, "images": []})
    from src.main import _dispatch_and_log

    await _dispatch_and_log("chat", "hi", "", 30, api_key_name="manually-picked")
    rows = temp_admin_db.list_recent(1)
    assert rows[0]["api_key_name"] == "manually-picked"


# ── admin routes: edit mode upload, cleanup, per-row delete ──


@pytest.mark.asyncio
async def test_admin_test_generate_edit_mode_with_upload(mock_worker_pool, temp_admin_db, tmp_path, monkeypatch):
    import base64

    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    mock_worker_pool.dispatch = AsyncMock(
        return_value={"success": True, "images": [_TINY_PNG_DATA_URL], "worker_id": 0}
    )
    from src.main import app

    transport = ASGITransport(app=app)
    png_bytes = base64.b64decode(_TINY_PNG_B64)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        resp = await client.post(
            "/admin/test-generate",
            data={"kind": "edit", "prompt": "make it blue", "timeout": "30"},
            files={"reference_image": ("ref.png", png_bytes, "image/png")},
        )
    assert resp.status_code == 200
    assert "test-result-img" in resp.text
    call_kwargs = mock_worker_pool.dispatch.call_args
    assert call_kwargs.kwargs["extra"]["reference_image"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_admin_test_generate_edit_mode_without_upload_errors(temp_admin_db):
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        resp = await client.post(
            "/admin/test-generate", data={"kind": "edit", "prompt": "make it blue", "timeout": "30"}
        )
    assert resp.status_code == 200
    assert "needs a reference image" in resp.text


@pytest.mark.asyncio
async def test_admin_test_generate_attributes_to_chosen_key(mock_worker_pool, temp_admin_db):
    mock_worker_pool.dispatch = AsyncMock(return_value={"success": True, "images": []})
    row, _ = temp_admin_db.create_api_key("line-sticker-studio")

    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        await client.post(
            "/admin/test-generate",
            data={"kind": "chat", "prompt": "hi", "timeout": "30", "api_key_choice": row["id"]},
        )
    rows = temp_admin_db.list_recent(1)
    assert rows[0]["api_key_name"] == "line-sticker-studio"
    assert temp_admin_db.list_api_keys()[0]["requests_count"] == 1


@pytest.mark.asyncio
async def test_admin_cleanup_route(temp_admin_db, tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images([_TINY_PNG_DATA_URL])
    old_path = tmp_path / "generated" / filenames[0]
    old_time = time.time() - 10 * 86400
    os.utime(old_path, (old_time, old_time))

    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        resp = await client.post("/admin/cleanup")
    assert resp.status_code == 200
    assert "Cleanup complete" in resp.text
    assert not old_path.exists()


@pytest.mark.asyncio
async def test_admin_delete_request_removes_row_and_file(temp_admin_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
    filenames = image_store.save_images([_TINY_PNG_DATA_URL])
    request_id = temp_admin_db.record(
        kind="generate", prompt="x", status="succeeded", image_paths=filenames
    )

    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        resp = await client.post(f"/admin/requests/{request_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert temp_admin_db.get_request(request_id) is None
    assert not (tmp_path / "generated" / filenames[0]).exists()


# ── /generated static mount: dir-not-yet-created shouldn't 500 ──


def test_static_files_dir_must_exist_before_first_request(tmp_path):
    """Documents the bug main.py's mkdir-before-mount guards against:
    StaticFiles(check_dir=False) only skips the *startup* existence check —
    it still os.stat()s `directory` on every request. If the dir is only
    created lazily later (e.g. by image_store on first successful save), any
    request to /generated/* before that first save 500s instead of 404ing."""
    from starlette.applications import Starlette
    from starlette.staticfiles import StaticFiles
    from starlette.testclient import TestClient

    missing_dir = tmp_path / "generated"  # deliberately not created yet
    app = Starlette()
    app.mount("/generated", StaticFiles(directory=str(missing_dir), check_dir=False))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/generated/nonexistent.png").status_code == 500

    missing_dir.mkdir(parents=True, exist_ok=True)  # main.py does this before mounting
    assert client.get("/generated/nonexistent.png").status_code == 404


def test_worker_success_24h(temp_admin_db):
    """Overview 的『24h success』欄：分 worker 統計，沒流量的 worker 不出現。

    這張表存在的理由：付費 fallback 會把單一 worker 的失敗補起來（API 照樣回
    200），所以服務「看起來正常」不代表每個帳號都在幹活 —— 只有這裡看得到。
    """
    for _ in range(3):
        temp_admin_db.record(kind="edit", prompt="x", status="succeeded", worker_id=0)
    temp_admin_db.record(kind="edit", prompt="x", status="failed", worker_id=1)
    temp_admin_db.record(kind="edit", prompt="x", status="succeeded", worker_id=1)
    temp_admin_db.record(kind="chat", prompt="x", status="succeeded")  # 沒帶 worker_id

    got = temp_admin_db.worker_success(24)
    assert got == {
        0: {"succeeded": 3, "failed": 0},
        1: {"succeeded": 1, "failed": 1},
    }
    assert 2 not in got  # 沒跑過的 worker 不會憑空出現


def test_worker_success_cell_renders():
    """0/0 要顯示 no traffic，不能算成 0% 誤報為掛掉。"""
    from src.admin import _success_cell

    assert "100%" in _success_cell({"succeeded": 4, "failed": 0})
    assert "50%" in _success_cell({"succeeded": 1, "failed": 1})
    assert "0%" in _success_cell({"succeeded": 0, "failed": 5})
    assert "chip-fail" in _success_cell({"succeeded": 0, "failed": 5})  # 全滅要紅的
    assert "no traffic" in _success_cell({"succeeded": 0, "failed": 0})
    assert "no traffic" in _success_cell(None)


def test_relative_time_handles_future():
    """History 的 Expires 欄是未來時間，不能被講成「幾天前」。"""
    from datetime import datetime, timedelta, timezone

    from src.admin import _relative_time

    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert _relative_time(future) == "in 3d"

    soon = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    assert _relative_time(soon) == "in 5h"

    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert past and _relative_time(past).endswith("ago")


def test_tables_are_wrapped_for_horizontal_scroll():
    """History 有 12 欄，塞不下時要讓表格自己捲，而不是整片爆出版面。"""
    from src.admin import _keys_table, _requests_table, _worker_table

    for html_out in (
        _requests_table([], ""),
        _worker_table([], {}),
        _keys_table([], ""),
    ):
        assert html_out.startswith("<div class='table-wrap'>")
        assert html_out.endswith("</table></div>")


def test_mode_form_has_its_own_css():
    """.mode-form 一定要有自己的排版規則。

    沒有的話它會掉進全域的 label{display:grid} 和 select{width:100%}，
    整個切換器被擠成一團塞在標題列右邊（2026-07-27 實際發生過）。
    """
    from src.admin import _STYLES

    assert ".mode-form {" in _STYLES
    assert ".mode-form select" in _STYLES  # 覆蓋 select{width:100%}
    assert ".mode-form label" in _STYLES   # 覆蓋 label{display:grid}


def test_stats_row_is_not_hardcoded_to_four():
    """Overview 現在有 5 格（含 Queued/running 與 Uptime）。

    寫死 repeat(4,…) 會把第 5 格擠到下一行獨佔整排；auto-fit 才會隨容器寬度
    與格子數自己排。
    """
    from src.admin import _STYLES

    assert "repeat(auto-fit, minmax(180px, 1fr))" in _STYLES
    assert "repeat(4, minmax(0, 1fr))" not in _STYLES
