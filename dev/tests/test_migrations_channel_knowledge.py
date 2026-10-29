"""v1.30 频道知识作用域迁移：旧库 → 新库。"""
from __future__ import annotations

import sqlite3

from anvil import db


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _drop_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    cols = _columns(conn, table)
    if column in cols:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def test_channel_knowledge_migration_and_idempotency(tmp_path):
    path = str(tmp_path / "anvil.db")
    conn = db.connect(path)
    conn.close()

    # 模拟 v1.29 旧库：移除本次新增的表/列，但保留其他既有结构。
    conn = db.open_conn(path)
    for table in (
        "task_knowledge_fts", "task_knowledge",
        "workspace_knowledge_fts", "workspace_knowledge_versions", "workspace_knowledge",
        "workspace_participants",
    ):
        if _table_exists(conn, table):
            conn.execute(f"DROP TABLE {table}")
    for index in ("idx_tasks_status_archived", "idx_notes_role_status"):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table, fields in (
        ("workspaces", ["knowledge_repo"]),
        ("tasks", ["archived_at"]),
        ("notes", [
            "title", "tldr", "fields_json", "practice_task_id", "practice_run_id",
            "outcome", "verified_at", "status", "source_knowledge_json",
        ]),
        ("lessons", ["title", "tldr", "fields_json"]),
        ("memories", [
            "title", "tldr", "fields_json", "practice_task_id", "practice_run_id",
            "outcome", "verified_at", "status", "source_knowledge_json",
        ]),
    ):
        for field in fields:
            _drop_column(conn, table, field)
    conn.execute("DELETE FROM settings WHERE key='schema.channel_knowledge_v1'")

    now = "2026-10-25T00:00:00+00:00"
    conn.execute(
        "INSERT INTO workspaces (id,name,repo,description,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("W1", "main", "repo-a", "desc", now, now),
    )
    conn.execute(
        "INSERT INTO workspace_members (workspace_id,role_slug,access,joined_at) VALUES (?,?,?,?)",
        ("W1", "qa-role", "write", now),
    )
    conn.execute(
        "INSERT INTO tasks (id,title,status,workspace_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("T1", "task", "todo", "W1", now, now),
    )
    conn.execute(
        "INSERT INTO notes (id,role_slug,path,content,created_at) VALUES (?,?,?,?,?)",
        ("N1", "qa-role", "notes/a.md", "第一行标题\n第二行正文", now),
    )
    conn.execute(
        "INSERT INTO lessons (id,content,status,created_at) VALUES (?,?,?,?)",
        ("L1", "共享经验正文", "approved", now),
    )
    conn.execute(
        "INSERT INTO memories (id,role_slug,content,owner,created_at) VALUES (?,?,?,?,?)",
        ("M1", "qa-role", "记忆内容", "self", now),
    )
    conn.commit()
    conn.close()

    # 重新建库触发迁移
    conn = db.connect(path)
    try:
        assert _table_exists(conn, "workspace_participants")
        assert _table_exists(conn, "workspace_knowledge")
        assert _table_exists(conn, "workspace_knowledge_versions")
        assert _table_exists(conn, "task_knowledge")
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_workspace_knowledge_harvest'").fetchone()
        assert idx is not None and "supersedes_id IS NULL" in idx["sql"]
        assert "knowledge_repo" in _columns(conn, "workspaces")
        assert "archived_at" in _columns(conn, "tasks")
        assert {"title", "tldr", "fields_json", "status"} <= _columns(conn, "notes")
        assert {"practice_task_id", "practice_run_id", "outcome", "verified_at"} <= _columns(conn, "notes")
        assert {"title", "tldr", "fields_json"} <= _columns(conn, "lessons")
        assert {"title", "tldr", "fields_json", "status"} <= _columns(conn, "memories")

        row = conn.execute(
            "SELECT role_slug FROM workspace_participants WHERE workspace_id='W1'"
        ).fetchone()
        assert row is not None and row["role_slug"] == "qa-role"

        note = conn.execute("SELECT title,tldr,status FROM notes WHERE id='N1'").fetchone()
        assert note["title"] == "第一行标题"
        assert note["tldr"].startswith("第一行标题")
        assert note["status"] == "active"

        lesson = conn.execute("SELECT title,tldr FROM lessons WHERE id='L1'").fetchone()
        assert lesson["title"] == "共享经验正文"
        assert lesson["tldr"] == "共享经验正文"

        memory = conn.execute("SELECT title,status FROM memories WHERE id='M1'").fetchone()
        assert memory["title"] == "记忆内容"
        assert memory["status"] == "active"

        marker = conn.execute(
            "SELECT value FROM settings WHERE key='schema.channel_knowledge_v1'"
        ).fetchone()
        assert marker is not None and marker["value"] == "1"
    finally:
        conn.close()

    # 二次启动幂等
    conn = db.connect(path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_participants WHERE workspace_id='W1'"
        ).fetchone()["n"]
        assert count == 1
    finally:
        conn.close()


def test_harvest_v2_index_migrates_when_v1_marker_already_set(tmp_path):
    """v1.30.0–v1.30.8 升级：v1 标记已存在时仍必须重建收割唯一索引。"""
    path = str(tmp_path / "upgraded.db")
    conn = db.connect(path)
    conn.close()

    # 模拟 v1.30.8：旧唯一索引没有 supersedes_id 谓词，v1 标记已写入。
    conn = db.open_conn(path)
    conn.execute("DROP INDEX IF EXISTS idx_workspace_knowledge_harvest")
    conn.execute(
        "CREATE UNIQUE INDEX idx_workspace_knowledge_harvest "
        "ON workspace_knowledge(workspace_id, source_task_id, kind, content_hash) "
        "WHERE source_task_id IS NOT NULL AND content_hash IS NOT NULL"
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) "
        "VALUES ('schema.channel_knowledge_v1', '1', '2026-10-25T00:00:00+00:00')")
    conn.execute(
        "DELETE FROM settings WHERE key='schema.workspace_knowledge_harvest_v2'")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_workspace_knowledge_harvest'").fetchone()
        assert idx is not None and "supersedes_id IS NULL" in idx["sql"]
        marker = conn.execute(
            "SELECT value FROM settings "
            "WHERE key='schema.workspace_knowledge_harvest_v2'").fetchone()
        assert marker is not None and marker["value"] == "1"
    finally:
        conn.close()


def test_new_database_has_channel_knowledge_schema(tmp_path):
    path = str(tmp_path / "new.db")
    conn = db.connect(path)
    try:
        for table in (
            "workspace_participants", "workspace_knowledge", "workspace_knowledge_versions",
            "task_knowledge",
        ):
            assert _table_exists(conn, table)
        assert "knowledge_repo" in _columns(conn, "workspaces")
        assert "archived_at" in _columns(conn, "tasks")
        assert "status" in _columns(conn, "notes")
    finally:
        conn.close()
