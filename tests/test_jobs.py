"""job API 的測試。用 monkeypatch 把 DB 指到 tmp_path，不碰真的 admin.db。"""
import json

import pytest

from src import jobs


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "admin.db")


def test_create_returns_queued_job():
    jid = jobs.create("gemini-2.5-flash-image", {"contents": []}, "tester")
    assert jid.startswith("job_")
    row = jobs.get(jid, "tester")
    assert row["status"] == "queued"
    assert row["response"] is None
    assert row["error"] is None


def test_get_is_scoped_to_the_key_that_created_it():
    jid = jobs.create("m", {"contents": []}, "owner")
    assert jobs.get(jid, "owner") is not None
    assert jobs.get(jid, "someone-else") is None


def test_get_unknown_id_is_none():
    assert jobs.get("job_deadbeef", "tester") is None


def test_finish_stores_the_response_verbatim():
    jid = jobs.create("m", {"contents": []}, "tester")
    jobs.mark_running(jid)
    assert jobs.get(jid, "tester")["status"] == "running"
    payload = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    jobs.finish(jid, payload)
    row = jobs.get(jid, "tester")
    assert row["status"] == "succeeded"
    assert row["response"] == payload
    assert row["error"] is None


def test_fail_stores_the_reason():
    jid = jobs.create("m", {"contents": []}, "tester")
    jobs.fail(jid, "upstream 500")
    row = jobs.get(jid, "tester")
    assert row["status"] == "failed"
    assert row["error"] == "upstream 500"
    assert row["response"] is None


def test_stale_running_jobs_are_failed_on_startup():
    """服務重啟時，進行中的 job 永遠不會完成。把它標成 failed，
    不要讓消費端輪詢一個永遠停在 running 的 job。"""
    a = jobs.create("m", {"contents": []}, "tester")
    b = jobs.create("m", {"contents": []}, "tester")
    jobs.mark_running(a)
    n = jobs.fail_stale_running()
    assert n == 1
    assert jobs.get(a, "tester")["status"] == "failed"
    assert "重啟" in jobs.get(a, "tester")["error"]
    assert jobs.get(b, "tester")["status"] == "queued"


def test_body_survives_a_round_trip():
    body = {"contents": [{"parts": [{"text": "x" * 1000}]}], "generationConfig": {"responseModalities": ["IMAGE"]}}
    jid = jobs.create("m", body, "tester")
    assert json.loads(jobs.get(jid, "tester")["body_json"]) == body
