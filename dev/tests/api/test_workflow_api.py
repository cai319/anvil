"""Workflow API：草案批准 → 创建工作流（不生成子任务）、步骤提交、gate 审核、收件箱聚合。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from tests.conftest import auth_headers, role_payload, runtime_payload, task_payload, human_headers  # noqa: E402

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
    r = await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
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


async def test_approve_workflow_draft_on_closed_task_is_rejected(client, seeded):
    did = await _draft(client)
    cancelled = await client.post(
        "/api/v1/tasks/T-WF/cancel", headers=human_headers())
    assert cancelled.status_code == 200, cancelled.text
    r = await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
                          json={"draft_ids": [did]})
    assert r.status_code == 409, r.text
    assert "terminal" in r.text


async def test_step_submit_gate_and_review(client, seeded, app):
    wid = None
    did = await _draft(client)
    await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
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

    r = await client.post(f"/api/v1/workflows/{wid}/steps/S1/review", headers=human_headers(),
                          json={"approve": True})
    step = next(s for s in r.json()["steps"] if s["step_id"] == "S1")
    assert step["status"] == "approved"
    inbox = (await client.get("/api/v1/inbox", headers=auth_headers())).json()
    assert all(i["kind"] != "gate" for i in inbox["items"])


async def test_mention_on_workflow_creates_reply_run(client, seeded):
    did = await _draft(client)
    await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
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


DECISION_TREE = {
    "steps": [
        {"id": "S1", "title": "修复", "role": "developer"},
        {"id": "S2", "title": "复验", "role": "pm", "depends_on": ["S1"]},
        {"id": "D", "kind": "decision", "title": "决策", "role": "developer",
         "depends_on": ["S2"], "on_reject": ["S1", "S2"]},
    ],
}


async def test_decision_step_api_approve_and_reject(client, seeded, app):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-DEC", assignees=["pm", "developer"]), headers=auth_headers())
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "决策", "source_task": "T-DEC", "kind": "workflow", "tree": DECISION_TREE})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
                      json={"draft_ids": [did]})
    wid = (await client.get("/api/v1/tasks/T-DEC/workflow",
                            headers=auth_headers())).json()["id"]

    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='approved' WHERE workflow_id=? "
                 "AND step_id IN ('S1','S2')", (wid,))
    conn.execute("UPDATE workflow_steps SET status='running' "
                 "WHERE workflow_id=? AND step_id='D'", (wid,))
    conn.commit()
    conn.close()

    r = await client.post(f"/api/v1/workflows/{wid}/steps/D/decide",
                          headers=auth_headers("developer"),
                          json={"approve": False, "feedback": "证据不足"})
    assert r.status_code == 200, r.text
    steps = {s["step_id"]: s for s in r.json()["steps"]}
    assert steps["S1"]["status"] == "pending"
    assert steps["S2"]["status"] == "pending"
    assert steps["D"]["status"] == "pending"
    assert steps["D"]["rework_count"] == 1

    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='approved' WHERE workflow_id=? "
                 "AND step_id IN ('S1','S2')", (wid,))
    conn.execute("UPDATE workflow_steps SET status='running' "
                 "WHERE workflow_id=? AND step_id='D'", (wid,))
    conn.commit()
    conn.close()

    r = await client.post(f"/api/v1/workflows/{wid}/steps/D/decide",
                          headers=auth_headers("developer"),
                          json={"approve": True, "feedback": "通过"})
    assert r.status_code == 200, r.text
    steps = {s["step_id"]: s for s in r.json()["steps"]}
    assert steps["D"]["status"] == "approved"


DYNAMIC_TREE = {
    "steps": [
        {"id": "P1", "title": "项目盘点与切片", "role": "pm", "kind": "plan_slices",
         "instructions": "盘点并切片"},
        {"id": "F1", "title": "最终报告", "role": "pm", "depends_on": ["P1"],
         "instructions": "汇总"},
    ],
    "loop": {
        "from": "P1",
        "execute": {"role": "developer"},
        "verify": {"role": "pm"},
        "fix": {"role": "developer"},
        "verify2": {"role": "pm"},
        "decide": {"role": "pm", "max_rework": 2},
    },
}


async def test_plan_slices_api_expands_runtime_plan(client, seeded, app):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-DYN", assignees=["pm", "developer"]), headers=auth_headers())
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "运行时切片", "source_task": "T-DYN", "kind": "workflow",
        "tree": DYNAMIC_TREE})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
                      json={"draft_ids": [did]})
    wid = (await client.get("/api/v1/tasks/T-DYN/workflow",
                            headers=auth_headers())).json()["id"]

    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='running' "
                 "WHERE workflow_id=? AND step_id='P1'", (wid,))
    conn.commit()
    conn.close()

    plan = {"rationale": "按模块", "slices": [
        {"id": "auth", "title": "认证模块",
         "execute_steps": [{"id": "static", "title": "静态审计"}]},
        {"id": "pay", "title": "支付模块"},
    ]}
    r = await client.post(f"/api/v1/workflows/{wid}/steps/P1/plan",
                          headers=auth_headers("pm"), json=plan)
    assert r.status_code == 200, r.text
    body = r.json()
    steps = {s["step_id"]: s for s in body["steps"]}
    assert steps["P1"]["status"] == "approved"
    assert steps["F1"]["depends_on"] == ["P1.pay.decide"]
    assert steps["P1.auth.decide"]["kind"] == "decision"
    assert steps["P1.auth.decide"]["slice_key"] == "auth"

    # 幂等：重复提交不会重复生成
    r = await client.post(f"/api/v1/workflows/{wid}/steps/P1/plan",
                          headers=auth_headers("pm"), json=plan)
    assert r.status_code == 200, r.text
    assert len(r.json()["steps"]) == len(body["steps"])


async def test_mention_cindy_triggers_replan(client, seeded, app):
    """执行角色 @cindy 提出 loop 诉求 → 拉起 planner run，由 Cindy 重新定制 workflow。"""
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-MENTION", assignees=["pm", "developer"]), headers=auth_headers())
    r = await client.post("/api/v1/tasks/T-MENTION/mention", headers=auth_headers("pm"),
                          json={"target": "cindy",
                                "message": "这个项目需要增加安全复核阶段，请调整 loop"})
    assert r.status_code == 200, r.text

    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT role_slug, prompt FROM runs WHERE task_id='T-MENTION' "
            "AND role_slug='cindy' ORDER BY started_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "安全复核阶段" in row["prompt"]
    assert "workflow/loop" in row["prompt"]


SLICED_TREE = {
    "feedback_rules": ["审计风险升级 @human"],
    "slices": [{
        "id": "A",
        "title": "认证模块审计",
        "execute_role": "pm",
        "verify_role": "developer",
        "fix_role": "pm",
        "decision_role": "developer",
        "execute_steps": [
            {"id": "static", "title": "静态审计", "acceptance": ["覆盖认证入口"]},
        ],
    }],
}


async def test_slice_draft_expands_to_loop_and_serial_chain_is_rejected(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-SLICE", assignees=["pm", "developer"]), headers=auth_headers())
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "代码审计", "source_task": "T-SLICE", "kind": "workflow",
        "tree": SLICED_TREE})
    assert r.status_code == 201, r.text
    steps = r.json()["tree"]["steps"]
    assert [s["id"] for s in steps] == [
        "A.scope", "A.static", "A.verify", "A.fix", "A.verify2", "A.decide",
    ]
    assert steps[-1]["kind"] == "decision"
    assert steps[-1]["on_reject"] == ["A.fix", "A.verify2"]

    # 同一任务同时只允许一个待审 workflow 草案；先驳回再验证串行结构校验。
    r = await client.post("/api/v1/board/drafts/reject", headers=human_headers(), json={
        "draft_id": r.json()["id"], "feedback": "换串行草案验证校验"})
    assert r.status_code == 200, r.text

    serial = {"steps": [
        {"id": f"S{i}", "title": f"step {i}", "role": ("pm", "developer")[i % 2],
         "depends_on": [f"S{i - 1}"] if i > 1 else []}
        for i in range(1, 9)
    ]}
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "串行草案", "source_task": "T-SLICE", "kind": "workflow", "tree": serial})
    assert r.status_code == 409, r.text
    assert "flat serial chain" in r.text
