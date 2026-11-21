"""任务结束后的知识收割提醒：when(task done) → Cindy 收割三域知识。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")


def _agent_session(app, role: str, token: str) -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00",
             "2999-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def _create_task(client, tid: str = "T-HARVEST", **overrides):
    payload = task_payload(id_=tid, **overrides)
    r = await client.post(f"{API}/tasks", json=payload, headers=auth_headers())
    assert r.status_code == 201, r.text
    return r.json()


async def _land_workflow(client, tid: str, role: str = "qa-role"):
    """模拟 Cindy 出 workflow 草案 + human 落板执行。"""
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload(role), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "知识收割", "source_task": tid, "kind": "workflow",
        "tree": {"steps": [{
            "id": "S1", "title": "执行", "role": role,
            "instructions": "完成工作", "acceptance": ["产物"],
        }]},
    })
    assert draft.status_code == 201, draft.text
    approved = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text


async def _mark_done(app, tid: str):
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()


async def _harvest_reminders(client, tid: str):
    items = (await client.get(f"{API}/reminders", headers=human_headers())).json()
    return [r for r in items if r["role_slug"] == "cindy"
            and r["kind"] == "when"
            and r["when_condition"] == f"task:{tid}:done"]


async def test_workflow_landing_adds_knowledge_harvest_reminder(client):
    ws = await client.post(f"{API}/workspaces", json={"name": "收割频道"},
                           headers=human_headers())
    assert ws.status_code == 201, ws.text
    await _create_task(client, "T-HARVEST", workspace_id=ws.json()["id"])
    # 仅创建任务不生成：必须 Cindy 规划且 human 落板后才创建。
    assert await _harvest_reminders(client, "T-HARVEST") == []

    await _land_workflow(client, "T-HARVEST")
    reminders = await _harvest_reminders(client, "T-HARVEST")
    assert len(reminders) == 1
    prompt = reminders[0]["prompt"]
    for token in (
        "知识收割", "任务知识", "主题知识", "频道知识",
        "anvil task knowledge-write", "anvil knowledge card-create",
        "anvil workspace knowledge harvest", "禁止 approve",
    ):
        assert token in prompt


async def test_feature_flag_disables_harvest_reminder(client, monkeypatch):
    from anvil import config
    monkeypatch.setattr(config, "KNOWLEDGE_HARVEST_REMINDER_ENABLED", False)
    await _create_task(client, "T-HARVEST-OFF")
    await _land_workflow(client, "T-HARVEST-OFF")
    assert await _harvest_reminders(client, "T-HARVEST-OFF") == []


async def test_done_task_fires_cindy_harvest_once(client, app):
    await _create_task(client, "T-HARVEST-DONE")
    await _land_workflow(client, "T-HARVEST-DONE")
    await _mark_done(app, "T-HARVEST-DONE")

    assert (await client.post(f"{API}/control/tick",
                              headers=human_headers())).status_code == 200
    runs = (await client.get(f"{API}/runs", headers=human_headers())).json()
    harvest = [r for r in runs if r["role_slug"] == "cindy"
               and "【知识收割】" in (r.get("prompt") or "")]
    assert len(harvest) == 1
    assert "T-HARVEST-DONE" in harvest[0]["prompt"]

    # 已触发后不重复建 run
    assert (await client.post(f"{API}/control/tick",
                              headers=human_headers())).status_code == 200
    runs = (await client.get(f"{API}/runs", headers=human_headers())).json()
    assert len([r for r in runs if r["role_slug"] == "cindy"
                and "【知识收割】" in (r.get("prompt") or "")]) == 1


async def test_cancel_removes_reminder_and_reopen_recreates(client):
    await _create_task(client, "T-HARVEST-CANCEL")
    await _land_workflow(client, "T-HARVEST-CANCEL")
    assert len(await _harvest_reminders(client, "T-HARVEST-CANCEL")) == 1

    assert (await client.post(f"{API}/tasks/T-HARVEST-CANCEL/cancel",
                              headers=human_headers())).status_code == 200
    assert await _harvest_reminders(client, "T-HARVEST-CANCEL") == []

    assert (await client.post(f"{API}/tasks/T-HARVEST-CANCEL/reopen",
                              headers=human_headers())).status_code == 200
    reminders = await _harvest_reminders(client, "T-HARVEST-CANCEL")
    assert len(reminders) == 1
    assert reminders[0]["status"] == "pending"


async def test_manual_harvest_reminder_is_not_duplicated_or_deleted(client):
    await _create_task(client, "T-HARVEST-MANUAL")
    manual = await client.post(f"{API}/reminders", headers=human_headers(), json={
        "role_slug": "cindy", "kind": "when",
        "when_condition": "task:T-HARVEST-MANUAL:done",
        "prompt": "手工知识收割提醒：由我处理",
    })
    assert manual.status_code == 201, manual.text

    await _land_workflow(client, "T-HARVEST-MANUAL")
    reminders = await _harvest_reminders(client, "T-HARVEST-MANUAL")
    assert len(reminders) == 1
    assert reminders[0]["id"] == manual.json()["id"]
    assert not reminders[0]["prompt"].startswith("【知识收割】")

    assert (await client.post(f"{API}/tasks/T-HARVEST-MANUAL/cancel",
                              headers=human_headers())).status_code == 200
    reminders = await _harvest_reminders(client, "T-HARVEST-MANUAL")
    assert len(reminders) == 1  # 人工提醒不随任务取消删除


async def test_delete_removes_harvest_reminder(client):
    await _create_task(client, "T-HARVEST-DELETE")
    await _land_workflow(client, "T-HARVEST-DELETE")
    assert len(await _harvest_reminders(client, "T-HARVEST-DELETE")) == 1

    deleted = await client.delete(
        f"{API}/tasks/T-HARVEST-DELETE?force=true", headers=human_headers())
    assert deleted.status_code == 204, deleted.text
    assert await _harvest_reminders(client, "T-HARVEST-DELETE") == []


async def test_backfill_keeps_legacy_reminder_for_unplanned_task(client, app):
    from anvil.db import open_conn
    from anvil.services import knowledge_harvest

    await _create_task(client, "T-HARVEST-LEGACY")
    conn = open_conn(app.state.db_path)
    try:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id='T-HARVEST-LEGACY'").fetchone()
        knowledge_harvest.ensure_task_reminder(conn, dict(task), commit=True)
        assert conn.execute(
            "SELECT COUNT(*) n FROM reminders WHERE kind='when'").fetchone()["n"] == 1
        knowledge_harvest.backfill(conn)
        assert conn.execute(
            "SELECT COUNT(*) n FROM reminders WHERE kind='when'").fetchone()["n"] == 1
    finally:
        conn.close()


async def test_backfill_refreshes_auto_prompt(client, app):
    from anvil.db import open_conn
    from anvil.services import knowledge_harvest

    await _create_task(client, "T-HARVEST-BACKFILL")
    await _land_workflow(client, "T-HARVEST-BACKFILL")
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "UPDATE reminders SET prompt=? WHERE kind='when'",
            ("【知识收割】任务 T-HARVEST-BACKFILL 已结束。旧文案",))
        conn.commit()
        assert knowledge_harvest.backfill(conn) == 0
        prompt = conn.execute(
            "SELECT prompt FROM reminders WHERE kind='when'").fetchone()["prompt"]
        assert "完成后触发知识收割" in prompt
        assert "已结束" not in prompt
    finally:
        conn.close()


async def test_cindy_can_propose_task_knowledge_only(client, app):
    await _create_task(client, "T-HARVEST-WRITE")
    fields = {
        "use_when": "同类任务复盘时",
        "expected_effect": "复用本次任务的结论",
        "evidence": "任务 T-HARVEST-WRITE 的步骤与产物",
        "task_context": "知识收割测试任务",
        "finding": "完成后由 Cindy 补充任务知识",
        "verification": "查看任务时间线与产物",
        "reuse_scope": "同主题任务",
    }
    body = {
        "title": "任务知识提案",
        "tldr": "Cindy 只能写 proposed",
        "kind": "task_finding",
        "fields": fields,
    }

    other = _agent_session(app, "dev", "harvest-other-token")
    denied = await client.post(f"{API}/tasks/T-HARVEST-WRITE/knowledge",
                               json=body, headers=other)
    assert denied.status_code == 403, denied.text

    cindy = _agent_session(app, "cindy", "harvest-cindy-token")
    created = await client.post(f"{API}/tasks/T-HARVEST-WRITE/knowledge",
                                json=body, headers=cindy)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "proposed"

    forced = await client.post(f"{API}/tasks/T-HARVEST-WRITE/knowledge",
                               json={**body, "title": "试图直接生效",
                                     "status": "active"}, headers=cindy)
    assert forced.status_code == 403, forced.text
