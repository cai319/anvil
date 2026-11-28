"""检查作业 API：任务绑定、不要求 finding、标准档不依赖严格档（v1.62 #2）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil.db import open_conn
from anvil.services import sandbox
from tests.conftest import API, human_headers, task_payload

CHECK = {
    "task_id": "T-CHK",
    "network": "none",
    "check": {
        "id": "c1",
        "kind": "test",
        "cmd": "pytest -q --junit-xml=/out/junit.xml",
        "junit": ["junit.xml"],
    },
}


@pytest.fixture
def pm_headers(app):
    now = datetime.now(timezone.utc)
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            ("pm-token", "pm", None, None, now.isoformat(),
             (now + timedelta(days=1)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": "pm-token", "X-Anvil-Role": "pm"}


@pytest.fixture
async def task(client):
    r = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-CHK", assignees=["pm"]),
        headers=human_headers())
    assert r.status_code == 201
    return r.json()


@pytest.mark.asyncio
async def test_human_create_check_without_finding(client, task):
    r = await client.post(
        f"{API}/check-jobs", json=CHECK, headers=human_headers())
    assert r.status_code == 201
    job = r.json()
    assert job["kind"] == "check"
    assert job["finding_id"] is None
    assert job["status"] == "queued"
    assert job["tier"] == "standard"

    listing = await client.get(
        f"{API}/check-jobs?task_id=T-CHK", headers=human_headers())
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == job["id"]

    detail = await client.get(
        f"{API}/check-jobs/{job['id']}", headers=human_headers())
    assert detail.status_code == 200
    body = detail.json()
    assert body["phases"] == []
    assert body["check_result"] is None


@pytest.mark.asyncio
async def test_check_job_does_not_require_strict_backend(client, task, monkeypatch):
    monkeypatch.setattr(
        sandbox, "strict_available", lambda: (False, "no docker"))
    r = await client.post(
        f"{API}/check-jobs", json=CHECK, headers=human_headers())
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_agent_check_requires_bound_task(client, task, pm_headers):
    unbound = {k: v for k, v in CHECK.items() if k != "task_id"}
    denied = await client.post(
        f"{API}/check-jobs", json=unbound, headers=pm_headers)
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/check-jobs", json=CHECK, headers=pm_headers)
    assert allowed.status_code == 201
    assert allowed.json()["status"] == "queued"

    relaxed = await client.post(
        f"{API}/check-jobs", json={**CHECK, "network": "bridge"},
        headers=pm_headers)
    assert relaxed.status_code == 201
    assert relaxed.json()["status"] == "waiting_approval"


@pytest.mark.asyncio
async def test_invalid_check_spec_returns_400(client, task):
    r = await client.post(
        f"{API}/check-jobs",
        json={"task_id": "T-CHK", "check": {"kind": "test", "cmd": "x",
                                           "junit": ["../x"]}},
        headers=human_headers())
    assert r.status_code == 400
