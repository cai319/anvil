"""L2 · 记忆/笔记/共享经验/提案（design §8/§9，SE §3.2/§3.4，M4 骨架）。

契约（SE §3.2）：`POST /api/v1/me/memory`（append + 更新 FTS）、`GET /me/memory/search?q=`
（FTS5 own+shared）、`POST /me/notes`、`POST /api/v1/proposals`、`POST /me/instruction`。
"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, human_headers  # noqa: E402

pytestmark = pytest.mark.usefixtures("client")


async def test_memory_append_and_search(client):
    """append 写自身 MEMORY.md 并更新 FTS；search 能命中（FTS5 own+shared）。"""
    app = await client.post(
        f"{API}/me/memory", json={"content": "pytest 前必须 docker compose up -d db"}, headers=auth_headers()
    )
    assert app.status_code == 200
    s = await client.get(f"{API}/me/memory/search", params={"q": "docker"}, headers=auth_headers())
    assert s.status_code == 200
    assert any("docker" in (r.get("content", "") or "") for r in s.json())


async def test_memory_search_scoped_to_own_and_shared(client):
    """search 只返回 own + shared，不越界到他人私有记忆（§8.4 owner 过滤）。"""
    s = await client.get(f"{API}/me/memory/search", params={"q": "secret"}, headers=auth_headers())
    assert s.status_code == 200
    for r in s.json():
        assert r.get("owner") in ("qa-role", "shared")


async def test_note_write(client):
    """note write 写 notes/ 并更新 FTS（§7.2 anvil note write）。"""
    r = await client.post(
        f"{API}/me/notes", json={"path": "alembic-gotchas.md", "content": "# 坑\n..."}, headers=auth_headers()
    )
    assert r.status_code == 200


async def test_proposal_create_and_approve(client):
    """提案创建→审批（§9 三档权限的审批档）。"""
    r = await client.post(
        f"{API}/proposals",
        json={"field": "tools", "proposed": {"allowlist": []}, "rationale": "x", "risk": "y"},
        headers=auth_headers(),
    )
    assert r.status_code == 201
    pid = r.json().get("id")
    assert pid
    approve = await client.post(f"{API}/proposals/{pid}/approve", headers=human_headers())
    assert approve.status_code == 200


async def test_lesson_propose_and_80_cap(client):
    """lesson propose 进待审队列；80 条上限时新增须先淘汰（§8.3）。"""
    r = await client.post(
        f"{API}/lessons/pending", json={"content": "运行测试前先起 db", "tags": ["testing"]}, headers=auth_headers()
    )
    assert r.status_code == 201
    pending = await client.get(f"{API}/lessons/pending", headers=auth_headers())
    assert pending.status_code == 200
    # 80 条上限淘汰逻辑在 M4 落地后按 last_hit_at 断言
