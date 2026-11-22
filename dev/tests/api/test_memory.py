"""L2 · 记忆/笔记/共享经验/提案（design §8/§9，SE §3.2/§3.4，M4 骨架）。

契约（SE §3.2）：`POST /api/v1/me/memory`（append + 更新 FTS）、`GET /me/memory/search?q=`
（FTS5 own+shared）、`POST /me/notes`、`POST /api/v1/proposals`、`POST /me/instruction`。
"""
from __future__ import annotations

import json

import pytest

from anvil import config
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
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
        f"{API}/lessons/pending",
        json={"title": "运行测试前先起 db", "content": "运行测试前先起 db", "tags": ["testing"],
              "fields": {
                  "use_when": "运行测试前", "expected_effect": "避免连接失败",
                  "evidence": "测试记录", "observation": "测试失败",
                  "lesson": "先起 db", "applicability": "同类测试",
                  "counterexamples": "暂无",
              }},
        headers=auth_headers()
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


async def test_note_compact_card_fields_visible_in_detail(client):
    """角色知识也有完整知识卡字段：详情/检索不能只给一段正文。"""
    created = await client.post(
        f"{API}/me/notes",
        json={
            "path": "decisions/x509.md",
            "content": "## 观察\n发行版 X509 校验行为不同。",
            "kind": "decision", "scope": "role",
            "title": "X509 先核对发行版环境",
            "tldr": "环境差异可能被误判为漏洞。",
            "fields": {
                "use_when": "审计 X509 且使用发行版 OpenSSL 时",
                "evidence": "T-1 / run r-1",
                "trigger_point": "读取证书链校验结果时",
            },
            "tags": ["x509"],
        },
        headers=auth_headers())
    assert created.status_code == 200, created.text
    note = created.json()
    assert note["title"] == "X509 先核对发行版环境"
    assert note["tldr"] == "环境差异可能被误判为漏洞。"
    assert note["fields"]["trigger_point"] == "读取证书链校验结果时"

    detail = await client.get(
        f"{API}/me/knowledge/{note['id']}", headers=auth_headers())
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["title"] == "X509 先核对发行版环境"
    assert body["fields"] == note["fields"]
    assert "发行版 X509" in body["content"]

    hits = await client.get(
        f"{API}/me/knowledge/search", params={"q": "X509"},
        headers=auth_headers())
    assert hits.status_code == 200
    hit = next(item for item in hits.json()["results"] if item["id"] == note["id"])
    assert hit["fields"]["use_when"] == "审计 X509 且使用发行版 OpenSSL 时"


async def test_note_append_preserves_existing_fields(client):
    first = await client.post(
        f"{API}/me/notes",
        json={"path": "decisions/preserve.md", "content": "第一条",
              "kind": "decision", "scope": "role",
              "fields": {"use_when": "保留字段"}},
        headers=auth_headers())
    assert first.status_code == 200, first.text
    nid = first.json()["id"]
    appended = await client.post(
        f"{API}/me/notes",
        json={"path": "decisions/preserve.md", "content": "追加一条",
              "kind": "decision", "scope": "role", "mode": "append"},
        headers=auth_headers())
    assert appended.status_code == 200, appended.text
    detail = await client.get(f"{API}/me/knowledge/{nid}", headers=auth_headers())
    assert detail.status_code == 200
    assert detail.json()["fields"] == {"use_when": "保留字段"}


async def test_reindex_restores_note_fields_from_file(client, app):
    """文件是真相：DB 行丢失后 reindex 仍能从文件元数据恢复结构化字段。"""
    from anvil.db import open_conn

    created = await client.post(
        f"{API}/me/notes",
        json={"path": "decisions/reindex.md", "content": "正文",
              "kind": "decision", "scope": "role",
              "title": "文件真相标题", "tldr": "文件真相摘要",
              "fields": {"use_when": "重建索引时"}},
        headers=auth_headers())
    assert created.status_code == 200, created.text
    note_id = created.json()["id"]
    conn = open_conn(app.state.db_path)
    try:
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        conn.commit()
    finally:
        conn.close()

    rebuilt = await client.post(
        f"{API}/me/knowledge/reindex", params={"role": "qa-role"},
        headers=auth_headers())
    assert rebuilt.status_code == 200, rebuilt.text
    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT title, tldr, fields_json FROM notes "
            "WHERE role_slug='qa-role' AND path='decisions/reindex.md'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["title"] == "文件真相标题"
    assert row["tldr"] == "文件真相摘要"
    assert json.loads(row["fields_json"]) == {"use_when": "重建索引时"}


async def test_delete_role_knowledge_removes_index_and_file(client):
    created = await client.post(
        f"{API}/me/notes",
        json={"path": "decisions/delete-me.md", "content": "待删除知识",
              "kind": "decision", "scope": "role",
              "fields": {"use_when": "删除测试"}},
        headers=auth_headers())
    assert created.status_code == 200, created.text
    note = created.json()
    note_file = config.AGENTS_DIR / "qa-role" / "notes" / "decisions" / "delete-me.md"
    assert note_file.exists()

    deleted = await client.delete(
        f"{API}/me/knowledge/{note['id']}?role=qa-role", headers=human_headers())
    assert deleted.status_code == 204, deleted.text
    assert not note_file.exists()
    hidden = await client.get(
        f"{API}/me/knowledge/{note['id']}", headers=auth_headers())
    assert hidden.status_code == 404


