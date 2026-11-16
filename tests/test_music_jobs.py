"""排隊版作曲 job：POST /api/music/jobs（或 POST /api/jobs 帶 type=music）→ GET /api/jobs/{id}

生成本身用假的 worker_pool.dispatch 代替，不碰瀏覽器、不打 Gemini。
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src import jobs
from src.config import settings


@pytest.fixture(autouse=True)
def _tmp_jobs_db(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(settings, "api_keys", ["secret"])


@pytest.fixture
def pool():
    with patch("src.main.worker_pool") as mock:
        mock.start = AsyncMock()
        mock.stop = AsyncMock()
        mock.waiting_count = 0
        mock._workers = []
        yield mock


H = {"x-goog-api-key": "secret"}


def _client():
    from src.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _wait_status(c, job_id, want, tries=200):
    for _ in range(tries):
        r = await c.get(f"/api/jobs/{job_id}", headers=H)
        if r.json()["status"] in want:
            return r.json()
        await asyncio.sleep(0.01)
    raise AssertionError(f"job 停在 {r.json()['status']}")


def _fake_dispatch(release: asyncio.Event, result: dict, seen: list):
    """拿到 worker 時呼叫 on_acquire（跟真的 worker_pool 一樣），等 release 才回結果。"""
    async def dispatch(kind, prompt, model, timeout, extra=None, ctx=None):
        seen.append({"kind": kind, "prompt": prompt, "timeout": timeout, "extra": extra})
        ctx["worker_id"] = 0
        ctx["on_acquire"](0)
        await release.wait()
        return result
    return dispatch


@pytest.mark.asyncio
async def test_submit_returns_id_then_poll_gets_audio(pool):
    release, seen = asyncio.Event(), []
    pool.dispatch = _fake_dispatch(release, {"success": True, "audio": "QUJD", "mime": "audio/mpeg",
                                             "prompt": "p", "elapsed_seconds": 1.0}, seen)
    async with _client() as c:
        r = await c.post("/api/music/jobs", headers=H,
                         json={"prompt": "lullaby", "timeout": 720, "vocals": "instrumental"})
        assert r.status_code == 202
        job_id = r.json()["id"]
        assert r.json()["status"] == "queued"
        running = await _wait_status(c, job_id, {"running"})
        assert running["type"] == "music"
        release.set()
        done = await _wait_status(c, job_id, {"succeeded", "failed"})
    assert done["status"] == "succeeded"
    assert done["response"]["audio"] == "QUJD"
    assert done["response"]["mime"] == "audio/mpeg"
    assert done["error"] is None
    # 參數照 /api/music 原樣交給 worker_pool
    assert seen == [{"kind": "music", "prompt": "lullaby", "timeout": 720,
                     "extra": {"options": {"vocals": "instrumental"}}}]


@pytest.mark.asyncio
async def test_generic_jobs_endpoint_accepts_type_music(pool):
    release, seen = asyncio.Event(), []
    release.set()
    pool.dispatch = _fake_dispatch(release, {"success": True, "audio": "QUJD", "mime": "audio/mpeg"}, seen)
    async with _client() as c:
        r = await c.post("/api/jobs", headers=H, json={"type": "music", "prompt": "hymn"})
        assert r.status_code == 202
        done = await _wait_status(c, r.json()["id"], {"succeeded", "failed"})
    assert done["status"] == "succeeded"
    assert seen[0]["prompt"] == "hymn" and seen[0]["timeout"] == 600


@pytest.mark.asyncio
async def test_gemini_failure_marks_job_failed_with_message(pool):
    release, seen = asyncio.Event(), []
    release.set()
    pool.dispatch = _fake_dispatch(release, {"success": False, "error": "content_blocked",
                                             "message": "Gemini 拒絕生成"}, seen)
    async with _client() as c:
        r = await c.post("/api/music/jobs", headers=H, json={"prompt": "x"})
        done = await _wait_status(c, r.json()["id"], {"succeeded", "failed"})
    assert done["status"] == "failed"
    assert "content_blocked" in done["error"] and "拒絕" in done["error"]
    assert done["response"] is None


@pytest.mark.asyncio
async def test_timeout_marks_job_failed(pool):
    pool.dispatch = AsyncMock(side_effect=asyncio.TimeoutError())
    async with _client() as c:
        r = await c.post("/api/music/jobs", headers=H, json={"prompt": "x", "timeout": 5})
        done = await _wait_status(c, r.json()["id"], {"succeeded", "failed"})
    assert done["status"] == "failed" and done["error"].startswith("408")


@pytest.mark.asyncio
async def test_no_music_account_marks_job_failed(pool):
    from src.worker_pool import NoCapableWorkerError
    pool.dispatch = AsyncMock(side_effect=NoCapableWorkerError("沒有帳號"))
    async with _client() as c:
        r = await c.post("/api/music/jobs", headers=H, json={"prompt": "x"})
        done = await _wait_status(c, r.json()["id"], {"succeeded", "failed"})
    assert done["status"] == "failed" and done["error"].startswith("503")


@pytest.mark.asyncio
async def test_bad_option_is_422_at_submit_and_no_job_is_made(pool):
    pool.dispatch = AsyncMock()
    async with _client() as c:
        r = await c.post("/api/music/jobs", headers=H, json={"prompt": "x", "genre": "polka"})
        r2 = await c.post("/api/jobs", headers=H, json={"type": "music", "prompt": "x", "vocals": "loud"})
    assert r.status_code == 422 and "polka" in r.json()["detail"]
    assert r2.status_code == 422 and "loud" in r2.json()["detail"]
    pool.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_requires_key(pool):
    pool.dispatch = AsyncMock()
    async with _client() as c:
        r = await c.post("/api/music/jobs", json={"prompt": "x"})
    assert r.status_code == 403
    pool.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_only_one_song_runs_at_a_time_the_rest_stay_queued(pool):
    release, seen = asyncio.Event(), []
    pool.dispatch = _fake_dispatch(release, {"success": True, "audio": "QUJD", "mime": "audio/mpeg"}, seen)
    async with _client() as c:
        a = (await c.post("/api/music/jobs", headers=H, json={"prompt": "a"})).json()["id"]
        b = (await c.post("/api/music/jobs", headers=H, json={"prompt": "b"})).json()["id"]
        await _wait_status(c, a, {"running"})
        await asyncio.sleep(0.05)
        assert (await c.get(f"/api/jobs/{b}", headers=H)).json()["status"] == "queued"
        assert [s["prompt"] for s in seen] == ["a"]
        release.set()
        assert (await _wait_status(c, a, {"succeeded"}))["status"] == "succeeded"
        assert (await _wait_status(c, b, {"succeeded"}))["status"] == "succeeded"
    assert [s["prompt"] for s in seen] == ["a", "b"]


@pytest.mark.asyncio
async def test_sync_music_endpoint_unchanged(pool):
    pool.dispatch = AsyncMock(return_value={"success": True, "audio": "QUJD", "mime": "audio/mpeg"})
    async with _client() as c:
        r = await c.post("/api/music", headers=H, json={"prompt": "x", "length": "short"})
    assert r.status_code == 200 and r.json()["audio"] == "QUJD"
    assert pool.dispatch.await_args.kwargs["extra"] == {"options": {"length": "short"}}


def test_queued_jobs_are_failed_on_startup():
    a = jobs.create("music", {"prompt": "x"}, "static")
    n = jobs.fail_stale_queued()
    assert n == 1
    row = jobs.get(a, "static")
    assert row["status"] == "failed" and "重啟" in row["error"]
