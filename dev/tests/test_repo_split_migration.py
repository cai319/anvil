"""Phase 1 迁移：旧 tasks.repo（执行仓库）→ task workspace.source_repo + 知识主题。"""
from __future__ import annotations

from anvil import config, db
from anvil.util import now_iso


def test_repo_split_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "data" / "task-workspaces")
    conn = db.connect(str(tmp_path / "t.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO knowledge_repos (id, display_name, status, created_at, updated_at) "
        "VALUES ('openssl', 'openssl', 'active', ?, ?)", (now, now))
    for tid, repo in (("A", "openssl"), ("B", "local-topic"), ("C", None)):
        conn.execute(
            "INSERT INTO tasks (id,title,status,repo,created_at,updated_at) "
            "VALUES (?,?,'todo',?,?,?)", (tid, f"T{tid}", repo, now, now))
    conn.commit()
    conn.execute("DELETE FROM settings WHERE key='schema.repo_split_v1'")
    conn.commit()

    db._migrate_repo_split_v1(conn)

    a = conn.execute(
        "SELECT t.repo, t.knowledge_mode, w.source_repo, w.kind "
        "FROM tasks t JOIN task_workspaces w ON w.task_id=t.id WHERE t.id='A'"
    ).fetchone()
    assert a["repo"] == "openssl" and a["knowledge_mode"] == "bound"
    assert a["source_repo"] == "openssl" and a["kind"] == "git_worktree"

    b = conn.execute(
        "SELECT t.repo, t.knowledge_mode, w.source_repo FROM tasks t "
        "JOIN task_workspaces w ON w.task_id=t.id WHERE t.id='B'").fetchone()
    assert b["repo"] == "local-topic" and b["knowledge_mode"] == "local"
    assert b["source_repo"] == "local-topic"
    b_bind = conn.execute(
        "SELECT scope, path FROM task_knowledge_repos WHERE task_id='B'").fetchone()
    assert b_bind["scope"] == "local" and b_bind["path"].endswith("local-topic")

    c = conn.execute("SELECT repo, knowledge_mode FROM tasks WHERE id='C'").fetchone()
    assert c["repo"] is None and c["knowledge_mode"] == "temp"
    rules = {r["task_id"]: r["rule"] for r in conn.execute(
        "SELECT task_id, rule FROM repo_split_map").fetchall()}
    assert rules == {"A": "topic", "B": "local", "C": "temp"}
    conn.close()