async def test_role_knowledge_smart_completion(client, app):
    """角色知识也可让角色用自身上下文补全/精炼；角色忙碌时排队。"""
    from anvil.db import open_conn

    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())
    created = await client.post(
        f"{API}/me/notes",
        json={"path": "modules/auth.md", "content": "旧正文",
              "kind": "module_knowledge", "scope": "role",
              "title": "旧标题", "tldr": "旧摘要",
              "fields": {"use_when": "认证模块"}},
        headers=auth_headers())
    assert created.status_code == 200, created.text
    note = created.json()

    requested = await client.post(
        f"{API}/me/knowledge/{note['id']}/request-completion",
        params={"role": "qa-role"}, headers=human_headers())
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert body["status"] == "requested" and body["role"] == "qa-role"
    assert body["run_id"] and "queued" in body

    conn = open_conn(app.state.db_path)
    try:
        run = conn.execute(
            "SELECT prompt FROM runs WHERE id=?", (body["run_id"],)).fetchone()
    finally:
        conn.close()
    assert run is not None
    assert note["id"] in run["prompt"]
    assert "anvil knowledge fill" in run["prompt"]

    filled = await client.post(
        f"{API}/me/knowledge/{note['id']}/fill",
        params={"role": "qa-role"},
        json={"title": "认证模块知识", "tldr": "精炼摘要",
              "content": "精炼后的正文与证据路径",
              "fields": {
                  "use_when": "认证模块任务", "expected_effect": "减少重复排查",
                  "evidence": "任务 T-1 的实践记录",
              }},
        headers=auth_headers("qa-role"))
    assert filled.status_code == 200, filled.text
    updated = filled.json()
    assert updated["title"] == "认证模块知识"
    assert updated["tldr"] == "精炼摘要"
    assert updated["fields"]["use_when"] == "认证模块任务"
    assert updated["completion"]["state"] == "completed"
    assert updated["ready"] is True


async def test_role_knowledge_partial_fill_is_rejected_without_completing(
        client, app):
    """部分字段补全仍不满足必须条款：422，且不能把 job 标成 completed。"""
    from anvil.db import open_conn
    from anvil.services import knowledge_completion

    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())
    created = await client.post(
        f"{API}/me/notes",
        json={"path": "modules/partial.md", "content": "旧正文",
              "kind": "module_knowledge", "scope": "role",
              "title": "旧标题", "tldr": "旧摘要",
              "fields": {"use_when": "认证模块"}},
        headers=auth_headers())
    assert created.status_code == 200, created.text
    note = created.json()
    requested = await client.post(
        f"{API}/me/knowledge/{note['id']}/request-completion",
        params={"role": "qa-role"}, headers=human_headers())
    assert requested.status_code == 200, requested.text

    partial = await client.post(
        f"{API}/me/knowledge/{note['id']}/fill",
        params={"role": "qa-role"},
        json={"title": "只有标题和场景",
              "fields": {"use_when": "认证模块任务"}},
        headers=auth_headers("qa-role"))
    assert partial.status_code == 422, partial.text
    assert "知识格式不合法" in str(partial.json()["detail"])

    conn = open_conn(app.state.db_path)
    try:
        state = knowledge_completion.status_for(
            conn, "role_note", note["id"], ready=False)
    finally:
        conn.close()
    assert state is not None and state["state"] != "completed"


async def test_knowledge_completion_job_is_deduplicated_and_reconciled(
        client, app):
    """同一目标只有一个 active job；目标删除后对账为 failed。"""
    from anvil.db import open_conn
    from anvil.services import knowledge_completion

    conn = open_conn(app.state.db_path)
    try:
        first = knowledge_completion.create_job(
            conn, target_type="lesson", target_id="L-DEDUPE")
        second = knowledge_completion.create_job(
            conn, target_type="lesson", target_id="L-DEDUPE")
        assert first["id"] == second["id"]
        knowledge_completion.complete(conn, "lesson", "L-DEDUPE")
        assert knowledge_completion.status_for(
            conn, "lesson", "L-DEDUPE")["state"] == "completed"

        orphan = knowledge_completion.create_job(
            conn, target_type="lesson", target_id="L-MISSING")
        result = knowledge_completion.reconcile(conn)
        assert result["failed"] >= 1
        state = knowledge_completion.status_for(
            conn, "lesson", "L-MISSING")
        assert state is not None and state["state"] == "failed"
        assert orphan["id"]
    finally:
        conn.close()


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
    # 直接编辑文件后由 human 全量 reindex 确认（Agent 侧 reindex 受实践门控）。
    rr = await client.post(f"{API}/knowledge/reindex", json={"role": "qa-role"},
                           headers=human_headers())
    assert rr.status_code == 200
    assert rr.json()["roles"][0]["scanned"] >= 1
    assert rr.json()["roles"][0]["updated"] >= 1

    hits = await client.get(f"{API}/me/knowledge/search",
                            params={"q": "REINDEXUNIQUE"}, headers=auth_headers())
    assert any(h["path"] == "modules/legacy.md" for h in hits.json()["results"])
