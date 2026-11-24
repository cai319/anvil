"""Workflow 读取兼容：Cindy 的结构化反馈规则/非列表字段不能导致 500。

Windows 实例 traceback：`definition.feedback_rules` 被写成
`[{"when": ..., "action": ...}]`，而 `WorkflowDefinitionOut` 要求 `list[str]`，
FastAPI 响应序列化阶段抛出 ResponseValidationError → 500。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from tests.conftest import (  # noqa: E402
    auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

WF_TREE = {
    "feedback_rules": ["阻塞 @pm"],
    "steps": [
        {"id": "S1", "title": "需求基线", "role": "pm", "acceptance": ["PRD"]},
        {"id": "S2", "title": "实现", "role": "developer", "depends_on": ["S1"]},
    ],
}

STRUCTURED_TREE = {
    "feedback_rules": [
        {"when": "条件", "action": "动作"},
    ],
    "verification": {"功能路径": "手工验证"},
    "steps": [
        {"id": "S1", "title": "需求基线", "role": "pm", "acceptance": "产出 PRD"},
        {"id": "S2", "title": "实现", "role": "developer", "depends_on": ["S1"],
         "acceptance": [{"text": "测试通过"}]},
    ],
}


@pytest.fixture
async def seeded(client):
    await client.post("/api/v1/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        r = await client.post(
            "/api/v1/roles", json=role_payload(slug), headers=auth_headers())
        assert r.status_code == 201, r.text


async def _approved(client, task_id: str = "T-WF", tree: dict | None = None) -> dict:
    await client.post("/api/v1/tasks", json=task_payload(
        id=task_id, assignees=["pm", "developer"]), headers=auth_headers())
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "兼容性", "source_task": task_id, "kind": "workflow",
        "tree": tree or WF_TREE})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    r = await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
                          json={"draft_ids": [did]})
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/v1/tasks/{task_id}/workflow", headers=auth_headers())
    assert r.status_code == 200, r.text
    return r.json()


async def test_structured_feedback_rules_flattened_on_read(client, seeded, app):
    """本次 Windows traceback 的精确回归：对象反馈规则必须扁平化为字符串。"""
    wf = await _approved(client)
    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT definition_json FROM workflows WHERE id=?", (wf["id"],)).fetchone()
        definition = json.loads(row["definition_json"])
        definition["feedback_rules"] = [{
            "when": "切片执行中发现跨切片耦合",
            "action": "立即 anvil task mention --to cindy",
        }]
        conn.execute(
            "UPDATE workflows SET definition_json=? WHERE id=?",
            (json.dumps(definition, ensure_ascii=False), wf["id"]))
        conn.commit()

        # 服务层（内部消费者）也必须看到规范化后的数据。
        from anvil.services import workflow as workflow_service
        service_wf = workflow_service.get_workflow(conn, wf["id"])
    finally:
        conn.close()

    expected = ["切片执行中发现跨切片耦合：立即 anvil task mention --to cindy"]
    assert service_wf["definition"]["feedback_rules"] == expected

    r = await client.get("/api/v1/tasks/T-WF/workflow", headers=auth_headers())
    assert r.status_code == 200, r.text
    assert r.json()["definition"]["feedback_rules"] == expected


async def test_non_list_fields_are_normalized_on_read(client, seeded, app):
    """老数据/异常字段：字符串、对象、非法枚举不能把 GET 打成 500。"""
    wf = await _approved(client)
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "UPDATE workflow_steps SET acceptance_json=?, depends_on_json=?, "
            "decision_authority='both' WHERE workflow_id=? AND step_id='S2'",
            (json.dumps("测试通过"), json.dumps("S1"), wf["id"]))
        conn.execute(
            "UPDATE workflow_steps SET acceptance_json=? WHERE workflow_id=? "
            "AND step_id='S1'",
            (json.dumps([{"text": "PRD"}, 42]), wf["id"]))
        conn.execute(
            "UPDATE workflows SET weight='high', status='paused' WHERE id=?",
            (wf["id"],))
        conn.commit()
    finally:
        conn.close()

    r = await client.get("/api/v1/tasks/T-WF/workflow", headers=auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    steps = {s["step_id"]: s for s in body["steps"]}
    assert steps["S1"]["acceptance"] == ["PRD", "42"]
    assert steps["S2"]["acceptance"] == ["测试通过"]
    assert steps["S2"]["depends_on"] == ["S1"]
    assert steps["S2"]["decision_authority"] == ""
    assert body["weight"] is None
    assert body["status"] == "failed"

    r = await client.get(f"/api/v1/workflows/{wf['id']}", headers=auth_headers())
    assert r.status_code == 200, r.text


async def test_new_workflow_stores_normalized_fields(client, seeded, app):
    """新草案落库前就规范化，避免 Windows 新任务再次写入对象反馈规则。"""
    wf = await _approved(client, task_id="T-WRITE", tree=STRUCTURED_TREE)
    assert wf["definition"]["feedback_rules"] == ["条件：动作"]
    verification = wf["definition"]["verification"]
    assert len(verification) == 1
    assert verification[0]["cell"] == "功能路径"
    assert verification[0]["method"] == "手工验证"
    steps = {s["step_id"]: s for s in wf["steps"]}
    assert steps["S1"]["acceptance"] == ["产出 PRD"]
    assert steps["S2"]["acceptance"] == ["测试通过"]

    conn = open_conn(app.state.db_path)
    try:
        definition = json.loads(conn.execute(
            "SELECT definition_json FROM workflows WHERE id=?", (wf["id"],)
        ).fetchone()["definition_json"])
        s1_acceptance = json.loads(conn.execute(
            "SELECT acceptance_json FROM workflow_steps WHERE workflow_id=? "
            "AND step_id='S1'", (wf["id"],)).fetchone()["acceptance_json"])
        s2_acceptance = json.loads(conn.execute(
            "SELECT acceptance_json FROM workflow_steps WHERE workflow_id=? "
            "AND step_id='S2'", (wf["id"],)).fetchone()["acceptance_json"])
    finally:
        conn.close()
    assert definition["feedback_rules"] == ["条件：动作"]
    # 字符串收敛为单元素数组；对象条目保留结构，供 review packet 读取 status/criterion。
    assert s1_acceptance == ["产出 PRD"]
    assert s2_acceptance == [{"text": "测试通过"}]


def test_workflow_out_model_tolerates_structured_fields():
    """响应模型安全网：绕过 get_workflow 的路径也不能 500。"""
    from anvil.schemas import WorkflowOut

    wf = WorkflowOut.model_validate({
        "id": "W-1",
        "task_id": "T-1",
        "status": "running",
        "created_at": "2026-11-24T00:00:00+00:00",
        "updated_at": "2026-11-24T00:00:00+00:00",
        "definition": {"feedback_rules": [{"when": "条件", "action": "动作"}]},
        "steps": [{
            "workflow_id": "W-1", "step_id": "S1", "title": "步骤",
            "status": "pending", "updated_at": "2026-11-24T00:00:00+00:00",
            "acceptance": [{"criterion": "安全评审完成", "status": "unmet"}],
        }],
    })
    assert wf.definition.feedback_rules == ["条件：动作"]
    assert wf.steps[0].acceptance == ["安全评审完成（unmet）"]
