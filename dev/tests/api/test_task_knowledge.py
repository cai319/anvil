"""v1.30 任务共享知识：参与角色共享、子任务标记、父任务聚合。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    for slug in ("qa-role", "other-role"):
        await client.post(f"{API}/roles", json=role_payload(slug=slug))


def _agent_headers(app, role: str = "qa-role", token: str = "task-kb-token") -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def _task(client, tid: str, **kw):
    r = await client.post(
        f"{API}/tasks", json=task_payload(id_=tid, **kw), headers=human_headers()
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _knowledge(client, tid: str, headers: dict, title: str = "子任务发现"):
    return await client.post(
        f"{API}/tasks/{tid}/knowledge",
        json={
            "title": title,
            "tldr": "任务内共享的发现。",
            "kind": "task_finding",
            "fields": {
                "task_context": "当前任务",
                "finding": "发现 A",
                "verification": "已用测试验证",
                "reuse_scope": "当前任务内的角色",
            },
        },
        headers=headers,
    )


async def test_subtask_knowledge_is_marked_and_aggregated(client, app):
    await _seed(client)
    await _task(client, "T-PARENT")
    child = await _task(client, "T-CHILD", parent_id="T-PARENT", assignees=["qa-role"])
    assert child["parent_id"] == "T-PARENT"

    created = await _knowledge(client, "T-CHILD", _agent_headers(app))
    assert created.status_code == 201, created.text
    card = created.json()
    assert card["origin"] == "subtask"
    assert card["parent_task_id"] == "T-PARENT"
    assert card["source_subtask_id"] == "T-CHILD"

    parent_knowledge = (
        await client.get(
            f"{API}/tasks/T-PARENT/knowledge?include_subtasks=true", headers=human_headers()
        )
    ).json()
    assert [k["id"] for k in parent_knowledge] == [card["id"]]
    assert parent_knowledge[0]["source_subtask_id"] == "T-CHILD"

    own_only = (
        await client.get(f"{API}/tasks/T-PARENT/knowledge", headers=human_headers())
    ).json()
    assert own_only == []


async def test_agent_must_be_task_participant(client, app):
    await _seed(client)
    await _task(client, "T-PARENT2")
    await _task(client, "T-CHILD2", parent_id="T-PARENT2", assignees=["qa-role"])
    r = await _knowledge(
        client, "T-CHILD2", _agent_headers(app, role="other-role", token="other-kb-token")
    )
    assert r.status_code == 403


async def test_deep_aggregation(client, app):
    await _seed(client)
    await _task(client, "T-P")
    await _task(client, "T-C", parent_id="T-P", assignees=["qa-role"])
    await _task(client, "T-GC", parent_id="T-C", assignees=["qa-role"])
    r = await _knowledge(client, "T-GC", _agent_headers(app))
    assert r.status_code == 201, r.text

    direct = (
        await client.get(f"{API}/tasks/T-P/knowledge?include_subtasks=true", headers=human_headers())
    ).json()
    deep = (
        await client.get(
            f"{API}/tasks/T-P/knowledge?include_subtasks=true&deep=true", headers=human_headers()
        )
    ).json()
    assert direct == []
    assert [k["source_subtask_id"] for k in deep] == ["T-GC"]


async def test_delete_task_archives_knowledge(client, app):
    await _seed(client)
    await _task(client, "T-DEL", assignees=["qa-role"])
    created = await _knowledge(client, "T-DEL", _agent_headers(app))
    assert created.status_code == 201
    kid = created.json()["id"]
    deleted = await client.delete(f"{API}/tasks/T-DEL", headers=human_headers())
    assert deleted.status_code in (200, 204)
    conn = sqlite3.connect(app.state.db_path)
    try:
        row = conn.execute("SELECT status FROM task_knowledge WHERE id=?", (kid,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "archived"
