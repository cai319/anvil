"""Phase 0：角色创建子任务的 guardrail。

规则：
- Agent 只能创建子任务（parent_id 必填），且必须是父任务参与角色。
- 子任务继承父任务 workspace_id/repo/base_branch，显式传不同值 → 409。
- human 创建子任务同样不能覆盖结构标记。
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import API, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload(slug="qa-role"))
    await client.post(f"{API}/roles", json=role_payload(slug="other-role"))


def _agent_headers(app, role: str = "qa-role", token: str = "agent-subtask-token") -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def _parent(client, **kw) -> dict:
    payload = task_payload(id_="T-PARENT", repo="main-repo", **kw)
    r = await client.post(f"{API}/tasks", json=payload, headers=human_headers())
    assert r.status_code == 201, r.text
    return r.json()


async def test_agent_can_create_subtask_under_own_task(client, app):
    await _seed(client)
    parent = await _parent(client, assignees=["qa-role"])

    r = await client.post(
        f"{API}/tasks",
        json={"title": "子任务", "parent_id": parent["id"]},
        headers=_agent_headers(app),
    )
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parent_id"] == parent["id"]
    assert child["repo"] == parent["repo"] == "main-repo"
    assert child["workspace_id"] == parent["workspace_id"]
    assert child["base_branch"] == parent["base_branch"]
    assert child["created_by"] == "agent:qa-role"
    conn = sqlite3.connect(app.state.db_path)
    try:
        audit = conn.execute(
            "SELECT action FROM audit WHERE task_id=? AND action='create'", (child["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert audit is not None


async def test_agent_cannot_create_top_level_task(client, app):
    await _seed(client)
    r = await client.post(
        f"{API}/tasks",
        json={"title": "顶层任务"},
        headers=_agent_headers(app),
    )
    assert r.status_code == 403


async def test_agent_cannot_create_under_foreign_task(client, app):
    await _seed(client)
    parent = await _parent(client, assignees=["other-role"])
    r = await client.post(
        f"{API}/tasks",
        json={"title": "越权子任务", "parent_id": parent["id"]},
        headers=_agent_headers(app),
    )
    assert r.status_code == 403


async def test_agent_cannot_override_parent_repo(client, app):
    await _seed(client)
    parent = await _parent(client, assignees=["qa-role"])
    r = await client.post(
        f"{API}/tasks",
        json={"title": "改 repo 子任务", "parent_id": parent["id"], "repo": "other-repo"},
        headers=_agent_headers(app),
    )
    assert r.status_code == 409


async def test_agent_cannot_override_parent_workspace_or_branch(client, app):
    await _seed(client)
    ws = await client.post(
        f"{API}/workspaces", json={"name": "main", "repo": "main-repo"}, headers=human_headers()
    )
    assert ws.status_code == 201, ws.text
    parent = await _parent(
        client,
        assignees=["qa-role"],
        workspace_id=ws.json()["id"],
        base_branch="main",
    )
    headers = _agent_headers(app)
    r1 = await client.post(
        f"{API}/tasks",
        json={"title": "改 workspace", "parent_id": parent["id"], "workspace_id": "other-ws"},
        headers=headers,
    )
    assert r1.status_code == 409
    r2 = await client.post(
        f"{API}/tasks",
        json={"title": "改 branch", "parent_id": parent["id"], "base_branch": "dev"},
        headers=headers,
    )
    assert r2.status_code == 409


async def test_human_subtask_cannot_override_parent_repo(client):
    await _seed(client)
    parent = await _parent(client)
    r = await client.post(
        f"{API}/tasks",
        json={"title": "human 改 repo", "parent_id": parent["id"], "repo": "other-repo"},
        headers=human_headers(),
    )
    assert r.status_code == 409


async def test_human_subtask_inherits_parent_markers(client):
    await _seed(client)
    parent = await _parent(client)
    r = await client.post(
        f"{API}/tasks",
        json={"title": "human 子任务", "parent_id": parent["id"]},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["repo"] == parent["repo"]
    assert child["workspace_id"] == parent["workspace_id"]
    assert child["base_branch"] == parent["base_branch"]
