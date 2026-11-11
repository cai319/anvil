"""POST /tasks/{id}/dispatches：派遣子任务 API。"""
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



def _add_slice(app, tid: str, *, role: str = "qa-role", slice_key: str = "A") -> None:
    from anvil.db import open_conn
    from anvil.util import now_iso

    now = now_iso()
    wid = f"W-{tid}"
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
            "VALUES (?,?,'{}','running',?,?)", (wid, tid, now, now))
        conn.execute(
            "INSERT INTO workflow_steps "
            "(workflow_id,step_id,title,role_slug,status,slice_key,updated_at) "
            "VALUES (?,?,?,?,'pending',?,?)",
            (wid, f"{slice_key}.execute", "执行", role, slice_key, now))
        conn.commit()
    finally:
        conn.close()

async def test_create_and_list_dispatch(client, app):
    await _seed(client)
    task = await client.post(
        f"{API}/tasks",
        json={"title": "来源任务", "assignee": "qa-role",
              "assignees": ["qa-role", "dev-role"]},
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
    assert target["parent_id"] == tid
    assert target["type"] == "dispatch"
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


async def test_human_mention_dispatches_even_when_agent_auto_flag_off(client, app):
    from anvil import config
    from anvil.db import open_conn

    await _seed(client)
    old = config.DISPATCH_AUTO_FROM_MENTION
    config.DISPATCH_AUTO_FROM_MENTION = False
    try:
        task = await client.post(
            f"{API}/tasks",
            json={"title": "来源任务", "assignee": "qa-role",
                  "assignees": ["qa-role", "dev-role"]},
            headers=human_headers(),
        )
        tid = task.json()["id"]
        _add_slice(app, tid)
        resp = await client.post(
            f"{API}/tasks/{tid}/mention",
            json={"target": "dev-role", "message": "完全无关的领域问题",
                  "slice_key": "A"},
            headers=human_headers(),
        )
        assert resp.status_code == 200, resp.text
        conn = open_conn(app.state.db_path)
        try:
            rows = conn.execute(
                "SELECT target_task_id, target_role FROM task_dispatches "
                "WHERE source_task_id=?", (tid,)).fetchall()
            assert len(rows) == 1
            assert rows[0]["target_role"] == "dev-role"
        finally:
            conn.close()
    finally:
        config.DISPATCH_AUTO_FROM_MENTION = old


async def test_agent_auto_dispatch_respects_flag_but_explicit_mode_bypasses(client, app):
    from anvil import config
    from anvil.db import open_conn

    await _seed(client)
    old = config.DISPATCH_AUTO_FROM_MENTION
    config.DISPATCH_AUTO_FROM_MENTION = False
    try:
        task = await client.post(
            f"{API}/tasks",
            json={"title": "来源任务 2", "assignee": "qa-role",
                  "assignees": ["qa-role", "dev-role"]},
            headers=human_headers(),
        )
        tid = task.json()["id"]
        _add_slice(app, tid)
        headers = _agent_session(app, "qa-role", tid)
        resp = await client.post(
            f"{API}/tasks/{tid}/mention",
            json={"target": "dev-role", "message": "无关问题", "slice_key": "A"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        conn = open_conn(app.state.db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM task_dispatches WHERE source_task_id=?",
                (tid,)).fetchone()["n"] == 0
        finally:
            conn.close()

        resp2 = await client.post(
            f"{API}/tasks/{tid}/mention",
            json={"target": "dev-role", "message": "显式独立工作",
                  "slice_key": "A", "context_mode": "dispatch"},
            headers=headers,
        )
        assert resp2.status_code == 200, resp2.text
        conn = open_conn(app.state.db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM task_dispatches WHERE source_task_id=?",
                (tid,)).fetchone()["n"] == 1
        finally:
            conn.close()
    finally:
        config.DISPATCH_AUTO_FROM_MENTION = old
