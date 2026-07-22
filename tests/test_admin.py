"""Admin webui 測試：session 簽章、history db、登入流程、test-generate"""
import time

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

from src import history_db
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


# ── history_db.py ──


@pytest.fixture
def temp_history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(history_db, "_DB_PATH", tmp_path / "admin.db")
    history_db.init_db()
    return history_db


def test_history_record_and_list(temp_history_db):
    temp_history_db.record(kind="generate", prompt="a cat", status="succeeded", via="browser", duration_seconds=1.5)
    temp_history_db.record(kind="chat", prompt="hi", status="failed", error="boom", duration_seconds=0.2)
    rows = temp_history_db.list_recent(10)
    assert len(rows) == 2
    assert rows[0]["kind"] == "chat"  # most recent first
    assert rows[1]["prompt"] == "a cat"


def test_history_stats(temp_history_db):
    temp_history_db.record(kind="generate", prompt="x", status="succeeded")
    temp_history_db.record(kind="generate", prompt="y", status="failed")
    s = temp_history_db.stats()
    assert s == {"total": 2, "succeeded": 1, "failed": 1}


def test_history_caps_at_500(temp_history_db):
    for i in range(510):
        temp_history_db.record(kind="generate", prompt=f"p{i}", status="succeeded")
    assert temp_history_db.stats()["total"] == 500


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
    default_max_age = _cookie_max_age(default_resp.headers["set-cookie"])
    remember_max_age = _cookie_max_age(remember_resp.headers["set-cookie"])
    assert remember_max_age > default_max_age
    assert remember_max_age == 30 * 86400


def _cookie_max_age(set_cookie_header: str) -> int:
    for part in set_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "max-age":
            return int(value)
    raise AssertionError(f"no Max-Age in Set-Cookie: {set_cookie_header}")


@pytest.mark.asyncio
async def test_admin_test_generate_logs_history(mock_worker_pool, temp_history_db):
    mock_worker_pool.dispatch = AsyncMock(return_value={"success": True, "images": ["data:image/png;base64,abc"]})

    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/login", data={"username": "admin", "password": "test-pass"})
        resp = await client.post("/admin/test-generate", data={"kind": "generate", "prompt": "a cat", "timeout": "30"})

    assert resp.status_code == 200
    assert "test-result-img" in resp.text
    rows = temp_history_db.list_recent(10)
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["prompt"] == "a cat"
