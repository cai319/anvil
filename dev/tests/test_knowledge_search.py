"""角色知识库检索：任务隔离、scope/kind 过滤、排序、provenance 与 LIKE 回退。"""
from __future__ import annotations

import sqlite3
from collections import Counter

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import knowledge, memory
from anvil.util import now_iso


@pytest.fixture
def env(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    (agents / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db_path = str(tmp_path / "knowledge.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,repo,scope_paths_json,created_at,updated_at) "
        "VALUES ('TA','Auth JWT audit','in_progress','[\"security\",\"jwt\"]','openssl',"
        "'[\"src/auth/**\"]',?,?)", (now, now))
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,repo,created_at,updated_at) "
        "VALUES ('TB','Admin JWT console','in_progress','[\"jwt\"]','console',?,?)",
        (now, now))
    conn.commit()
    yield conn, tmp_path, db_path
    conn.close()


def _write_task_snapshot(conn, task_id, marker):
    return memory.write_note(
        conn, "dev", f"knowledge/{task_id}/snapshot.md",
        f"{marker} jwt strict 口径", kind="task_snapshot", scope="task",
        task_id=task_id, tags=["jwt"], confidence=0.9)


def _write_role_pattern(conn, marker="ROLE-JWT-PATTERN"):
    return memory.write_note(
        conn, "dev", "vulns/jwt.md",
        f"{marker} jwt algorithm confusion pattern",
        kind="vulnerability_pattern", scope="role", tags=["jwt"],
        repo="openssl", module="auth", confidence=0.8)


def test_task_scope_is_isolated_between_tasks(env):
    """任务 A 的快照/决策绝不出现在任务 B 或无任务检索中；role 知识跨任务可见。"""
    conn, _, _ = env
    snapshot = _write_task_snapshot(conn, "TA", "TA-SNAPSHOT-MARKER")
    decision = memory.write_note(
        conn, "dev", "knowledge/TA/decision.md", "TA-DECISION-MARKER jwt 决策",
        kind="decision", scope="task", task_id="TA")
    pattern = _write_role_pattern(conn)

    hits_a = knowledge.search_knowledge(conn, "dev", "jwt", task_id="TA", scope="auto")
    assert {h["id"] for h in hits_a} >= {decision["id"], pattern["id"]}
    # 快照默认不参与“相关记忆”，只由 CIC 的任务块单独取
    assert snapshot["id"] not in {h["id"] for h in hits_a}
    with_snapshot = knowledge.search_knowledge(
        conn, "dev", "jwt", task_id="TA", scope="auto", include_snapshots=True)
    assert snapshot["id"] in {h["id"] for h in with_snapshot}

    hits_b = knowledge.search_knowledge(conn, "dev", "jwt", task_id="TB", scope="auto")
    ids_b = {h["id"] for h in hits_b}
    assert pattern["id"] in ids_b
    assert decision["id"] not in ids_b and snapshot["id"] not in ids_b

    hits_global = knowledge.search_knowledge(conn, "dev", "jwt", task_id=None, scope="auto")
    ids_global = {h["id"] for h in hits_global}
    assert pattern["id"] in ids_global
    assert decision["id"] not in ids_global and snapshot["id"] not in ids_global

    # scope=task 只返回当前任务；没有 task_id 时不返回任何 task-scoped 行
    task_b = knowledge.search_knowledge(conn, "dev", "jwt", task_id="TB", scope="task")
    assert task_b == []
    assert knowledge.search_knowledge(conn, "dev", "jwt", scope="task") == []


