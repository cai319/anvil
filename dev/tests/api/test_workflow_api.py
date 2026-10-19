"""Workflow API：草案批准 → 创建工作流（不生成子任务）、步骤提交、gate 审核、收件箱聚合。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from tests.conftest import auth_headers, role_payload, runtime_payload, task_payload  # noqa: E402

WF_TREE = {
    "feedback_rules": ["阻塞 @pm"],
    "steps": [
        {"id": "S1", "title": "需求基线", "role": "pm", "depends_on": [], "gate": True,
         "instructions": "产出 PRD", "acceptance": ["PRD"]},
        {"id": "S2", "title": "实现", "role": "developer", "depends_on": ["S1"]},
    ],
}


@pytest.fixture
async def seeded(client):
    await client.post("/api/v1/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post("/api/v1/roles", json=role_payload(slug), headers=auth_headers())


async def _draft(client):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-WF", assignees=["pm", "developer"]), headers=auth_headers())
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "跳棋", "source_task": "T-WF", "kind": "workflow", "tree": WF_TREE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_approve_workflow_draft_creates_workflow_not_tasks(client, seeded):
    did = await _draft(client)
    r = await client.post("/api/v1/board/drafts/approve", headers=auth_headers(),
                          json={"draft_ids": [did]})
    assert r.json()["landed"] == [did]
    wf = await client.get("/api/v1/tasks/T-WF/workflow", headers=auth_headers())
    assert wf.status_code == 200
    body = wf.json()
    assert [s["step_id"] for s in body["steps"]] == ["S1", "S2"]
    assert body["steps"][0]["gate"] is True
    # 不生成子任务（大厅只有高层任务）
    tasks = (await client.get("/api/v1/tasks", headers=auth_headers())).json()
    assert [t["id"] for t in tasks] == ["T-WF"]
    # 任务进入进行中
    assert tasks[0]["status"] == "in_progress"


async def test_step_submit_gate_and_review(client, seeded, app):
    wid = None
    did = await _draft(client)
    await client.post("/api/v1/board/drafts/approve", headers=auth_headers(),
                      json={"draft_ids": [did]})
    wid = (await client.get("/api/v1/tasks/T-WF/workflow", headers=auth_headers())).json()["id"]

    # 模拟调度器启动 S1
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='running' WHERE workflow_id=? AND step_id='S1'", (wid,))
    conn.commit()
    conn.close()

    r = await client.post(f"/api/v1/workflows/{wid}/steps/S1/submit", headers=auth_headers("pm"),
                          json={"summary": "PRD 完成"})
    assert r.status_code == 200, r.text
    step = next(s for s in r.json()["steps"] if s["step_id"] == "S1")
    assert step["status"] == "submitted"       # gate 等待人工

    # 收件箱出现 gate 审核项
    inbox = (await client.get("/api/v1/inbox", headers=auth_headers())).json()
    gates = [i for i in inbox["items"] if i["kind"] == "gate"]
    assert len(gates) == 1

    r = await client.post(f"/api/v1/workflows/{wid}/steps/S1/review", headers=auth_headers(),
                          json={"approve": True})
    step = next(s for s in r.json()["steps"] if s["step_id"] == "S1")
    assert step["status"] == "approved"
    inbox = (await client.get("/api/v1/inbox", headers=auth_headers())).json()
    assert all(i["kind"] != "gate" for i in inbox["items"])


async def test_mention_on_workflow_creates_reply_run(client, seeded):
    did = await _draft(client)
    await client.post("/api/v1/board/drafts/approve", headers=auth_headers(),
                      json={"draft_ids": [did]})
    r = await client.post("/api/v1/tasks/T-WF/mention",
                          headers={"X-Anvil-Session": "test-session", "X-Anvil-Role": "pm"},
                          json={"target": "developer", "message": "接口缺少 moveId，请确认"})
    assert r.status_code == 200, r.text
    runs = (await client.get("/api/v1/runs?task_id=T-WF", headers=auth_headers())).json()
    reply = [x for x in runs if x["role_slug"] == "developer"
             and (x.get("prompt") or "").startswith("[anvil:reply]")]
    assert len(reply) == 1
    assert "接口缺少 moveId" in reply[0]["prompt"]
