"""M2b：agent safe 作业、relaxed 审批、任务范围读取。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil.db import open_conn
from anvil.services import sandbox
from tests.conftest import API, human_headers, task_payload

SAFE = {
    "task_id": "T-POC",
    "network": "none",
    "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
}
RELAXED = {**SAFE, "network": "bridge"}


@pytest.fixture
def pm_headers(app):
    """真实 session token（test_bypass=False），让 is_agent 生效。"""
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
        json=task_payload(id_="T-POC", assignees=["pm"]),
        headers=human_headers())
    assert r.status_code == 201
    return r.json()


@pytest.mark.asyncio
async def test_agent_safe_job_and_human_approval(client, task, pm_headers, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    safe = await client.post(
        f"{API}/poc-jobs", json=SAFE, headers=pm_headers)
    assert safe.status_code == 201
    assert safe.json()["status"] == "queued"

    relaxed = await client.post(
        f"{API}/poc-jobs", json=RELAXED, headers=pm_headers)
    assert relaxed.status_code == 201
    job = relaxed.json()
    assert job["status"] == "waiting_approval"

    denied = await client.post(
        f"{API}/poc-jobs/{job['id']}/approve", json={},
        headers=pm_headers)
    assert denied.status_code == 403

    approved = await client.post(
        f"{API}/poc-jobs/{job['id']}/approve", json={"note": "ok"},
        headers=human_headers())
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_agent_read_requires_task_scope(client, task, pm_headers, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    job = (await client.post(
        f"{API}/poc-jobs", json=SAFE, headers=pm_headers)).json()
    listing = await client.get(
        f"{API}/poc-jobs?task_id=T-POC", headers=pm_headers)
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == job["id"]

    denied = await client.get(
        f"{API}/poc-jobs", headers=pm_headers)
    assert denied.status_code == 403

    detail = await client.get(
        f"{API}/poc-jobs/{job['id']}", headers=pm_headers)
    assert detail.status_code == 200


@pytest.mark.asyncio
async def test_agent_cannot_create_unbound_job(client, pm_headers, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    r = await client.post(
        f"{API}/poc-jobs", json={"phases": {"run": [{"cmd": "true"}]}},
        headers=pm_headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_agent_cannot_approve_or_reject(client, task, pm_headers, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    job = (await client.post(
        f"{API}/poc-jobs", json=RELAXED, headers=pm_headers)).json()
    assert job["status"] == "waiting_approval"
    assert (await client.post(
        f"{API}/poc-jobs/{job['id']}/approve", json={},
        headers=pm_headers)).status_code == 403
    assert (await client.post(
        f"{API}/poc-jobs/{job['id']}/reject", json={},
        headers=pm_headers)).status_code == 403
    rejected = await client.post(
        f"{API}/poc-jobs/{job['id']}/reject", json={"note": "no"},
        headers=human_headers())
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
