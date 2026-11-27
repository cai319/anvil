"""角色建议先于工作流审批的门禁。"""
from __future__ import annotations

import pytest

from anvil.db import open_conn
from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

TREE = {"steps": [
    {"id": "S1", "title": "执行", "role": "pm"},
    {"id": "S2", "title": "开发", "role": "developer", "depends_on": ["S1"]},
]}


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def _task(client, tid: str = "T-GATE"):
    r = await client.post(
        f"{API}/tasks",
        json={**task_payload(id=tid, assignees=["pm"]), "planner": "pm"},
        headers=auth_headers(),
    )
    assert r.status_code == 201, r.text


async def _workflow_draft(client, tid: str) -> dict:
    r = await client.post(
        f"{API}/board/drafts", headers=auth_headers("pm"), json={
            "goal": "门禁工作流", "source_task": tid, "kind": "workflow",
            "tree": TREE,
        })
    assert r.status_code == 201, r.text
    return r.json()


async def _role_proposal(client, tid: str) -> str:
    r = await client.post(
        f"{API}/tasks/{tid}/assign-existing",
        json={"roles": ["pm", "developer"], "reason": "需要开发能力"},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 202, r.text
    return r.json()["draft_id"]


def _assignees(app, tid: str) -> set[str]:
    conn = open_conn(app.state.db_path)
    try:
        return {row["role_slug"] for row in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (tid,))}
    finally:
        conn.close()


def _fake_planner(monkeypatch):
    calls: list[dict] = []

    def fake(conn, db_path, orchestrator, task_id, planner, assignees=None,
             extra_constraint="", replace_active=False):
        calls.append({
            "task_id": task_id, "planner": planner, "assignees": assignees,
            "extra_constraint": extra_constraint, "replace_active": replace_active,
        })
        return {"id": "run-role-approval", "role_slug": planner, "task_id": task_id}

    monkeypatch.setattr("anvil.services.planning.start_planner_run", fake)
    return calls


async def test_workflow_approval_blocked_by_pending_role_suggestion(client, seeded):
    await _task(client)
    draft_id = await _role_proposal(client, "T-GATE")
    wf = await _workflow_draft(client, "T-GATE")

    r = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [wf["id"]]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["impact"]["role_suggestion_blockers"][0]["draft_id"] == draft_id
    # 工作流没有被创建
    workflows = (await client.get(
        f"{API}/tasks/T-GATE/workflows", headers=auth_headers())).json()
    assert workflows == []


async def test_authorize_role_must_be_created_and_bound_before_workflow(
        client, seeded, app, monkeypatch):
    calls = _fake_planner(monkeypatch)
    await _task(client)
    draft_id = await _role_proposal(client, "T-GATE")
    wf = await _workflow_draft(client, "T-GATE")

    approved_role = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft_id]})
    assert approved_role.status_code == 200, approved_role.text
    outcome = approved_role.json()["role_suggestions"][draft_id]
    assert outcome["action"] == "authorized"
    assert "developer" in outcome["needs_planner"]
    assert outcome["wake"] == "started"
    assert calls and calls[0]["replace_active"] is True
    assert "developer" in calls[0]["extra_constraint"]
    assert _assignees(app, "T-GATE") == {"pm"}

    r = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [wf["id"]]})
    assert r.status_code == 409, r.text
    blocker = r.json()["detail"]["impact"]["role_suggestion_blockers"][0]
    assert blocker["reason"] == "authorization_not_consumed"
    assert blocker["unbound_roles"] == ["developer"]

    # planner 完成绑定后，工作流审批才解锁。
    r = await client.post(
        f"{API}/tasks/T-GATE/assign-existing",
        json={"roles": ["pm", "developer"]}, headers=auth_headers("pm"))
    assert r.status_code == 200, r.text
    assert _assignees(app, "T-GATE") == {"pm", "developer"}
    r = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [wf["id"]]})
    assert r.status_code == 200, r.text


async def test_rejected_role_suggestion_unblocks_workflow(client, seeded):
    await _task(client)
    draft_id = await _role_proposal(client, "T-GATE")
    wf = await _workflow_draft(client, "T-GATE")

    r = await client.post(
        f"{API}/board/drafts/reject", headers=human_headers(),
        json={"draft_id": draft_id, "feedback": "不加人"})
    assert r.status_code == 200, r.text

    r = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [wf["id"]]})
    assert r.status_code == 200, r.text


async def test_create_bind_role_suggestion_unblocks_after_approval(client, seeded):
    await _task(client)
    draft = await client.post(
        f"{API}/board/drafts", headers=auth_headers("pm"), json={
            "goal": "绑定开发", "source_task": "T-GATE", "kind": "role_suggestion",
            "tree": {"suggested_roles": [{
                "slug": "developer", "display_name": "开发",
                "description": "负责实现", "runtime": {"id": "claude-code"},
            }]},
        })
    assert draft.status_code == 201, draft.text
    wf = await _workflow_draft(client, "T-GATE")

    blocked = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [wf["id"]]})
    assert blocked.status_code == 409, blocked.text

    approved = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text
    outcome = approved.json()["role_suggestions"][draft.json()["id"]]
    assert outcome["action"] == "bound_existing"
    assert outcome["bound"] == ["developer"]

    ok = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [wf["id"]]})
    assert ok.status_code == 200, ok.text


async def test_unknown_apply_mode_rejected_at_creation(client, seeded):
    await _task(client)
    r = await client.post(
        f"{API}/board/drafts", headers=auth_headers("pm"), json={
            "goal": "错误模式", "source_task": "T-GATE", "kind": "role_suggestion",
            "tree": {"apply_mode": "auto", "suggested_roles": [{
                "slug": "developer", "display_name": "开发"}]},
        })
    assert r.status_code == 409, r.text
    assert "apply_mode" in r.text
