"""委派工单：父 step 校验、step 收口门、派发者验收。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")

WF_TREE = {
    "steps": [
        {"id": "S1", "title": "执行", "role": "pm", "depends_on": []},
        {"id": "S2", "title": "验证", "role": "developer", "depends_on": ["S1"]},
    ],
}


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer", "other-role"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


def _agent_headers(app, role: str, token: str) -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00",
             "2999-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def _workflow(client, app) -> str:
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-WF", assignees=["pm", "developer"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "委派测试", "source_task": "T-WF", "kind": "workflow", "tree": WF_TREE})
    assert draft.status_code == 201, draft.text
    approved = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text
    wid = (await client.get(f"{API}/tasks/T-WF/workflow",
                            headers=auth_headers())).json()["id"]
    _set_step(app, wid, "S1", "running")
    return wid


def _set_step(app, wid: str, step_id: str, status: str) -> None:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "UPDATE workflow_steps SET status=? WHERE workflow_id=? AND step_id=?",
            (status, wid, step_id))
        conn.commit()
    finally:
        conn.close()


def _set_task_status(app, tid: str, status: str) -> None:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
    finally:
        conn.close()


async def _delegation(client, app, **kw):
    body = {
        "title": "构造 PoC",
        "parent_id": "T-WF",
        "parent_step_id": "S1",
        "assignee": "developer",
        "description": "输入：可疑模块；输出：PoC + 命令 + 输出",
        "acceptance": ["PoC 可复现"],
        "scope_paths": ["poc/"],
    }
    body.update(kw)
    return await client.post(
        f"{API}/tasks", json=body,
        headers=_agent_headers(app, "pm", "tok-delegator-pm"))


async def test_delegator_creates_delegation_with_parent_step(client, app):
    await _seed(client)
    await _workflow(client, app)
    r = await _delegation(client, app, due_at="2026-12-01T18:00:00+08:00")
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parent_id"] == "T-WF"
    assert child["parent_step_id"] == "S1"
    assert child["type"] == "delegation"
    assert child["created_by"] == "agent:pm"
    assert child["repo"] == "main-project"
    assert child["acceptance"] == ["PoC 可复现"]
    assert child["scope_paths"] == ["poc/"]
    assert child["due_at"] == "2026-12-01T18:00:00+08:00"

    listed = await client.get(
        f"{API}/tasks/T-WF/delegations", headers=auth_headers("pm"))
    assert listed.status_code == 200, listed.text
    row = next(x for x in listed.json() if x["id"] == child["id"])
    assert row["step_title"] == "执行"
    assert row["assignee"] == "developer"


async def test_only_step_role_can_create_delegation(client, app):
    await _seed(client)
    await _workflow(client, app)
    r = await client.post(
        f"{API}/tasks", json={
            "title": "越权委派", "parent_id": "T-WF", "parent_step_id": "S1",
            "assignee": "developer",
        },
        headers=_agent_headers(app, "other-role", "tok-other"),
    )
    assert r.status_code == 403


async def test_delegation_requires_running_step_and_parent(client, app):
    await _seed(client)
    await _workflow(client, app)
    _set_step(app, (await client.get(
        f"{API}/tasks/T-WF/workflow", headers=auth_headers())).json()["id"],
        "S1", "pending")
    r = await _delegation(client, app)
    assert r.status_code == 409

    await _seed(client)
    r = await client.post(
        f"{API}/tasks", json={"title": "无父任务", "parent_step_id": "S1"},
        headers=auth_headers("pm"))
    assert r.status_code == 422


async def test_step_submit_waits_for_delegations(client, app):
    await _seed(client)
    wid = await _workflow(client, app)
    child = (await _delegation(client, app)).json()

    blocked = await client.post(
        f"{API}/workflows/{wid}/steps/S1/submit",
        headers=_agent_headers(app, "pm", "tok-delegator-pm"),
        json={"summary": "执行完成"},
    )
    assert blocked.status_code == 409
    assert "delegations not all done" in blocked.text

    _set_task_status(app, child["id"], "done")
    ok = await client.post(
        f"{API}/workflows/{wid}/steps/S1/submit",
        headers=_agent_headers(app, "pm", "tok-delegator-pm"),
        json={"summary": "执行完成"},
    )
    assert ok.status_code == 200, ok.text


async def test_delegator_accepts_but_owner_cannot(client, app):
    await _seed(client)
    await _workflow(client, app)
    child = (await _delegation(client, app)).json()
    _set_task_status(app, child["id"], "in_review")

    own = await client.post(
        f"{API}/tasks/{child['id']}/delegation/accept",
        headers=_agent_headers(app, "developer", "tok-owner-dev"),
    )
    assert own.status_code == 403

    accepted = await client.post(
        f"{API}/tasks/{child['id']}/delegation/accept",
        headers=_agent_headers(app, "pm", "tok-delegator-pm"),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "done"


async def test_delegator_rejects_with_feedback(client, app):
    await _seed(client)
    await _workflow(client, app)
    child = (await _delegation(client, app)).json()
    _set_task_status(app, child["id"], "in_review")

    rejected = await client.post(
        f"{API}/tasks/{child['id']}/delegation/reject",
        headers=_agent_headers(app, "pm", "tok-delegator-pm"),
        json={"feedback": "缺少命令输出"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "in_progress"

    conn = sqlite3.connect(app.state.db_path)
    try:
        mention = conn.execute(
            "SELECT payload_json FROM activities WHERE task_id=? AND kind='mention' "
            "ORDER BY id DESC LIMIT 1", (child["id"],)).fetchone()
    finally:
        conn.close()
    assert mention is not None and "缺少命令输出" in mention[0]


async def test_human_can_accept_delegation(client, app):
    await _seed(client)
    await _workflow(client, app)
    child = (await _delegation(client, app)).json()
    _set_task_status(app, child["id"], "in_review")

    accepted = await client.post(
        f"{API}/tasks/{child['id']}/delegation/accept", headers=human_headers())
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "done"

    conn = sqlite3.connect(app.state.db_path)
    try:
        mention = conn.execute(
            "SELECT payload_json FROM activities WHERE task_id=? AND kind='mention' "
            "ORDER BY id DESC LIMIT 1", (child["id"],)).fetchone()
    finally:
        conn.close()
    assert mention is not None and "委派已验收" in mention[0]


async def test_delegate_submit_notifies_delegator(client, app):
    await _seed(client)
    await _workflow(client, app)
    child = (await _delegation(client, app)).json()
    _set_task_status(app, child["id"], "in_progress")

    submitted = await client.post(
        f"{API}/tasks/{child['id']}/submit",
        headers=_agent_headers(app, "developer", "tok-owner-dev"),
        json={"summary": "PoC 完成"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "in_review"

    conn = sqlite3.connect(app.state.db_path)
    try:
        mention = conn.execute(
            "SELECT payload_json FROM activities WHERE task_id=? AND kind='mention' "
            "ORDER BY id DESC LIMIT 1", (child["id"],)).fetchone()
    finally:
        conn.close()
    assert mention is not None and "委派已提交" in mention[0]


async def test_final_review_waits_for_children(client, app):
    """workflow 已 done 但委派未完成时，父任务不能进入最终评审。"""
    from anvil.db import open_conn
    from anvil.services import workflow as workflow_service

    await _seed(client)
    wid = await _workflow(client, app)
    delegated = await _delegation(client, app)
    assert delegated.status_code == 201, delegated.text
    child = delegated.json()
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
        conn.execute("UPDATE workflows SET status='done' WHERE id=?", (wid,))
        conn.commit()
    finally:
        conn.close()

    conn = open_conn(app.state.db_path)
    try:
        assert workflow_service.finalize_task_review_if_ready(conn, "T-WF") is False
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child["id"],))
        conn.commit()
        assert workflow_service.finalize_task_review_if_ready(conn, "T-WF") is True
        task = conn.execute(
            "SELECT status FROM tasks WHERE id='T-WF'").fetchone()
        assert task["status"] == "in_review"
    finally:
        conn.close()