def test_metadata_filters_and_provenance(env):
    conn, _, _ = env
    pattern = _write_role_pattern(conn)
    memory.write_note(
        conn, "dev", "vulns/race.md", "jwt race condition failure scenario",
        kind="failure_scenario", scope="role", tags=["race"], repo="other")

    hits = knowledge.search_knowledge(
        conn, "dev", "jwt", kinds=("vulnerability_pattern",), tags=("jwt",),
        repo="openssl", module="auth", limit=5)
    assert [h["id"] for h in hits] == [pattern["id"]]
    hit = hits[0]
    assert hit["scope"] == "role" and hit["kind"] == "vulnerability_pattern"
    assert hit["tags"] == ["jwt"] and hit["confidence"] == pytest.approx(0.8)
    assert hit["provenance"]["path"] == "vulns/jwt.md"
    assert hit["provenance"]["repo"] == "openssl"
    assert len(hit["snippet"]) <= 400
    assert "jwt" in hit["snippet"].lower()


def test_ranking_prefers_metadata_matches(env):
    """同样命中关键词时，repo/tag/kind/module 匹配的笔记排在前面。"""
    conn, _, _ = env
    good = memory.write_note(
        conn, "dev", "vulns/jwt.md", "jwt token validation",
        kind="vulnerability_pattern", scope="role", tags=["jwt"],
        repo="openssl", module="auth", confidence=0.9)
    memory.write_note(
        conn, "dev", "notes/misc.md", "jwt " + ("填充内容" * 2000),
        kind="note", scope="role", repo="other", confidence=0.2)

    hits = knowledge.search_knowledge(conn, "dev", "jwt", task_id="TA",
                                      scope="auto", limit=1)
    assert hits and hits[0]["id"] == good["id"]
    assert "repo" in hits[0]["why"] or "kind:vulnerability_pattern" in hits[0]["why"]


def test_get_knowledge_isolates_task_scope(env):
    conn, _, _ = env
    snapshot = _write_task_snapshot(conn, "TA", "TA-SNAPSHOT-MARKER")
    pattern = _write_role_pattern(conn)

    assert knowledge.get_knowledge(conn, "dev", snapshot["id"], task_id="TB") is None
    assert knowledge.get_knowledge(conn, "dev", snapshot["id"], task_id="TA")["id"] == snapshot["id"]
    assert knowledge.get_knowledge(conn, "dev", pattern["id"], task_id="TB")["id"] == pattern["id"]
    # 角色隔离：其它角色即使知道 id 也读不到
    assert knowledge.get_knowledge(conn, "tester", pattern["id"]) is None


def test_knowledge_index_counts_role_and_current_task(env):
    conn, _, _ = env
    _write_task_snapshot(conn, "TA", "TA-SNAPSHOT-MARKER")
    memory.write_note(conn, "dev", "knowledge/TA/decision.md", "TA decision",
                      kind="decision", scope="task", task_id="TA")
    _write_role_pattern(conn)

    idx_a = knowledge.knowledge_index(conn, "dev", "TA")
    assert idx_a["role_counts"].get("vulnerability_pattern") == 1
    assert idx_a["task_counts"].get("task_snapshot") == 1
    assert idx_a["task_counts"].get("decision") == 1
    assert idx_a["total_role"] == 1 and idx_a["total_task"] == 2

    idx_b = knowledge.knowledge_index(conn, "dev", "TB")
    assert idx_b["task_counts"] == {}
    assert idx_b["role_counts"].get("vulnerability_pattern") == 1


def test_like_fallback_when_fts_unavailable(env):
    """FTS5 表被删/不可用时，检索退化为带 scope 过滤的 LIKE，仍然不串任务。"""
    conn, _, _ = env
    pattern = _write_role_pattern(conn, "FALLBACK-JWT-PATTERN")
    snapshot = _write_task_snapshot(conn, "TA", "FALLBACK-TASK-SNAPSHOT")
    conn.execute("DROP TABLE notes_fts")
    conn.commit()

    hits = knowledge.search_knowledge(conn, "dev", "FALLBACK-JWT-PATTERN")
    assert [h["id"] for h in hits] == [pattern["id"]]
    task_hits = knowledge.search_knowledge(
        conn, "dev", "FALLBACK-TASK-SNAPSHOT", task_id="TB", scope="auto")
    assert snapshot["id"] not in {h["id"] for h in task_hits}


