"""L2 · 记忆/笔记/共享经验/提案（design §8/§9，SE §3.2/§3.4，M4 骨架）。

契约（SE §3.2）：`POST /api/v1/me/memory`（append + 更新 FTS）、`GET /me/memory/search?q=`
（FTS5 own+shared）、`POST /me/notes`、`POST /api/v1/proposals`、`POST /me/instruction`。
"""
from __future__ import annotations

import pytest

from anvil import config
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture(autouse=True)
def isolate_agent_home(tmp_path, monkeypatch):
    """note/reindex 会写角色 home；测试必须落在临时目录。"""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")


async def _seed_knowledge_tasks(client):
    for tid in ("K-1", "K-2", "K-3"):
        r = await client.post(
            f"{API}/tasks",
            json=task_payload(id_=tid, title=f"JWT {tid}", tags=["jwt"],
                              repo="openssl"),
            headers=auth_headers())
        assert r.status_code == 201
    for tid in ("K-1", "K-2"):
        r = await client.patch(
            f"{API}/tasks/{tid}", json={"assignee": "qa-role"},
            headers=human_headers())
        assert r.status_code == 200


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


# ---- 知识库：scope 隔离 / 元数据 / 检索 / reindex -------------------------


async def test_knowledge_note_scopes_isolation_and_show(client):
    await _seed_knowledge_tasks(client)
    snap = await client.post(
        f"{API}/me/notes",
        json={"path": "knowledge/K-1/snap.md", "content": "K1SNAPUNIQUE jwt",
              "kind": "task_snapshot", "scope": "task", "task_id": "K-1"},
        headers=auth_headers())
    assert snap.status_code == 200
    decision = await client.post(
        f"{API}/me/notes",
        json={"path": "knowledge/K-1/decision.md", "content": "K1DECISIONUNIQUE jwt",
              "kind": "decision", "scope": "task", "task_id": "K-1"},
        headers=auth_headers())
    pattern = await client.post(
        f"{API}/me/notes",
        json={"path": "vulns/jwt.md",
              "content": "PATTERNUNIQUE jwt algorithm confusion",
              "kind": "vulnerability_pattern", "scope": "role",
              "tags": ["jwt"], "repo": "openssl", "confidence": 0.8},
        headers=auth_headers())
    assert decision.status_code == 200 and pattern.status_code == 200
    snap_id = snap.json()["id"]
    decision_id = decision.json()["id"]
    pattern_id = pattern.json()["id"]

    # 当前任务：决策 + role 模式；快照默认不混进“相关记忆”
    r1 = await client.get(f"{API}/me/knowledge/search",
                          params={"q": "jwt", "task_id": "K-1"},
                          headers=auth_headers())
    assert r1.status_code == 200
    ids1 = {h["id"] for h in r1.json()["results"]}
    assert decision_id in ids1 and pattern_id in ids1 and snap_id not in ids1
    # 显式要求时才返回当前任务快照
    r1b = await client.get(
        f"{API}/me/knowledge/search",
        params={"q": "jwt", "task_id": "K-1", "include_snapshots": True},
        headers=auth_headers())
    assert snap_id in {h["id"] for h in r1b.json()["results"]}

    # 另一个任务：只有 role 模式，绝无 K-1 的任务行
    r2 = await client.get(f"{API}/me/knowledge/search",
                          params={"q": "jwt", "task_id": "K-2"},
                          headers=auth_headers())
    ids2 = {h["id"] for h in r2.json()["results"]}
    assert pattern_id in ids2
    assert decision_id not in ids2 and snap_id not in ids2

    # 无任务会话（reminder/直聊）：只含 role-scoped
    r3 = await client.get(f"{API}/me/knowledge/search", params={"q": "jwt"},
                          headers=auth_headers())
    ids3 = {h["id"] for h in r3.json()["results"]}
    assert pattern_id in ids3
    assert decision_id not in ids3 and snap_id not in ids3

    # show 也执行同样的隔离
    assert (await client.get(f"{API}/me/knowledge/{snap_id}",
                             params={"task_id": "K-1"},
                             headers=auth_headers())).status_code == 200
    assert (await client.get(f"{API}/me/knowledge/{snap_id}",
                             params={"task_id": "K-2"},
                             headers=auth_headers())).status_code == 404


