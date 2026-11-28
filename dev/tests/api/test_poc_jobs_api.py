"""PoC 作业 API（M2a/M2b：human 与任务范围内的 agent）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil.db import open_conn
from anvil.services import sandbox
from tests.conftest import API, human_headers, task_payload


def _pm_headers(app) -> dict:
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

SPEC = {
    "network": "none",
    "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
    "artifacts": [{"path": "out/**"}],
}


@pytest.mark.asyncio
async def test_human_create_list_cancel(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    r = await client.post("/api/v1/poc-jobs", headers=human_headers(), json=SPEC)
    assert r.status_code == 201
    job = r.json()
    assert job["status"] == "queued"

    listing = await client.get(
        "/api/v1/poc-jobs", headers=human_headers())
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == job["id"]

    detail = await client.get(
        f"/api/v1/poc-jobs/{job['id']}", headers=human_headers())
    assert detail.status_code == 200
    assert detail.json()["phases"] == []

    canceled = await client.post(
        f"/api/v1/poc-jobs/{job['id']}/cancel", headers=human_headers())
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_agent_unbound_job_denied(client, app, monkeypatch):
    """Agent 只能创建/读取绑定到参与任务的作业。"""
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    headers = _pm_headers(app)
    denied = await client.post(
        "/api/v1/poc-jobs", headers=headers, json=SPEC)
    assert denied.status_code == 403
    listing = await client.get("/api/v1/poc-jobs", headers=headers)
    assert listing.status_code == 403


@pytest.mark.asyncio
async def test_strict_unavailable_returns_409(client, monkeypatch):
    monkeypatch.setattr(
        sandbox, "strict_available", lambda: (False, "docker/podman not found"))
    r = await client.post("/api/v1/poc-jobs", headers=human_headers(), json=SPEC)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "strict_sandbox_unavailable"


@pytest.mark.asyncio
async def test_invalid_spec_returns_400(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    r = await client.post(
        "/api/v1/poc-jobs", headers=human_headers(),
        json={"network": "host", "phases": {"run": [{"cmd": "true"}]}})
    assert r.status_code == 400


CHECK_SPEC = {
    "task_id": "T-POC-CHK",
    "network": "none",
    "check": {
        "id": "c1",
        "kind": "test",
        "cmd": "pytest -q --junit-xml=/out/junit.xml",
        "junit": ["junit.xml"],
    },
}


@pytest.mark.asyncio
async def test_poc_endpoints_do_not_leak_check_jobs(client, monkeypatch):
    """PoC 列表/详情/日志/产物只服务于 kind=poc；检查作业必须走 /check-jobs。"""
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    created = await client.post(
        f"{API}/tasks",
        json=task_payload("T-POC-CHK", assignees=[]), headers=human_headers())
    assert created.status_code == 201, created.text
    poc = (await client.post(
        "/api/v1/poc-jobs", headers=human_headers(),
        json={**SPEC, "task_id": "T-POC-CHK"})).json()
    check = await client.post(
        f"{API}/check-jobs", headers=human_headers(), json=CHECK_SPEC)
    assert check.status_code == 201, check.text
    check_id = check.json()["id"]

    listing = await client.get(
        "/api/v1/poc-jobs?task_id=T-POC-CHK", headers=human_headers())
    assert listing.status_code == 200
    assert [job["id"] for job in listing.json()] == [poc["id"]]
    assert all(job["kind"] == "poc" for job in listing.json())

    for suffix in ("", "/log", "/artifacts", "/evidence"):
        r = await client.get(
            f"/api/v1/poc-jobs/{check_id}{suffix}", headers=human_headers())
        assert r.status_code == 404, (suffix, r.text)


@pytest.mark.asyncio
async def test_artifact_download_rejects_unknown_path(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    job = (await client.post(
        "/api/v1/poc-jobs", headers=human_headers(), json=SPEC)).json()
    r = await client.get(
        f"/api/v1/poc-jobs/{job['id']}/artifacts/../../etc/passwd",
        headers=human_headers())
    assert r.status_code == 404
