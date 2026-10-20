"""临时子任务：agent 创建、继承父任务仓库、可被其他角色认领、不进大厅（parent 过滤）。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, role_payload, runtime_payload, task_payload  # noqa: E402


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def test_agent_creates_subtask_and_other_claims(client, seeded):
    parent = await client.post(f"{API}/tasks", json=task_payload(
        id="T-PARENT", repo="main", base_branch="main"), headers=auth_headers())
    assert parent.status_code == 201

    child = await client.post(f"{API}/tasks", headers=auth_headers("pm"), json={
        "title": "临时调研：AI 寻路", "description": "为 S4 调研 A* 可行性",
        "parent_id": "T-PARENT", "created_by": "agent:pm"})
    assert child.status_code == 201, child.text
    body = child.json()
    assert body["parent_id"] == "T-PARENT"
    assert body["repo"] == "main"          # 继承父任务仓库
    assert body["workspace_id"] == parent.json()["workspace_id"]

    # 子任务列表过滤
    kids = (await client.get(f"{API}/tasks?parent_id=T-PARENT", headers=auth_headers())).json()
    assert [t["id"] for t in kids] == [body["id"]]

    # 另一个角色可以认领
    r = await client.post(f"{API}/tasks/{body['id']}/claim", json={},
                          headers=auth_headers("developer"))
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "developer"
