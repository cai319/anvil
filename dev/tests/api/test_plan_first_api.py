"""P1-0 字段与端点：任务 plan_first/planner、规划 run、草案提交与批准落板。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import (  # noqa: E402
    auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)


@pytest.fixture
async def seeded(client):
    await client.post("/api/v1/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "designer", "developer"):
        await client.post("/api/v1/roles", json=role_payload(slug), headers=auth_headers())


async def test_task_plan_first_roundtrip(client, seeded):
    r = await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF1", assignees=["pm", "developer"], plan_first=True, planner="pm"), headers=auth_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plan_first"] is True
    assert body["planner"] == "pm"
    got = await client.get("/api/v1/tasks/T-PF1", headers=auth_headers())
    assert got.json()["planner"] == "pm"
    assert got.json()["plan_first"] is True


async def test_plan_endpoint_creates_planner_run(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF2", assignees=["pm", "developer"], plan_first=True, planner="pm"),
        headers=auth_headers())
    r = await client.post("/api/v1/tasks/T-PF2/plan", headers=human_headers())
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["role_slug"] == "pm"
    assert "工作流规划者" in (run.get("prompt") or "")
    # 只拉起 planner，其他角色不动
    runs = await client.get("/api/v1/runs", headers=auth_headers())
    assert [x["role_slug"] for x in runs.json() if x.get("task_id") == "T-PF2"] == ["pm"]


async def test_plan_endpoint_rejects_non_assignee_planner(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF3", assignees=["pm"], plan_first=True, planner="developer"),
        headers=auth_headers())
    r = await client.post("/api/v1/tasks/T-PF3/plan", headers=human_headers())
    assert r.status_code == 409


async def test_plan_endpoint_requires_planner(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF5", assignees=["pm"]), headers=auth_headers())
    r = await client.post("/api/v1/tasks/T-PF5/plan", headers=human_headers())
    assert r.status_code == 409


async def test_agent_can_submit_draft_and_human_approves(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF4", assignees=["pm", "designer", "developer"], plan_first=True, planner="pm"),
        headers=auth_headers())
    tree = {"leader": "pm", "feedback_rules": ["阻塞 @pm"],
            "children": [{"id": "S1", "title": "设计", "type": "design", "role": "designer",
                          "acceptance": ["输出设计文档"], "scope_paths": ["docs/**"]}]}
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "五子棋", "tree": tree, "source_task": "T-PF4", "created_by": "agent:pm"})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    # 人工批准（human session）
    r2 = await client.post("/api/v1/board/drafts/approve", headers=human_headers(),
                           json={"draft_ids": [did]})
    assert r2.status_code == 200 and r2.json()["landed"] == [did]
    s1 = await client.get("/api/v1/tasks/S1", headers=auth_headers())
    assert s1.status_code == 200
    assert s1.json()["parent_id"] == "T-PF4"
    assert s1.json()["assignees"] == ["designer"]
    # 按 source_task 过滤草案列表
    drafts = await client.get("/api/v1/board/drafts?source_task=T-PF4", headers=auth_headers())
    assert len(drafts.json()) == 1
