"""@ 唤醒回归：子任务/未绑定角色/工作流非参与角色都必须能被 @ 唤醒。"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

pytest.importorskip("anvil.main")

from anvil.db import open_conn  # noqa: E402
from anvil.services import workflow as workflow_service  # noqa: E402
from anvil.util import now_iso  # noqa: E402
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)


class _Orch:
    def __init__(self):
        self.pumped = []

    def pump(self, db_path, run_id, event, payload=None):
        self.pumped.append((run_id, event, payload or {}))

    def resume_review(self, db_path, run_id, approve):
        return False


@pytest.fixture
async def seeded(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "report-collector"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    app.state.orchestrator = _Orch()
    return app.state.orchestrator


def _runs(app, task_id, role):
    conn = open_conn(app.state.db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, role_slug, status, prompt FROM runs "
            "WHERE task_id=? AND role_slug=? ORDER BY started_at", (task_id, role))]
    finally:
        conn.close()


async def test_mention_wakes_unbound_role_on_plain_task(client, app, seeded):
    """子任务只有 assignee、没有 task_assignees 时，@ 其它现有角色仍要能唤醒。

    普通任务/子任务起普通 run（prompt 为空，由任务协议驱动 claim）。
    """
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-PLAIN", assignee="pm"))
    assert r.status_code == 201, r.text

    m = await client.post(f"{API}/tasks/T-MENTION-PLAIN/mention",
                          headers=auth_headers("pm"),
                          json={"target": "report-collector", "message": "请认领子任务"})
    assert m.status_code == 200, m.text
    runs = _runs(app, "T-MENTION-PLAIN", "report-collector")
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
    assert runs[0]["prompt"] is None  # 普通 run，不是 reply run
    assert any(e == "wake" for _, e, _ in seeded.pumped)


async def test_mention_on_workflow_task_creates_reply_run(client, app, seeded):
    """工作流任务里 @ 非工作流角色 → 回复 run，不越权改 workflow 结构。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-WF", assignee="pm"))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        workflow_service.create_workflow(conn, "T-MENTION-WF", {
            "steps": [{"id": "S1", "title": "x", "role": "pm"}]})
    finally:
        conn.close()

    m = await client.post(f"{API}/tasks/T-MENTION-WF/mention",
                          headers=auth_headers("pm"),
                          json={"target": "report-collector", "message": "请复核报告口径"})
    assert m.status_code == 200, m.text
    runs = _runs(app, "T-MENTION-WF", "report-collector")
    assert len(runs) == 1
    assert str(runs[0]["prompt"]).startswith("[anvil:reply]")


async def test_mention_resumes_suspended_run(client, app, seeded):
    """目标角色已有挂起 run → 直接 resume，不新建 run。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-RESUME", assignee="pm"))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
            "VALUES ('R-SUSP','report-collector','T-MENTION-RESUME','suspended','2026-01-01T00:00:00+00:00')")
        conn.commit()
    finally:
        conn.close()

    m = await client.post(f"{API}/tasks/T-MENTION-RESUME/mention",
                          headers=auth_headers("pm"),
                          json={"target": "report-collector", "message": "请继续"})
    assert m.status_code == 200, m.text
    assert seeded.pumped == [("R-SUSP", "resume", {})]
    assert _runs(app, "T-MENTION-RESUME", "report-collector")[0]["status"] == "suspended"


async def test_human_mention_cindy_starts_planner_run(client, app, seeded):
    """自然人 @Cindy：交给有规划职责的角色处理（plan run），不是系统替人决策。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-HUMAN-CINDY", assignee="pm"))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        now = now_iso()
        conn.execute(
            "INSERT OR IGNORE INTO roles (slug, display_name, home_path, definition_json, "
            "created_at, updated_at, enabled) VALUES (?,?,?,?,?,?,1)",
            ("cindy", "Cindy", os.path.join(tempfile.gettempdir(), "cindy"),
             json.dumps({"kind": "onboarding"}), now, now))
        conn.commit()
        workflow_service.create_workflow(conn, "T-HUMAN-CINDY", {
            "steps": [{"id": "S1", "title": "x", "role": "pm"}]})
    finally:
        conn.close()

    m = await client.post(f"{API}/tasks/T-HUMAN-CINDY/mention",
                          headers=human_headers(),
                          json={"target": "cindy", "message": "通过，请总结并答复"})
    assert m.status_code == 200, m.text
    runs = _runs(app, "T-HUMAN-CINDY", "cindy")
    assert len(runs) == 1
    prompt = str(runs[0]["prompt"])
    assert prompt.startswith("[anvil:plan]")
    assert "通过，请总结并答复" in prompt
    assert any(e == "wake" for _, e, _ in seeded.pumped)


async def test_reject_workflow_draft_wakes_its_author(client, app, seeded):
    """驳回 workflow 草案必须唤醒作者（Cindy）重新规划，而不是只写一条 mention。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-REJECT-WAKE", assignee="pm"))
    assert r.status_code == 201, r.text
    created = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "重新规划", "source_task": "T-REJECT-WAKE", "kind": "workflow",
        "created_by": "agent:cindy",
        "tree": {"steps": [{"id": "S1", "title": "x", "role": "pm"}]}})
    assert created.status_code == 201, created.text
    rejected = await client.post(f"{API}/board/drafts/reject", headers=human_headers(),
                                 json={"draft_id": created.json()["id"],
                                       "feedback": "补 subagent_hint 后重提"})
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["replan"]["planner"] == "cindy"
    runs = _runs(app, "T-REJECT-WAKE", "cindy")
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
    assert str(runs[0]["prompt"]).startswith("[anvil:plan]")
    assert "补 subagent_hint 后重提" in str(runs[0]["prompt"])
    assert any(e == "wake" for _, e, _ in seeded.pumped)


async def test_mention_pseudo_role_never_wakes(client, app, seeded):
    """workflow/system 是系统伪发送者；即使历史数据里有同名 role 行也不能被 @ 唤醒。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-PSEUDO", assignee="pm"))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO roles (slug, display_name, home_path, definition_json, "
            "created_at, updated_at, enabled) VALUES "
            "('workflow','Workflow',?,'{}','2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00',1)",
            (os.path.join(tempfile.gettempdir(), "workflow"),))
        conn.commit()
    finally:
        conn.close()
    m = await client.post(f"{API}/tasks/T-MENTION-PSEUDO/mention",
                          headers=auth_headers("pm"),
                          json={"target": "workflow", "message": "在吗"})
    assert m.status_code == 200, m.text
    assert _runs(app, "T-MENTION-PSEUDO", "workflow") == []
    assert seeded.pumped == []


