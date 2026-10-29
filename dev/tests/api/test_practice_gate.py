"""v1.30 个人知识实践门控：无成功实践 → draft，不进入检索/CIC。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())


def _agent_headers(app, role: str = "qa-role", token: str = "practice-agent-token") -> dict:
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


async def _done_task(client, tid: str = "T-PRACTICE"):
    r = await client.post(f"{API}/tasks", json=task_payload(id_=tid), headers=human_headers())
    assert r.status_code == 201, r.text
    assert (await client.post(f"{API}/tasks/{tid}/claim", json={}, headers=auth_headers())).status_code == 200
    assert (await client.post(
        f"{API}/tasks/{tid}/submit", json={"summary": "done"}, headers=auth_headers()
    )).status_code == 200
    assert (await client.post(
        f"{API}/tasks/{tid}/review", json={"approve": True}, headers=human_headers()
    )).status_code == 200


async def test_memory_without_evidence_is_draft(client, app):
    await _seed(client)
    headers = _agent_headers(app)
    r = await client.post(
        f"{API}/me/memory", json={"content": "未经实践的猜测"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "draft"
    hits = (await client.get(f"{API}/me/memory/search?q=猜测", headers=headers)).json()
    assert hits == []


async def test_memory_with_successful_practice_is_active(client, app):
    await _seed(client)
    await _done_task(client)
    headers = _agent_headers(app)
    r = await client.post(
        f"{API}/me/memory",
        json={
            "content": "成功实践：提交前先跑 pytest",
            "practice_task_id": "T-PRACTICE",
            "outcome": "pytest 全绿",
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    hits = (await client.get(f"{API}/me/memory/search?q=pytest", headers=headers)).json()
    assert any("pytest" in h["content"] for h in hits)

    conn = sqlite3.connect(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT status, practice_task_id, outcome FROM memories "
            "WHERE content LIKE '%成功实践%'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("active", "T-PRACTICE", "pytest 全绿")


async def test_failed_practice_stays_draft(client, app):
    await _seed(client)
    r = await client.post(f"{API}/tasks", json=task_payload(id_="T-OPEN"), headers=human_headers())
    assert r.status_code == 201
    headers = _agent_headers(app, token="practice-failed-token")
    r = await client.post(
        f"{API}/me/memory",
        json={"content": "失败实践不该固化", "practice_task_id": "T-OPEN", "outcome": "未完成"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "draft"


async def test_note_draft_is_not_searchable(client, app):
    await _seed(client)
    headers = _agent_headers(app, token="practice-note-token")
    r = await client.post(
        f"{API}/me/notes",
        json={"path": "draft.md", "content": "未验证的笔记", "kind": "note", "scope": "role"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    hits = (
        await client.get(f"{API}/me/knowledge/search?q=未验证", headers=headers)
    ).json()
    assert hits.get("results") == []

    conn = sqlite3.connect(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM notes WHERE role_slug='qa-role' AND path='draft.md'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "draft"


async def test_updating_active_note_does_not_downgrade(client, app):
    """无证据写入不能覆盖已验证笔记：应拒绝，且原知识保持可检索。"""
    await _seed(client)
    r = await client.post(
        f"{API}/me/notes",
        json={"path": "stable.md", "content": "已验证知识 v1",
              "kind": "note", "scope": "role"},
        headers=auth_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    headers = _agent_headers(app, token="practice-update-token")
    r = await client.post(
        f"{API}/me/notes",
        json={"path": "stable.md", "content": "未验证的 v2 补充",
              "kind": "note", "scope": "role"},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    hits = (await client.get(
        f"{API}/me/knowledge/search?q=已验证知识", headers=headers)).json()
    assert hits.get("results")
    assert all("未验证的 v2" not in h["content"] for h in hits["results"])


async def test_note_with_practice_is_active(client, app):
    await _seed(client)
    await _done_task(client, "T-NOTE")
    headers = _agent_headers(app, token="practice-note-active-token")
    r = await client.post(
        f"{API}/me/notes",
        json={
            "path": "verified.md", "content": "经过验证的笔记", "kind": "note", "scope": "role",
            "practice_task_id": "T-NOTE", "outcome": "测试通过",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    hits = (
        await client.get(f"{API}/me/knowledge/search?q=经过验证", headers=headers)
    ).json()
    assert hits.get("results")
