"""Workflow 增量修订 API：预览、草案、human 审批、驳回反馈。"""
from __future__ import annotations

import json

import pytest

from anvil.db import open_conn
from anvil.services import workflow as wf_service
from anvil.util import now_iso
from tests.conftest import API, auth_headers, human_headers, task_payload

WF = {
    "steps": [
        {"id": "P1", "title": "切片计划", "role": "qa-role", "kind": "plan_slices"},
        {"id": "F1", "title": "最终报告", "role": "qa-role", "depends_on": ["P1"]},
    ],
    "loop": {
        "from": "P1",
        "execute": {"role": "auditor", "title": "执行"},
        "verify": {"role": "reviewer", "title": "验证"},
        "fix": {"role": "developer", "title": "修复"},
        "verify2": {"role": "reviewer", "title": "复验"},
        "decide": {"role": "lead", "title": "决策", "max_rework": 2},
    },
}
PLAN = {"rationale": "按模块切片", "slices": [
    {"id": "auth", "title": "认证", "scope_paths": ["src/auth/**"],
     "execute_steps": [{"id": "static", "title": "静态审计"}]},
    {"id": "pay", "title": "支付", "scope_paths": ["src/pay/**"],
     "execute_steps": [{"id": "static", "title": "静态审计"}]},
]}
INTENT = {"add_slices": [{
    "id": "auth2", "title": "认证补充", "scope_paths": ["src/auth/**"],
    "execute_steps": [{"id": "static", "title": "补充审计"}],
}], "rationale": "认证还需一轮"}


async def _seed(client, app, tid="T-REV"):
    r = await client.post(
        f"{API}/tasks", headers=auth_headers(),
        json={**task_payload(id=tid), "planner": "qa-role"})
    assert r.status_code == 201, r.text
    conn = open_conn(app.state.db_path)
    try:
        wf = wf_service.create_workflow(conn, tid, WF)
        wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "qa-role", PLAN)
        conn.execute(
            "UPDATE workflow_steps SET status='approved' "
            "WHERE workflow_id=? AND slice_key='auth'", (wf["id"],))
        conn.commit()
        wid = wf["id"]
    finally:
        conn.close()
    return wid


async def test_create_and_apply_safe_revision(client, app):
    wid = await _seed(client, app)
    r = await client.post(
        f"{API}/workflows/{wid}/revisions/preview", headers=auth_headers(),
        json={"intent": INTENT})
    assert r.status_code == 200, r.text
    assert r.json()["safe"] is True

    r = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT, "rationale": "切片反馈"})
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["kind"] == "workflow_revision"
    assert draft["tree"]["impact"]["classification"] == "safe_loop"

    inbox = (await client.get(f"{API}/inbox", headers=human_headers())).json()
    item = next(i for i in inbox["items"] if i["id"] == f"draft:{draft['id']}")
    assert item["meta"]["draft_kind"] == "workflow_revision"
    assert item["meta"]["workflow_revision"]["impact"]["safe"] is True

    r = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft["id"]]})
    assert r.status_code == 200, r.text

    wf = (await client.get(f"{API}/workflows/{wid}", headers=auth_headers())).json()
    assert wf["execution_no"] == 1
    steps = {s["step_id"]: s for s in wf["steps"]}
    assert "P1.auth2.static" in steps
    assert steps["F1"]["depends_on"] == ["P1.auth2.decide"]


async def test_revision_requires_restart_is_rejected_at_creation(client, app):
    wid = await _seed(client, app, "T-REV-UNSAFE")
    conn = open_conn(app.state.db_path)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=? AND step_id='F1'",
        (wid,))
    conn.commit()
    conn.close()

    r = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["impact"]["classification"] == "requires_restart"
    assert any(b["step_id"] == "F1" for b in detail["impact"]["blocked_by"])


async def test_revision_stale_at_approval_returns_impact(client, app):
    wid = await _seed(client, app, "T-REV-STALE")
    r = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT})
    draft = r.json()
    conn = open_conn(app.state.db_path)
    conn.execute(
        "UPDATE workflow_steps SET status='running' WHERE workflow_id=? AND step_id='F1'",
        (wid,))
    conn.commit()
    conn.close()

    r = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft["id"]]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["impact"]["classification"] == "requires_restart"
    wf = (await client.get(f"{API}/workflows/{wid}", headers=auth_headers())).json()
    assert "P1.auth2.static" not in {s["step_id"] for s in wf["steps"]}


async def test_full_workflow_draft_blocked_while_revision_pending(client, app):
    """修订待审时不允许再提交完整 workflow 草案，避免两个变更入口互相覆盖。"""
    wid = await _seed(client, app, "T-REV-DUP")
    r = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT, "rationale": "补充切片"})
    assert r.status_code == 201, r.text

    r = await client.post(
        f"{API}/board/drafts", headers=auth_headers(),
        json={"goal": "完整重规划", "kind": "workflow",
              "source_task": "T-REV-DUP",
              "tree": {"steps": [{"id": "S1", "title": "重做",
                                  "role": "qa-role"}]}})
    assert r.status_code == 409, r.text
    assert "pending" in r.text


async def test_revision_reject_feedback_wakes_planner(client, app):
    wid = await _seed(client, app, "T-REV-REJECT")
    r = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT, "rationale": "补充切片"})
    draft = r.json()
    r = await client.post(
        f"{API}/board/drafts/reject", headers=human_headers(),
        json={"draft_id": draft["id"], "feedback": "请先说明影响范围"})
    assert r.status_code == 200, r.text
    inbox = (await client.get(
        f"{API}/me/inbox", params={"task_id": "T-REV-REJECT"},
        headers=auth_headers())).json()
    assert any("请先说明影响范围" in i["text"] for i in inbox["items"])


