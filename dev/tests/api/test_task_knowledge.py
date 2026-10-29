"""v1.30 任务共享知识：参与角色共享、子任务标记、父任务聚合。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil import config, db
from anvil.services import task_knowledge

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


async def test_delete_requires_matching_task(client, app):
    """DELETE 的 tid 必须与知识卡所属任务一致。"""
    await _seed(client)
    await _task(client, "T-OWN-A")
    await _task(client, "T-OWN-B", assignees=["qa-role"])
    card = (await _knowledge(client, "T-OWN-B", _agent_headers(app))).json()
    deleted = await client.delete(
        f"{API}/tasks/T-OWN-A/knowledge/{card['id']}", headers=human_headers())
    assert deleted.status_code == 404
    remaining = (
        await client.get(f"{API}/tasks/T-OWN-B/knowledge", headers=human_headers())
    ).json()
    assert card["id"] in {k["id"] for k in remaining}


async def test_agent_must_be_task_participant(client, app):
    await _seed(client)
    await _task(client, "T-PARENT2")
    await _task(client, "T-CHILD2", parent_id="T-PARENT2", assignees=["qa-role"])
    r = await _knowledge(
        client, "T-CHILD2", _agent_headers(app, role="other-role", token="other-kb-token")
    )
    assert r.status_code == 403


async def test_agent_must_be_participant_to_read(client, app):
    """任务知识只对参与角色、planner/onboarding 与 human 可读。"""
    await _seed(client)
    await _task(client, "T-READ", assignees=["qa-role"])
    r = await client.get(
        f"{API}/tasks/T-READ/knowledge",
        headers=_agent_headers(app, role="other-role", token="other-read-token"))
    assert r.status_code == 403
    ok = await client.get(
        f"{API}/tasks/T-READ/knowledge",
        headers=_agent_headers(app, role="qa-role", token="qa-read-token"))
    assert ok.status_code == 200
    human = await client.get(f"{API}/tasks/T-READ/knowledge", headers=human_headers())
    assert human.status_code == 200


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


async def test_render_increments_single_card_hits(client, app, monkeypatch):
    await _seed(client)
    await _task(client, "T-HIT", assignees=["qa-role"])
    created = await _knowledge(client, "T-HIT", _agent_headers(app))
    assert created.status_code == 201, created.text
    kid = created.json()["id"]
    monkeypatch.setattr(config, "TASK_KNOWLEDGE_ENABLED", True)
    conn = db.open_conn(app.state.db_path)
    try:
        assert "任务共享知识" in task_knowledge.render_for_task(conn, "T-HIT")
        task_knowledge.render_for_task(conn, "T-HIT")
        row = conn.execute(
            "SELECT hit_count, last_hit_at FROM task_knowledge WHERE id=?", (kid,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["hit_count"] == 2
    assert row["last_hit_at"]


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
