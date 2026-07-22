"""Admin webui 測試：session 簽章、history db、登入流程、test-generate"""
import time

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

from src import admin_db
from src.config import settings
from src.security import create_admin_session, verify_admin_session


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


def _fake_request():
    from starlette.requests import Request

    return Request(scope={"type": "http", "headers": [], "query_string": b""})


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