def test_search_memory_merges_role_notes_but_not_other_task_rows(env):
    """anvil memory search 兼容入口：MEMORY + role notes；task 内容需显式 task_id。"""
    conn, _, _ = env
    memory.append_memory(conn, "dev", "MEMORYCOMPATUNIQUE")
    role_note = _write_role_pattern(conn, "ROLECOMPATUNIQUE")
    task_note = memory.write_note(
        conn, "dev", "knowledge/TA/decision.md", "TASKONLYUNIQUE jwt",
        kind="decision", scope="task", task_id="TA")
    snapshot = _write_task_snapshot(conn, "TA", "SNAPSHOTONLYUNIQUE")

    role_hits = memory.search_memory(conn, "dev", "ROLECOMPATUNIQUE")
    assert any(h["content"].startswith("ROLECOMPATUNIQUE") for h in role_hits)
    assert any(h.get("path") == "vulns/jwt.md" for h in role_hits)
    assert role_note["scope"] == "role"
    # task-scoped 默认不进入角色级搜索
    assert memory.search_memory(conn, "dev", "TASKONLYUNIQUE") == []
    assert memory.search_memory(conn, "dev", "SNAPSHOTONLYUNIQUE") == []
    # 显式指定任务后才包含该任务的决策；快照仍需显式打开
    task_hits = memory.search_memory(conn, "dev", "TASKONLYUNIQUE", task_id="TA")
    assert any(h["content"].startswith("TASKONLYUNIQUE") for h in task_hits)
    snapshot_hits = memory.search_memory(
        conn, "dev", "SNAPSHOTONLYUNIQUE", task_id="TA", include_task_snapshots=True)
    assert any(h["content"].startswith("SNAPSHOTONLYUNIQUE") for h in snapshot_hits)
    assert task_note["scope"] == "task" and snapshot["kind"] == "task_snapshot"


def test_reindex_preserves_hit_stats(env):
    """文件内容变化触发 reindex 时，命中统计不能被清零。"""
    conn, _, _ = env
    home = config.AGENTS_DIR / "dev" / "notes"
    (home / "modules").mkdir(parents=True, exist_ok=True)
    target = home / "modules" / "stats.md"
    target.write_text("HITSTATUNIQUE v1", encoding="utf-8")
    knowledge.reindex_notes(conn, "dev")
    conn.execute(
        "UPDATE notes SET hit_count=7, last_hit_at='2026-01-01T00:00:00+00:00' "
        "WHERE role_slug='dev' AND path='modules/stats.md'")
    conn.commit()

    target.write_text("HITSTATUNIQUE v2 changed", encoding="utf-8")
    knowledge.reindex_notes(conn, "dev")
    row = conn.execute(
        "SELECT hit_count, last_hit_at FROM notes "
        "WHERE role_slug='dev' AND path='modules/stats.md'").fetchone()
    assert row["hit_count"] == 7
    assert row["last_hit_at"] == "2026-01-01T00:00:00+00:00"


def test_reindex_reads_metadata_and_legacy_files(env):
    conn, _, _ = env
    home = config.AGENTS_DIR / "dev" / "notes"
    (home / "modules").mkdir(parents=True, exist_ok=True)
    (home / "modules" / "legacy.md").write_text(
        "LEGACYMODULEUNIQUE legacy module knowledge", encoding="utf-8")
    (home / "failures").mkdir(parents=True, exist_ok=True)
    (home / "failures" / "race.md").write_text(
        '<!-- anvil: {"kind":"failure_scenario","scope":"role","tags":["race"],'
        '"repo":"openssl","confidence":0.7} -->\n'
        "FRONTMATTERFAILUREUNIQUE race window", encoding="utf-8")

    result = knowledge.reindex_notes(conn, "dev")
    assert result["scanned"] >= 2 and result["updated"] >= 2

    legacy = conn.execute(
        "SELECT * FROM notes WHERE role_slug='dev' AND path='modules/legacy.md'"
    ).fetchone()
    assert legacy is not None and legacy["scope"] == "role"
    assert legacy["kind"] == "module_knowledge"
    front = conn.execute(
        "SELECT * FROM notes WHERE role_slug='dev' AND path='failures/race.md'"
    ).fetchone()
    assert front is not None and front["kind"] == "failure_scenario"
    assert front["tags_json"] == '["race"]'
    assert front["confidence"] == pytest.approx(0.7)

    hits = knowledge.search_knowledge(conn, "dev", "LEGACYMODULEUNIQUE")
    assert [h["path"] for h in hits] == ["modules/legacy.md"]