async def test_knowledge_search_validation_and_task_permission(client):
    await _seed_knowledge_tasks(client)

    r = await client.get(f"{API}/me/knowledge/search",
                         params={"q": "jwt", "scope": "task"},
                         headers=auth_headers())
    assert r.status_code == 422  # scope=task 必须有 task_id
    r = await client.get(f"{API}/me/knowledge/search",
                         params={"q": "jwt", "scope": "bogus"},
                         headers=auth_headers())
    assert r.status_code == 422
    r = await client.get(f"{API}/me/knowledge/search",
                         params={"q": "jwt", "include_snapshots": True},
                         headers=auth_headers())
    assert r.status_code == 422

    # K-3 未绑定 qa-role：角色级 token 不能借 task_id 越权读取
    r = await client.get(f"{API}/me/knowledge/search",
                         params={"q": "jwt", "task_id": "K-3"},
                         headers=auth_headers())
    assert r.status_code == 403
    # 自然人可浏览
    r = await client.get(f"{API}/me/knowledge/search",
                         params={"q": "jwt", "task_id": "K-3", "role": "qa-role"},
                         headers=human_headers())
    assert r.status_code == 200


async def test_note_path_validation_and_metadata(client):
    for bad in ("../evil.md", "/abs.md", "a\\..\\evil.md", "", "knowledge/../../x.md"):
        r = await client.post(f"{API}/me/notes",
                              json={"path": bad, "content": "x"},
                              headers=auth_headers())
        assert r.status_code == 422, bad

    r = await client.post(
        f"{API}/me/notes",
        json={"path": "failures/race.md", "content": "race window",
              "kind": "failure_scenario", "scope": "role",
              "tags": ["race", "concurrency"], "repo": "openssl",
              "module": "auth", "confidence": 0.7},
        headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "failure_scenario" and body["scope"] == "role"
    assert body["tags"] == ["race", "concurrency"]
    assert body["repo"] == "openssl" and body["module"] == "auth"
    assert body["confidence"] == pytest.approx(0.7)


async def test_knowledge_index_and_reindex(client):
    await _seed_knowledge_tasks(client)
    await client.post(
        f"{API}/me/notes",
        json={"path": "knowledge/K-1/decision.md", "content": "K1DECISIONUNIQUE",
              "kind": "decision", "scope": "task", "task_id": "K-1"},
        headers=auth_headers())
    await client.post(
        f"{API}/me/notes",
        json={"path": "vulns/jwt.md", "content": "PATTERNUNIQUE",
              "kind": "vulnerability_pattern", "scope": "role", "tags": ["jwt"]},
        headers=auth_headers())

    idx = await client.get(f"{API}/me/knowledge/index",
                           params={"task_id": "K-1"}, headers=auth_headers())
    assert idx.status_code == 200
    data = idx.json()
    assert data["role_counts"].get("vulnerability_pattern") == 1
    assert data["task_counts"].get("decision") == 1

    # 直接编辑文件后通过 reindex 同步（文件是真相）
    home = config.AGENTS_DIR / "qa-role" / "notes" / "modules"
    home.mkdir(parents=True, exist_ok=True)
    (home / "legacy.md").write_text("REINDEXUNIQUE module knowledge", encoding="utf-8")
    rr = await client.post(f"{API}/me/knowledge/reindex", headers=auth_headers())
    assert rr.status_code == 200
    assert rr.json()["scanned"] >= 1 and rr.json()["updated"] >= 1

    hits = await client.get(f"{API}/me/knowledge/search",
                            params={"q": "REINDEXUNIQUE"}, headers=auth_headers())
    assert any(h["path"] == "modules/legacy.md" for h in hits.json()["results"])