async def test_mention_unknown_role_only_records_text(client, app, seeded):
    """不存在的角色只记录 mention，不创建 run。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-GHOST", assignee="pm"))
    assert r.status_code == 201, r.text
    m = await client.post(f"{API}/tasks/T-MENTION-GHOST/mention",
                          headers=auth_headers("pm"),
                          json={"target": "ghost-role", "message": "在吗"})
    assert m.status_code == 200, m.text
    assert _runs(app, "T-MENTION-GHOST", "ghost-role") == []
    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM activities WHERE task_id='T-MENTION-GHOST' AND kind='mention' "
            "AND json_extract(payload_json,'$.target')='ghost-role'").fetchone()
    finally:
        conn.close()
    assert row is not None


async def test_mention_prefers_running_step_run_over_newer_reply(client, app, seeded):
    """步骤 run 仍 running 时，mention 必须 resume 它，而不是最新的 reply run。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-STEP", assignee="pm"))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        wf = workflow_service.create_workflow(conn, "T-MENTION-STEP", {
            "steps": [{"id": "S1", "title": "x", "role": "report-collector"}]})
        now = now_iso()
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,workflow_id,step_id,"
            "started_at) VALUES ('R-STEP','report-collector','T-MENTION-STEP',"
            "'suspended',?,'S1','2026-01-01T00:00:00+00:00')", (wf["id"],))
        # 更新、但不是步骤 run 的 reply run：旧逻辑会选中它。
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,started_at) "
            "VALUES ('R-REPLY','report-collector','T-MENTION-STEP','suspended',?)",
            ("2026-02-01T00:00:00+00:00",))
        conn.execute(
            "UPDATE workflow_steps SET status='running', run_id='R-STEP' "
            "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
        conn.commit()
    finally:
        conn.close()

    m = await client.post(f"{API}/tasks/T-MENTION-STEP/mention",
                          headers=auth_headers("pm"),
                          json={"target": "report-collector", "message": "请复核 S1 产出"})
    assert m.status_code == 200, m.text

    pumped = [rid for rid, event, _ in seeded.pumped if event == "resume"]
    assert "R-STEP" in pumped
    assert "R-REPLY" not in pumped
    conn = open_conn(app.state.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE task_id='T-MENTION-STEP'"
        ).fetchone()["n"] == 2  # 没有新建 run
    finally:
        conn.close()


async def test_mention_on_created_step_run_wakes_it_without_reply_run(client, app, seeded):
    """步骤 run 尚未启动（created）：mention 作为 wake_note 带过去，不另建 reply run。"""
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-MENTION-CREATED", assignee="pm"))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        wf = workflow_service.create_workflow(conn, "T-MENTION-CREATED", {
            "steps": [{"id": "S1", "title": "x", "role": "report-collector"}]})
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,workflow_id,step_id,"
            "started_at) VALUES ('R-CREATED','report-collector','T-MENTION-CREATED',"
            "'created',?,'S1','2026-01-01T00:00:00+00:00')", (wf["id"],))
        conn.execute(
            "UPDATE workflow_steps SET status='running', run_id='R-CREATED' "
            "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
        conn.commit()
    finally:
        conn.close()

    m = await client.post(f"{API}/tasks/T-MENTION-CREATED/mention",
                          headers=auth_headers("pm"),
                          json={"target": "report-collector", "message": "请先看接口契约"})
    assert m.status_code == 200, m.text

    assert ("R-CREATED", "wake") in [
        (rid, event) for rid, event, _ in seeded.pumped]
    conn = open_conn(app.state.db_path)
    try:
        run = conn.execute(
            "SELECT wake_note FROM runs WHERE id='R-CREATED'").fetchone()
        assert "请先看接口契约" in (run["wake_note"] or "")
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE task_id='T-MENTION-CREATED'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


async def test_governance_suppressed_dispatch_does_not_fallback_reply(client, app, seeded):
    """frozen/budget 下不能把 dispatch 降级为 reply run 绕过治理闸门。"""
    from anvil.services import governance
    from anvil.db import open_conn

    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json=task_payload(id="T-GOV-DISP",
                                            assignees=["pm", "report-collector"]))
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        governance.set_freeze(conn, True)
    finally:
        conn.close()

    m = await client.post(
        f"{API}/tasks/T-GOV-DISP/mention", headers=auth_headers("pm"),
        json={"target": "report-collector", "message": "请处理独立工作",
              "context_mode": "dispatch"})
    assert m.status_code == 200, m.text

    conn = open_conn(app.state.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE task_id='T-GOV-DISP'"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT 1 FROM audit WHERE task_id='T-GOV-DISP' "
            "AND action='dispatch.suppressed'").fetchone() is not None
    finally:
        conn.close()
