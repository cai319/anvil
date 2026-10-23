"""Inbox 聚合端点（信息架构重构）：四类人机决策点统一入口。"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from anvil.util import now_iso  # noqa: E402
from tests.conftest import auth_headers, role_payload, runtime_payload, task_payload, human_headers  # noqa: E402


@pytest.fixture
async def seeded(client):
    await client.post("/api/v1/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post("/api/v1/roles", json=role_payload("pm"), headers=auth_headers())


async def _inbox(client) -> dict:
    r = await client.get("/api/v1/inbox", headers=auth_headers())
    assert r.status_code == 200, r.text
    return r.json()


async def test_inbox_empty(client, seeded):
    assert await _inbox(client) == {"count": 0, "items": []}


async def test_inbox_review_item(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(id="T-REV"), headers=auth_headers())
    await client.patch("/api/v1/tasks/T-REV", json={"status": "in_review"}, headers=human_headers())
    body = await _inbox(client)
    assert body["count"] == 1
    item = body["items"][0]
    assert item["kind"] == "review" and item["task_id"] == "T-REV"
    # 评审通过后消失
    await client.post("/api/v1/tasks/T-REV/review", json={"approve": True}, headers=human_headers())
    assert (await _inbox(client))["count"] == 0


async def test_inbox_mention_lifecycle(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(id="T-MEN"), headers=auth_headers())
    await client.post("/api/v1/tasks/T-MEN/mention",
                      json={"target": "human", "message": "请确认规则"},
                      headers=auth_headers("pm"))
    body = await _inbox(client)
    mens = [i for i in body["items"] if i["kind"] == "mention"]
    assert len(mens) == 1 and mens[0]["task_id"] == "T-MEN"
    # 同任务第二条 @human → 只保留最新一条
    await client.post("/api/v1/tasks/T-MEN/mention",
                      json={"target": "human", "message": "再问一次"},
                      headers=auth_headers("pm"))
    mens = [i for i in (await _inbox(client))["items"] if i["kind"] == "mention"]
    assert len(mens) == 1 and "再问一次" in mens[0]["title"]
    # human 评论（任何评论）后 → 视为已处理，消失
    await client.post("/api/v1/tasks/T-MEN/comment", json={"body": "已确认：无禁手"},
                      headers=human_headers())
    body = await _inbox(client)
    assert all(i["kind"] != "mention" for i in body["items"])


async def test_inbox_labels_workflow_sender(client, app, seeded):
    """workflow 是系统伪发送者，不是 Agent：收件箱要显示“工作流引擎/系统通知”。"""
    await client.post("/api/v1/tasks", json=task_payload(id="T-SYS"), headers=auth_headers())
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            ("T-SYS", "workflow", "mention",
             json.dumps({"target": "human",
                         "message": "工作流全部步骤完成，请做最终评审（通过/驳回）"}),
             now_iso()))
        conn.commit()
    finally:
        conn.close()
    body = await _inbox(client)
    item = next(i for i in body["items"] if i["kind"] == "mention")
    assert "工作流引擎" in item["title"]
    assert "系统通知" in item["detail"]


async def test_inbox_draft_lesson_proposal(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(id="T-D"), headers=auth_headers())
    await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "五子棋", "source_task": "T-D",
        "tree": {"leader": "pm", "children": [{"id": "S1", "title": "设计", "role": "pm"}]}})
    await client.post("/api/v1/lessons/pending", headers=auth_headers("pm"),
                      json={"content": "AI 需先算连五", "tags": ["gomoku"]})
    await client.post("/api/v1/proposals", headers=auth_headers("pm"),
                      json={"field": "tools", "proposed": {"preset": "x"},
                            "rationale": "省 token", "risk": "低"})
    body = await _inbox(client)
    assert sorted(i["kind"] for i in body["items"]) == ["draft", "lesson", "proposal"]
    # 批准草案与 lesson 后消失，proposal 保留
    did = next(i["id"] for i in body["items"] if i["kind"] == "draft").split(":", 1)[1]
    await client.post("/api/v1/board/drafts/approve", json={"draft_ids": [did]},
                      headers=human_headers())
    lid = next(i["id"] for i in body["items"] if i["kind"] == "lesson").split(":", 1)[1]
    await client.post(f"/api/v1/lessons/{lid}/approve", headers=human_headers())
    kinds = [i["kind"] for i in (await _inbox(client))["items"]]
    assert kinds == ["proposal"]
    pid = next(i["id"] for i in body["items"] if i["kind"] == "proposal").split(":", 1)[1]
    await client.post(f"/api/v1/proposals/{pid}/approve", headers=human_headers())
    assert (await _inbox(client))["count"] == 0
