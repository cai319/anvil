"""P1.12：TaskOut/RunOut 嵌套契约模型不裁剪既有响应字段。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.util import now_iso
from tests.conftest import (
    API, auth_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post(
            f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def test_task_out_keeps_nested_contract_fields(client):
    await _seed(client)
    created = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-CONTRACT", assignees=["pm", "developer"]),
        headers=auth_headers())
    assert created.status_code == 201, created.text

    got = await client.get(
        f"{API}/tasks/T-CONTRACT", headers=auth_headers("pm"))
    assert got.status_code == 200, got.text
    task = got.json()
    assert set(task["task_workspace"]) >= {"source_repo", "path", "kind", "status"}
    assert task["workspace"] is None or set(task["workspace"]) >= {
        "id", "name", "repo"}
    claim = next(c for c in task["role_claims"] if c["role_slug"] == "pm")
    assert set(claim) >= {
        "role_slug", "status", "summary", "run_id", "claimed_at",
        "lease_expires_at", "submitted_at", "release_requested_at",
    }
    assert isinstance(task["knowledge_repos"], list)
    assert set(task["depends_on"]) == set()


async def test_run_out_keeps_all_lifecycle_fields(client, app):
    await _seed(client)
    created = await client.post(
        f"{API}/tasks", json=task_payload(id_="T-RUN", assignees=["pm"]),
        headers=auth_headers())
    assert created.status_code == 201, created.text
    now = now_iso()
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
            "VALUES ('R-CONTRACT','pm','T-RUN','running',?)", (now,))
        conn.commit()
    finally:
        conn.close()

    got = await client.get(
        f"{API}/runs/R-CONTRACT", headers=auth_headers("pm"))
    assert got.status_code == 200, got.text
    run = got.json()
    assert run["status"] == "running"
    for key in ("profile_snapshot", "worktree_path", "native_session_id",
                "log_path", "prompt", "step_id", "workflow_id",
                "failure_class", "slice_key", "dispatch_id", "origin",
                "context_return_context_key", "context_return_run_id",
                "context_return_after_turn", "context_returned_at",
                "recovery_count", "next_retry_at", "recovered_by",
                "recovery_of_run_id", "recovery_exhausted",
                "context_return_attempts", "context_return_last_attempt_at",
                "context_return_armed_at"):
        assert key in run

    listed = await client.get(
        f"{API}/runs?task_id=T-RUN", headers=auth_headers("pm"))
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["id"] == "R-CONTRACT"
