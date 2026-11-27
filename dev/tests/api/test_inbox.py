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


async def test_inbox_keeps_latest_human_mention_per_sender(client, seeded):
    """不同角色各自 @human 时不能被同任务的另一条提醒覆盖。"""
    await client.post("/api/v1/roles", json=role_payload("qa"), headers=auth_headers())
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-MULTI-MEN", assignees=["pm", "qa"]), headers=auth_headers())
    await client.post("/api/v1/tasks/T-MULTI-MEN/mention",
                      json={"target": "human", "message": "PM 的问题"},
                      headers=auth_headers("pm"))
    await client.post("/api/v1/tasks/T-MULTI-MEN/mention",
                      json={"target": "human", "message": "QA 的问题"},
                      headers=auth_headers("qa"))
    mens = [i for i in (await _inbox(client))["items"] if i["kind"] == "mention"]
    assert len(mens) == 2
    titles = " ".join(i["title"] for i in mens)
    assert "PM 的问题" in titles and "QA 的问题" in titles

    # 人类任一次回复都晚于两条提醒 → 两条都消失。
    await client.post("/api/v1/tasks/T-MULTI-MEN/comment",
                      json={"body": "两个问题都已确认"}, headers=human_headers())
    assert all(i["kind"] != "mention" for i in (await _inbox(client))["items"])


async def test_inbox_mention_reply_cutoff_is_per_sender(client, seeded):
    """回复只清掉其之前的角色提醒，不会误清之后的新提醒。"""
    await client.post("/api/v1/roles", json=role_payload("qa"), headers=auth_headers())
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-CUTOFF", assignees=["pm", "qa"]), headers=auth_headers())
    await client.post("/api/v1/tasks/T-CUTOFF/mention",
                      json={"target": "human", "message": "先问 PM"},
                      headers=auth_headers("pm"))
    await client.post("/api/v1/tasks/T-CUTOFF/comment",
                      json={"body": "回复 PM"}, headers=human_headers())
    await client.post("/api/v1/tasks/T-CUTOFF/mention",
                      json={"target": "human", "message": "QA 后问"},
                      headers=auth_headers("qa"))
    mens = [i for i in (await _inbox(client))["items"] if i["kind"] == "mention"]
    assert len(mens) == 1 and "QA 后问" in mens[0]["title"]


async def test_inbox_mention_clears_when_human_replies_to_sender(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(id="T-REPLY"), headers=auth_headers())
    await client.post("/api/v1/tasks/T-REPLY/mention",
                      json={"target": "human", "message": "请确认是否继续"},
                      headers=auth_headers("pm"))
    assert any(i["kind"] == "mention" for i in (await _inbox(client))["items"])
    # 自然人回复该角色（而不是评论）也应视为已响应。
    await client.post("/api/v1/tasks/T-REPLY/mention",
                      json={"target": "pm", "message": "已确认，继续"},
                      headers=human_headers())
    assert all(i["kind"] != "mention" for i in (await _inbox(client))["items"])


async def test_inbox_mention_clears_on_done_task(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(id="T-DONE"), headers=auth_headers())
    await client.post("/api/v1/tasks/T-DONE/mention",
                      json={"target": "human", "message": "规划确认：任务已完成"},
                      headers=auth_headers("pm"))
    assert any(i["kind"] == "mention" for i in (await _inbox(client))["items"])
    await client.patch("/api/v1/tasks/T-DONE", json={"status": "in_review"},
                       headers=human_headers())
    reviewed = await client.post("/api/v1/tasks/T-DONE/review", json={"approve": True},
                                 headers=human_headers())
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "done"
    assert all(i["kind"] != "mention" for i in (await _inbox(client))["items"])


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
                      json={"title": "AI 需先算连五", "content": "AI 需先算连五",
                            "tags": ["gomoku"],
                            "fields": {
                                "use_when": "五子棋 AI 决策时",
                                "expected_effect": "避免漏算连五",
                                "evidence": "任务 T-D 的棋谱",
                                "observation": "AI 漏算连五",
                                "lesson": "先算连五",
                                "applicability": "五子棋任务",
                                "counterexamples": "暂无",
                            }})
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