def test_migration_assigns_legacy_notes_to_role_scope(tmp_path, monkeypatch):
    """旧库 notes/ 迁移为 role scope；同 path 去重合并；孤儿快照保留目录名但不注入。"""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, role_slug TEXT NOT NULL, "
        "path TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at) VALUES "
        "('n1','dev','notes/old.md','OLDROLEPARTONE',?)",
        ("2026-01-01T00:00:00+00:00",))
    raw.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at) VALUES "
        "('n3','dev','notes/old.md','OLDROLEPARTTWO',?)",
        ("2026-01-02T00:00:00+00:00",))
    raw.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at) VALUES "
        "('n2','dev','knowledge/GONE/snap.md','ORPHANSNAPSHOT',?)",
        ("2026-01-01T00:00:00+00:00",))
    raw.commit()
    raw.close()

    conn = connect(str(db_path))
    rows = list(conn.execute("SELECT * FROM notes"))
    old_rows = [r for r in rows if r["path"] == "notes/old.md"]
    assert len(old_rows) == 1  # 同 (role,path) 去重
    old = old_rows[0]
    assert old["scope"] == "role" and old["kind"] == "note"
    assert "OLDROLEPARTONE" in old["content"]
    assert "OLDROLEPARTTWO" in old["content"]
    assert old["updated_at"].startswith("2026-01-02")

    orphan = next(r for r in rows if r["path"] == "knowledge/GONE/snap.md")
    assert orphan["scope"] == "unclassified"
    assert orphan["kind"] == "task_snapshot"
    assert orphan["task_id"] is None
    assert orphan["source_task"] == "GONE"  # 保留目录名/来源

    # 普通旧笔记立刻可检索（合并后仍是一条）；孤儿快照不会自动注入
    hits = knowledge.search_knowledge(conn, "dev", "OLDROLEPARTTWO")
    assert len(hits) == 1 and hits[0]["path"] == "notes/old.md"
    assert knowledge.search_knowledge(conn, "dev", "ORPHANSNAPSHOT") == []
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('GONE','同名新任务','todo',?,?)", ("2026-02-01T00:00:00+00:00",) * 2)
    conn.commit()
    assert knowledge.latest_task_snapshot(conn, "dev", "GONE") is None
    conn.close()


def test_write_note_upserts_same_path(env):
    """同 path 只保留一行索引；append 累积正文，replace 覆盖并保留 id。"""
    conn, _, _ = env
    first = memory.write_note(conn, "dev", "notes/evolve.md", "PARTONEUNIQUE")
    second = memory.write_note(conn, "dev", "notes/evolve.md", "PARTTWOUNIQUE")
    assert first["id"] == second["id"]

    rows = conn.execute(
        "SELECT * FROM notes WHERE role_slug='dev' AND path='evolve.md'"
    ).fetchall()
    assert len(rows) == 1
    assert "PARTONEUNIQUE" in rows[0]["content"]
    assert "PARTTWOUNIQUE" in rows[0]["content"]
    file_path = config.AGENTS_DIR / "dev" / "notes" / "evolve.md"
    raw = file_path.read_text(encoding="utf-8")
    assert raw.count("<!-- anvil:") == 1

    hits = knowledge.search_knowledge(conn, "dev", "PARTONEUNIQUE")
    assert len(hits) == 1 and hits[0]["path"] == "evolve.md"
    assert "PARTTWOUNIQUE" in hits[0]["content"]

    third = memory.write_note(
        conn, "dev", "notes/evolve.md", "REPLACEUNIQUE", mode="replace",
        updated_at="2026-01-02T03:04:05+00:00")
    assert third["id"] == first["id"]
    row = conn.execute(
        "SELECT * FROM notes WHERE role_slug='dev' AND path='evolve.md'"
    ).fetchone()
    assert row["content"] == "REPLACEUNIQUE"
    assert row["updated_at"].startswith("2026-01-02")
    raw = file_path.read_text(encoding="utf-8")
    assert raw.count("<!-- anvil:") == 1
    assert "PARTONEUNIQUE" not in raw and "PARTTWOUNIQUE" not in raw


