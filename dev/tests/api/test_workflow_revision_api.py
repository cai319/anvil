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
    assert detail["impact"]["classification"] == "stale"
    wf = (await client.get(f"{API}/workflows/{wid}", headers=auth_headers())).json()
    assert "P1.auth2.static" not in {s["step_id"] for s in wf["steps"]}


async def test_pending_revision_only_attached_to_its_base_workflow(client, app):
    wid = await _seed(client, app, "T-REV-BASE")
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id, task_id, definition_json, status, created_at, updated_at) "
            "VALUES ('wf-old-execution', 'T-REV-BASE', '{}', 'terminated', ?, ?)",
            (now_iso(), now_iso()))
        conn.commit()
    finally:
        conn.close()
    draft = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT, "rationale": "只针对当前执行"})
    assert draft.status_code == 201, draft.text

    old = (await client.get(f"{API}/workflows/wf-old-execution",
                            headers=auth_headers())).json()
    assert "pending_revision" not in old
    current = (await client.get(f"{API}/workflows/{wid}", headers=auth_headers())).json()
    assert current["pending_revision"]["draft_id"] == draft.json()["id"]


async def test_batch_approval_tasks_then_revision_does_not_500(client, app):
    """批量批准：先落 tasks 草案（未提交事务）再应用修订，不能撞 BEGIN IMMEDIATE。"""
    wid = await _seed(client, app, "T-REV-BATCH")
    r = await client.post(f"{API}/tasks", json=task_payload(id="T-BATCH"),
                          headers=auth_headers())
    assert r.status_code == 201, r.text
    tasks_draft = await client.post(f"{API}/board/drafts", headers=auth_headers(), json={
        "goal": "补一个子任务", "source_task": "T-BATCH", "kind": "tasks",
        "tree": {"children": [{"id": "T-BATCH-1", "title": "子任务", "role": "qa-role"}]},
    })
    assert tasks_draft.status_code == 201, tasks_draft.text
    rev_draft = await client.post(
        f"{API}/workflows/{wid}/revisions", headers=auth_headers(),
        json={"intent": INTENT, "rationale": "批量批准"})
    assert rev_draft.status_code == 201, rev_draft.text

    r = await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                          json={"draft_ids": [tasks_draft.json()["id"],
                                              rev_draft.json()["id"]]})
    assert r.status_code == 200, r.text
    wf = (await client.get(f"{API}/workflows/{wid}", headers=auth_headers())).json()
    assert "P1.auth2.static" in {s["step_id"] for s in wf["steps"]}
    child = await client.get(f"{API}/tasks/T-BATCH-1", headers=auth_headers())
    assert child.status_code == 200


async def test_revision_invalid_value_types_return_invalid_not_500(client, app):
    wid = await _seed(client, app, "T-REV-TYPES")
    cases = [
        {"remove_slices": 3},
        {"patch_steps": [{"step_id": "F1", "fields": {"depends_on": 3}}]},
    ]
    for intent in cases:
        r = await client.post(
            f"{API}/workflows/{wid}/revisions/preview", headers=auth_headers(),
            json={"intent": intent})
        assert r.status_code == 200, (intent, r.text)
        assert r.json()["classification"] == "invalid", (intent, r.json())
        assert r.json()["safe"] is False


async def test_revision_rejects_noop_and_malformed_slices(client, app):
    wid = await _seed(client, app, "T-REV-NOOP")
    cases = [
        {},
        {"add_slice": [{"id": "auth2"}]},        # 拼错键：不能被当成“无实际变更”
        {"add_slices": [5]},                      # 非对象条目
        {"update_slices": ["x"]},
    ]
    for intent in cases:
        r = await client.post(
            f"{API}/workflows/{wid}/revisions/preview", headers=auth_headers(),
            json={"intent": intent})
        assert r.status_code == 200, (intent, r.text)
        assert r.json()["classification"] == "invalid", (intent, r.json())
        assert r.json()["safe"] is False


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


