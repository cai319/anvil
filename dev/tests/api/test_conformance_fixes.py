"""回归：HITL/隔离/实践门控与 workflow 提交边界（2026-10-29 review）。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil import config
from anvil.db import open_conn
from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client, roles=("qa-role", "dev")):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in roles:
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


def _agent_headers(app, role: str = "qa-role", token: str = "conformance-agent-token") -> dict:
    """真实运行级会话（test_bypass=False），用于验证 Agent 身份分支。"""
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


async def _make_done_task(client, tid: str = "T-DONE", role: str = "dev"):
    r = await client.post(f"{API}/tasks", json=task_payload(id_=tid), headers=auth_headers())
    assert r.status_code == 201, r.text
    await client.patch(f"{API}/tasks/{tid}", json={"assignees": [role]}, headers=human_headers())
    assert (await client.post(f"{API}/tasks/{tid}/claim", json={},
                              headers=auth_headers(role))).status_code == 200
    assert (await client.post(f"{API}/tasks/{tid}/submit", json={"summary": "done"},
                              headers=auth_headers(role))).status_code == 200
    assert (await client.post(f"{API}/tasks/{tid}/review", json={"approve": True},
                              headers=human_headers())).status_code == 200


# ---- Reminder 只能由本人或人类管理 --------------------------------------


async def test_agent_cannot_create_reminder_for_another_role(client):
    await _seed(client)
    r = await client.post(f"{API}/reminders", json={
        "role_slug": "dev", "kind": "at",
        "fire_at": "2030-01-01T00:00:00+00:00", "prompt": "代替别人排提醒",
    }, headers=auth_headers("qa-role"))
    assert r.status_code == 403, r.text


async def test_agent_cannot_read_update_delete_or_fire_another_roles_reminder(client):
    await _seed(client)
    created = await client.post(f"{API}/reminders", json={
        "role_slug": "dev", "kind": "at",
        "fire_at": "2030-01-01T00:00:00+00:00", "prompt": "dev 的提醒",
    }, headers=human_headers())
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    listed = await client.get(f"{API}/reminders", headers=auth_headers("qa-role"))
    assert listed.status_code == 200
    assert all(item["role_slug"] == "qa-role" for item in listed.json())

    assert (await client.put(f"{API}/reminders/{rid}", json={"prompt": "偷改"},
                             headers=auth_headers("qa-role"))).status_code == 403
    assert (await client.delete(f"{API}/reminders/{rid}",
                                headers=auth_headers("qa-role"))).status_code == 403
    assert (await client.post(f"{API}/reminders/{rid}/fire-now",
                              headers=auth_headers("qa-role"))).status_code == 403


async def test_when_reminder_does_not_fire_for_cancelled_task(client):
    await _seed(client)
    r = await client.post(f"{API}/tasks", json=task_payload(id_="T-WHEN-CANCEL"),
                          headers=auth_headers())
    assert r.status_code == 201, r.text
    assert (await client.post(f"{API}/tasks/T-WHEN-CANCEL/cancel",
                              headers=human_headers())).status_code == 200
    r = await client.post(f"{API}/reminders", json={
        "role_slug": "qa-role", "kind": "when",
        "when_condition": "T-WHEN-CANCEL", "prompt": "取消任务不应触发"
    }, headers=human_headers())
    assert r.status_code == 201, r.text

    assert (await client.post(f"{API}/control/tick", headers=human_headers())).status_code == 200
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert not any(run["prompt"] == "取消任务不应触发" for run in runs)


async def test_when_reminder_fires_for_done_task(client):
    await _seed(client)
    await _make_done_task(client, "T-WHEN-DONE")
    r = await client.post(f"{API}/reminders", json={
        "role_slug": "qa-role", "kind": "when",
        "when_condition": "task:T-WHEN-DONE:done", "prompt": "完成提醒"
    }, headers=human_headers())
    assert r.status_code == 201, r.text

    assert (await client.post(f"{API}/control/tick", headers=human_headers())).status_code == 200
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert any(run["prompt"] == "完成提醒" for run in runs)


# ---- 实践证据：取消的任务/别人的任务都不能算成功 -------------------------


async def test_cancelled_task_is_not_practice_evidence(client, app):
    await _seed(client)
    r = await client.post(f"{API}/tasks", json=task_payload(id_="T-PRAC-CANCEL"),
                          headers=auth_headers())
    assert r.status_code == 201, r.text
    assert (await client.post(f"{API}/tasks/T-PRAC-CANCEL/claim", json={},
                              headers=auth_headers("qa-role"))).status_code == 200
    assert (await client.post(f"{API}/tasks/T-PRAC-CANCEL/cancel",
                              headers=human_headers())).status_code == 200

    r = await client.post(f"{API}/me/memory", json={
        "content": "取消任务里总结的经验",
        "practice_task_id": "T-PRAC-CANCEL", "outcome": "未完成",
    }, headers=_agent_headers(app, token="practice-cancel-token"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"


async def test_another_roles_done_task_is_not_practice_evidence(client, app):
    await _seed(client)
    await _make_done_task(client, "T-OTHERS-DONE", role="dev")

    r = await client.post(f"{API}/me/memory", json={
        "content": "借用别人的成功任务",
        "practice_task_id": "T-OTHERS-DONE", "outcome": "别人做的",
    }, headers=_agent_headers(app, token="practice-other-token"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"


# ---- Agent reindex 不得绕过实践门控 -------------------------------------


async def test_agent_reindex_keeps_unverified_note_as_draft(client, app, tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    notes = agents / "qa-role" / "notes"
    notes.mkdir(parents=True)
    (notes / "unverified.md").write_text("手工塞入的未验证知识", encoding="utf-8")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)

    headers = _agent_headers(app, token="reindex-agent-token")
    r = await client.post(f"{API}/me/knowledge/reindex", headers=headers)
    assert r.status_code == 200, r.text

    conn = sqlite3.connect(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM notes WHERE role_slug='qa-role' AND path='unverified.md'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("draft",)
    hits = (await client.get(f"{API}/me/knowledge/search?q=unverified",
                             headers=headers)).json()
    assert hits["results"] == []


# ---- 手动派发 run 是人类动作，Agent 不能替其它角色拉起进程 ----------------


async def test_agent_cannot_start_run_for_another_role(client, app, monkeypatch):
    from anvil.platform import factory
    from tests.graph.conftest import FakePM

    monkeypatch.setattr(factory, "get_platform", lambda: (FakePM(), None))
    await _seed(client)
    r = await client.post(f"{API}/tasks", json=task_payload(id_="T-RUN-OTHER"),
                          headers=auth_headers())
    assert r.status_code == 201, r.text
    await client.patch(f"{API}/tasks/T-RUN-OTHER", json={"assignees": ["dev"]},
                       headers=human_headers())

    r = await client.post(f"{API}/runs", json={
        "role_slug": "dev", "task_id": "T-RUN-OTHER",
    }, headers=_agent_headers(app, role="dev", token="run-agent-token"))
    assert r.status_code == 403, r.text


# ---- 派发/规划是人类的编排动作，Agent 只能通过 @planner 请求 ---------------


async def test_agent_cannot_dispatch_or_plan_task(client, app, monkeypatch):
    from anvil.platform import factory
    from tests.graph.conftest import FakePM

    monkeypatch.setattr(factory, "get_platform", lambda: (FakePM(), None))
    await _seed(client)
    r = await client.post(f"{API}/tasks", json={
        **task_payload(id_="T-AGENT-DISPATCH"),
        "assignees": ["dev"], "planner": "dev",
    }, headers=auth_headers())
    assert r.status_code == 201, r.text
    headers = _agent_headers(app, role="dev", token="dispatch-agent-token")

    assert (await client.post(f"{API}/tasks/T-AGENT-DISPATCH/dispatch",
                              headers=headers)).status_code == 403
    assert (await client.post(f"{API}/tasks/T-AGENT-DISPATCH/plan",
                              headers=headers)).status_code == 403


# ---- 只读数据按任务参与范围收紧 ------------------------------------------


async def test_agent_reads_are_scoped_to_participating_tasks(client, app):
    await _seed(client)
    for tid, assignees in (("T-READ-DEV", ["dev"]), ("T-READ-OPEN", [])):
        r = await client.post(f"{API}/tasks", json={
            **task_payload(id_=tid), "assignees": assignees,
        }, headers=auth_headers())
        assert r.status_code == 201, r.text
    headers = _agent_headers(app, token="read-agent-token")

    assert (await client.get(f"{API}/tasks/T-READ-DEV/timeline",
                             headers=headers)).status_code == 403
    assert (await client.get(f"{API}/tasks/T-READ-OPEN/timeline",
                             headers=headers)).status_code == 200
    assert (await client.get(f"{API}/tasks/T-READ-DEV/diff",
                             headers=headers)).status_code == 403

    conn = sqlite3.connect(app.state.db_path)
    try:
        for rid, role, tid in (("R-DEV", "dev", "T-READ-DEV"),
                               ("R-QA", "qa-role", "T-READ-OPEN")):
            conn.execute(
                "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
                "VALUES (?,?,?,?,?)", (rid, role, tid, "completed",
                                       "2026-01-01T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()

    assert (await client.get(f"{API}/runs/R-DEV", headers=headers)).status_code == 403
    assert (await client.get(f"{API}/runs/R-QA", headers=headers)).status_code == 200
    listed = (await client.get(f"{API}/runs", headers=headers)).json()
    assert {run["id"] for run in listed} == {"R-QA"}


async def test_agent_workspace_reads_require_a_participating_task(client, app):
    await _seed(client)
    ws = (await client.post(f"{API}/workspaces", json={"name": "scoped"},
                            headers=human_headers())).json()
    r = await client.post(f"{API}/tasks", json={
        **task_payload(id_="T-WS-DEV"), "workspace_id": ws["id"],
        "assignees": ["dev"],
    }, headers=auth_headers())
    assert r.status_code == 201, r.text
    headers = _agent_headers(app, token="ws-read-agent-token")
    assert (await client.get(f"{API}/workspaces/{ws['id']}/tree",
                             headers=headers)).status_code == 403

    r = await client.post(f"{API}/tasks", json={
        **task_payload(id_="T-WS-QA"), "workspace_id": ws["id"],
        "assignees": ["qa-role"],
    }, headers=auth_headers())
    assert r.status_code == 201, r.text
    assert (await client.get(f"{API}/workspaces/{ws['id']}/tree",
                             headers=headers)).status_code == 200


async def test_agent_cannot_read_global_audit(client, app):
    await _seed(client)
    r = await client.get(f"{API}/audit",
                         headers=_agent_headers(app, role="dev", token="audit-agent-token"))
    assert r.status_code == 403, r.text


# ---- workflow 任务禁止用 task submit 绕过 step 状态机 --------------------


async def test_agent_cannot_task_submit_task_with_running_workflow(client, app):
    await _seed(client)
    r = await client.post(f"{API}/tasks", json=task_payload(id_="T-WF-SUBMIT"),
                          headers=auth_headers())
    assert r.status_code == 201, r.text
    await client.patch(f"{API}/tasks/T-WF-SUBMIT", json={"assignees": ["qa-role"]},
                       headers=human_headers())
    assert (await client.post(f"{API}/tasks/T-WF-SUBMIT/claim", json={},
                              headers=auth_headers("qa-role"))).status_code == 200
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id, task_id, definition_json, status, created_at, updated_at) "
            "VALUES ('wf-submit-guard', 'T-WF-SUBMIT', '{}', 'running', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    r = await client.post(f"{API}/tasks/T-WF-SUBMIT/submit", json={"summary": "想绕过步骤"},
                          headers=auth_headers("qa-role"))
    assert r.status_code == 409, r.text
    assert "step submit" in r.text
