"""POST /tasks/{id}/dispatches：独立派遣任务 API。"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    for slug in ("qa-role", "dev-role"):
        r = await client.post(
            f"{API}/roles", json=role_payload(slug=slug, display_name=slug))
        assert r.status_code == 201, r.text


def _agent_session(app, role: str, task_id: str) -> dict:
    """真实运行级 token：绕过 test_auth 旁路的 human 化，保留 agent 身份。"""
    from anvil.db import open_conn
    from anvil.util import now_iso

    token = f"tok-{role}"
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (token, role_slug, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?)", (token, role, task_id, now_iso(), None))
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def test_create_and_list_dispatch(client, app):
    await _seed(client)
    task = await client.post(
        f"{API}/tasks",
        json={"title": "来源任务", "assignee": "qa-role"},
        headers=human_headers(),
    )
    assert task.status_code == 201, task.text
    tid = task.json()["id"]
    headers = _agent_session(app, "qa-role", tid)

    resp = await client.post(
        f"{API}/tasks/{tid}/dispatches",
        json={
            "target_role": "dev-role",
            "message": "处理另一个独立领域的问题",
            "slice_key": "A",
            "acceptance": ["可复现", "修复完成"],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    target = data["task"]
    assert target["id"] != tid
    assert target["parent_id"] is None
    assert target["type"] == "dispatched"
    assert target["source_task_id"] == tid
    assert target["source_slice_key"] == "A"
    assert target["requested_by"] == "qa-role"
    assert data["dispatch"]["status"] == "open"
    assert data["run"]["origin"] == "dispatch"
    assert data["run"]["dispatch_id"] == data["dispatch"]["id"]

    listed = await client.get(
        f"{API}/tasks/{tid}/dispatches", headers=headers)
    assert listed.status_code == 200
    assert [x["target_task_id"] for x in listed.json()] == [target["id"]]

    # 同一请求幂等：返回同一张任务卡。
    again = await client.post(
        f"{API}/tasks/{tid}/dispatches",
        json={"target_role": "dev-role",
              "message": "处理另一个独立领域的问题", "slice_key": "A"},
        headers=headers,
    )
    assert again.status_code == 201, again.text
    assert again.json()["reused"] is True
    assert again.json()["task"]["id"] == target["id"]


async def test_dispatch_requires_participant_requester(client, app):
    await _seed(client)
    task = await client.post(
        f"{API}/tasks",
        json={"title": "来源任务", "assignee": "qa-role"},
        headers=human_headers(),
    )
    assert task.status_code == 201, task.text
    tid = task.json()["id"]
    resp = await client.post(
        f"{API}/tasks/{tid}/dispatches",
        json={"target_role": "qa-role", "message": "x"},
        headers=_agent_session(app, "dev-role", tid),
    )
    assert resp.status_code == 409
    assert "participant" in resp.json()["detail"]