def test_ranking_prefers_same_task_and_confidence(env):
    conn, _, _ = env
    same = memory.write_note(
        conn, "dev", "vulns/same.md", "JWT ranking pattern SAMEONLY",
        kind="vulnerability_pattern", scope="role", task_id="TA",
        tags=["jwt"], repo="openssl", confidence=0.9)
    memory.write_note(
        conn, "dev", "vulns/other.md", "JWT ranking pattern OTHERONLY",
        kind="vulnerability_pattern", scope="role", task_id="TB",
        tags=["jwt"], repo="openssl", confidence=0.9)
    hits = knowledge.search_knowledge(
        conn, "dev", "JWT ranking pattern", task_id="TA", scope="auto", limit=1)
    assert hits and hits[0]["id"] == same["id"]

    high = memory.write_note(
        conn, "dev", "notes/high.md", "JWT confidence HIGHONLYZZ",
        kind="note", scope="role", tags=["jwt"], confidence=1.0)
    memory.write_note(
        conn, "dev", "notes/low.md", "JWT confidence LOWONLYZZZ",
        kind="note", scope="role", tags=["jwt"], confidence=0.1)
    hits = knowledge.search_knowledge(
        conn, "dev", "JWT confidence", scope="role", limit=1)
    assert hits and hits[0]["id"] == high["id"]


def test_results_diversity_caps_dominant_kind(env):
    conn, _, _ = env
    for i in range(4):
        memory.write_note(
            conn, "dev", f"vulns/d{i}.md",
            f"JWT diversity term variant{i} " + " ".join(
                f"alpha{i}{j}" for j in range(12)),
            kind="vulnerability_pattern", scope="role", tags=["jwt"], repo="openssl")
    for i in range(2):
        memory.write_note(
            conn, "dev", f"notes/d{i}.md",
            f"JWT diversity term note{i} " + " ".join(
                f"beta{i}{j}" for j in range(12)),
            kind="note", scope="role", tags=["jwt"], repo="openssl")
        memory.write_note(
            conn, "dev", f"modules/d{i}.md",
            f"JWT diversity term module{i} " + " ".join(
                f"gamma{i}{j}" for j in range(12)),
            kind="module_knowledge", scope="role", tags=["jwt"], repo="openssl")

    hits = knowledge.search_knowledge(
        conn, "dev", "JWT diversity term", task_id="TA", scope="auto", limit=6)
    counts = Counter(h["kind"] for h in hits)
    assert counts["vulnerability_pattern"] <= 2
    assert len(hits) >= 4


def test_include_task_snapshots_alias_and_bm25(env):
    conn, _, _ = env
    snap = _write_task_snapshot(conn, "TA", "ALIASSNAPSHOTUNIQUE")
    hits = knowledge.search_knowledge(
        conn, "dev", "ALIASSNAPSHOTUNIQUE", task_id="TA",
        include_task_snapshots=True)
    hit = next(h for h in hits if h["id"] == snap["id"])
    assert hit["bm25"] is not None
    assert knowledge.search_knowledge(
        conn, "dev", "ALIASSNAPSHOTUNIQUE", task_id="TA") == []