async def test_inbox_lesson_keeps_full_title_and_review_meta(client, seeded):
    """收件箱不能把经验标题截成 60 字；详情所需上下文放进 meta。"""
    title = "经验标题不能在中途被截断" + "·补充信息" * 8
    content = f"{title}\n\n完整正文：适用场景、触发逻辑与证据都应可查看。"
    created = await client.post(
        "/api/v1/lessons/pending",
        headers=auth_headers("pm"),
        json={
            "title": title,
            "tldr": "先看适用场景，再核对触发逻辑与证据。",
            "content": content,
            "fields": {
                "use_when": "同类审计任务", "trigger_point": "读取校验结果时",
                "expected_effect": "减少误判", "evidence": "任务 T-1 的复现记录",
                "observation": "环境差异导致误判", "lesson": "先核对环境",
                "applicability": "同类审计任务", "counterexamples": "暂无",
            },
            "tags": ["knowledge"], "repo": "shared", "source_task": "T-1",
        })
    assert created.status_code == 201, created.text
    lid = created.json()["id"]

    body = await _inbox(client)
    item = next(i for i in body["items"] if i["kind"] == "lesson")
    assert item["title"] == title
    assert item["meta"]["lesson_id"] == lid
    assert item["meta"]["tldr"] == "先看适用场景，再核对触发逻辑与证据。"
    assert item["meta"]["tags"] == ["knowledge"]
    assert item["meta"]["repo"] == "shared"
    assert item["meta"]["source_role"] == "pm"
    assert item["meta"]["source_task"] == "T-1"
    assert item["meta"]["lesson"]["title"] == title
    assert item["meta"]["lesson"]["fields"]["trigger_point"] == "读取校验结果时"
    assert item["meta"]["lesson"]["ready"] is True
    assert item["meta"]["lesson"]["submission_errors"] == []

    detail = await client.get(f"/api/v1/lessons/{lid}", headers=auth_headers("pm"))
    assert detail.status_code == 200
    assert detail.json()["fields"]["trigger_point"] == "读取校验结果时"


async def test_inbox_delegation_review_item(client, app, seeded):
    """委派 in_review 出现在收件箱；human 验收后消失。"""
    from anvil.db import open_conn

    conn = open_conn(app.state.db_path)
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T-DPARENT','父任务','in_progress',?,?)", (now, now))
        conn.execute(
            "INSERT INTO tasks (id,title,status,type,parent_id,parent_step_id,"
            "assignee,created_by,created_at,updated_at) "
            "VALUES ('C-DEL','构造 PoC','in_review','delegation','T-DPARENT','S1',"
            "'developer','agent:pm',?,?)", (now, now))
        conn.commit()
    finally:
        conn.close()

    body = await _inbox(client)
    item = next(i for i in body["items"] if i["kind"] == "delegation")
    assert item["task_id"] == "C-DEL"
    assert "父步骤" in item["detail"]

    accepted = await client.post(
        "/api/v1/tasks/C-DEL/delegation/accept", headers=human_headers())
    assert accepted.status_code == 200, accepted.text
    assert not [i for i in (await _inbox(client))["items"]
                if i["kind"] == "delegation"]


async def test_agent_comment_does_not_clear_human_mention(client, seeded):
    """agent 自己的进度 comment 不能替人类清掉待办提醒。"""
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-AGENT", assignees=["pm"]), headers=auth_headers())
    await client.post("/api/v1/tasks/T-AGENT/mention",
                      json={"target": "human", "message": "请确认是否继续"},
                      headers=auth_headers("pm"))
    await client.post("/api/v1/tasks/T-AGENT/comment",
                      json={"body": "我先继续做别的步骤"},
                      headers=auth_headers("pm"))
    mens = [i for i in (await _inbox(client))["items"] if i["kind"] == "mention"]
    assert len(mens) == 1 and mens[0]["task_id"] == "T-AGENT"

    await client.post("/api/v1/tasks/T-AGENT/comment",
                      json={"body": "确认，继续"}, headers=human_headers())
    assert all(i["kind"] != "mention" for i in (await _inbox(client))["items"])


async def test_clarification_confirmation_suppresses_older_mention(client, seeded, app):
    """confirm/skip 更新 clarification_confirmed_at 后，旧提醒应消失，新提醒保留。"""
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-CLAR-INBOX", assignees=["pm"]), headers=auth_headers())
    await client.post("/api/v1/tasks/T-CLAR-INBOX/mention",
                      json={"target": "human", "message": "请在任务简报卡确认或跳过"},
                      headers=auth_headers("pm"))
    assert any(i["kind"] == "mention" for i in (await _inbox(client))["items"])

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "UPDATE tasks SET clarification_status='confirmed', "
            "clarification_confirmed_at=? WHERE id=?",
            (now_iso(), "T-CLAR-INBOX"))
        conn.commit()
    finally:
        conn.close()
    assert all(i["kind"] != "mention" for i in (await _inbox(client))["items"])

    await client.post("/api/v1/tasks/T-CLAR-INBOX/mention",
                      json={"target": "human", "message": "确认后再问一个新的问题"},
                      headers=auth_headers("pm"))
    assert any(i["kind"] == "mention" for i in (await _inbox(client))["items"])


async def test_closed_task_mention_hidden(client, seeded, app):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-CLOSED-MEN", assignees=["pm"]), headers=auth_headers())
    await client.post("/api/v1/tasks/T-CLOSED-MEN/mention",
                      json={"target": "human", "message": "请处理"},
                      headers=auth_headers("pm"))
    conn = open_conn(app.state.db_path)
    try:
        conn.execute("UPDATE tasks SET status='closed' WHERE id='T-CLOSED-MEN'")
        conn.commit()
    finally:
        conn.close()
    assert all(i["kind"] != "mention" for i in (await _inbox(client))["items"])
