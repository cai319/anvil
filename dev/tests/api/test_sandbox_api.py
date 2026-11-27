"""沙箱状态/自检 API（人类）与 run 沙箱视图。"""
from __future__ import annotations

import pytest

from anvil.db import open_conn
from anvil.services import runs as runs_service
from tests.conftest import auth_headers, human_headers


@pytest.mark.asyncio
async def test_sandbox_status_human_only(client):
    r = await client.get("/api/v1/sandbox/status", headers=human_headers())
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] in ("enforce", "host-unsafe")
    assert data["standard"]["backend"]
    assert "warnings" in data

    denied = await client.get(
        "/api/v1/sandbox/status", headers=auth_headers("pm"))
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_sandbox_self_check_records_audit(client):
    r = await client.post("/api/v1/sandbox/self-check", headers=human_headers())
    assert r.status_code == 200
    assert "checks" in r.json()
    # 审计写入不破坏请求
    denied = await client.post(
        "/api/v1/sandbox/self-check", headers=auth_headers("pm"))
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_run_sandbox_view(client, app):
    conn = open_conn(app.state.db_path)
    try:
        run = runs_service.create_run(conn, "qa-role", task_id=None)
    finally:
        conn.close()
    r = await client.get(
        f"/api/v1/runs/{run['id']}/sandbox", headers=human_headers())
    assert r.status_code == 200
    data = r.json()
    assert data["run_id"] == run["id"]
    assert data["instance"] is None
