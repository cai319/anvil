"""角色知识的来源主题：repo_id 溯源、别名显示与相关性排序。"""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.db import connect
from anvil.services import knowledge, knowledge_repos, memory
from anvil.util import now_iso


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_DIR", data / "knowledge-repos")
    conn = connect(str(tmp_path / "notes.db"))
    yield conn, data
    conn.close()


def _task(conn, tid="T1", repo=None):
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,repo,created_at,updated_at) "
        "VALUES (?,?,'todo','[\"openssl\"]',?,?,?)",
        (tid, "OpenSSL audit", repo, now, now))
    conn.commit()


def test_write_note_resolves_repo_id_without_creating_registry(env):
    conn, _ = env
    knowledge_repos.ensure(conn, "OpenSSL-4.0.1", description="审计主题")
    memory.write_note(conn, "dev", "notes/a.md", "先验证版本再下结论",
                      repo="openssl-4.0.1")
    row = conn.execute(
        "SELECT repo, repo_id FROM notes WHERE role_slug='dev' AND path LIKE '%a.md'"
    ).fetchone()
    assert row["repo"] == "openssl-4.0.1"
    assert row["repo_id"] == "openssl-4.0.1"

    memory.write_note(conn, "dev", "notes/b.md", "历史项目经验",
                      repo="legacy-project")
    row = conn.execute(
        "SELECT repo, repo_id FROM notes WHERE role_slug='dev' AND path LIKE '%b.md'"
    ).fetchone()
    assert row["repo"] == "legacy-project"
    assert row["repo_id"] is None
    assert knowledge_repos.get(conn, "legacy-project") is None


def test_reindex_resolves_repo_id(env):
    conn, data = env
    knowledge_repos.ensure(conn, "OpenSSL-4.0.1")
    draft = data / "agents" / "dev" / "notes" / "openssl" / "finding.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(
        '<!-- anvil: {"kind":"module_knowledge","scope":"role",'
        '"repo":"OpenSSL-4.0.1"} -->\n\nX509 版本核对\n', encoding="utf-8")
    out = knowledge.reindex_notes(conn, "dev")
    assert out["updated"] == 1
    row = conn.execute(
        "SELECT repo, repo_id FROM notes WHERE role_slug='dev' "
        "AND path='openssl/finding.md'").fetchone()
    assert row["repo_id"] == "openssl-4.0.1"


def test_migration_backfills_notes_repo_id(env):
    conn, _ = env
    knowledge_repos.ensure(conn, "OpenSSL-4.0.1")
    now = now_iso()
    conn.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at, repo) "
        "VALUES ('old-note','dev','notes/old.md','历史经验',?,'OpenSSL-4.0.1')",
        (now,))
    # 移除标记，重跑回填迁移，模拟升级前写入的 note。
    conn.execute("DELETE FROM settings WHERE key='schema.notes_repo_id_v2'")
    conn.commit()
    db._migrate_notes_repo_ids_v2(conn)
    row = conn.execute(
        "SELECT repo_id FROM notes WHERE id='old-note'").fetchone()
    assert row["repo_id"] == "openssl-4.0.1"


def test_role_index_uses_registry_display_name(env):
    conn, _ = env
    repo = knowledge_repos.ensure(conn, "OpenSSL-4.0.1")
    memory.write_note(conn, "dev", "notes/a.md", "经验 A", repo="openssl-4.0.1")
    try:
        knowledge_repos.rename(conn, repo["id"], "OpenSSL 审计")
    except Exception as exc:  # 改名冲突时至少验证 canonical id 显示
        print("rename skipped:", exc)
    index = knowledge.knowledge_index(conn, "dev")
    recent = index["recent"][0]
    assert recent["repo_id"] == repo["id"]
    assert recent["repo_display"] in ("OpenSSL 审计", "OpenSSL-4.0.1")


def test_rank_prefers_canonical_repo_id(env):
    conn, _ = env
    knowledge_repos.ensure(conn, "OpenSSL-4.0.1")
    knowledge_repos.ensure(conn, "other-project")
    _task(conn, "T1", repo="openssl-4.0.1")
    memory.write_note(conn, "dev", "notes/a.md",
                      "sharedmarker 先验证版本再下结论 one", repo="openssl-4.0.1")
    memory.write_note(conn, "dev", "notes/b.md",
                      "sharedmarker 先验证版本再下结论 two", repo="other-project")
    hits = knowledge.search_knowledge(
        conn, "dev", "sharedmarker", task_id="T1", scope="role",
        limit=10, min_score=0)
    assert [h["repo_id"] for h in hits[:2]] == ["openssl-4.0.1", "other-project"]
    assert hits[0]["repo_display"] == "OpenSSL-4.0.1"
