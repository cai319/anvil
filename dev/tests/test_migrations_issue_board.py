"""问题板（Issue Board）迁移：tabs 开关、reviewers/board/findings 表。"""
from __future__ import annotations

import sqlite3

from anvil import db


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_issue_board_schema_and_upgrade(tmp_path):
    path = str(tmp_path / "anvil.db")
    conn = db.connect(path)
    conn.close()

    # 模拟 v1.30.9：移除本次新增的列/表/标记。
    conn = db.open_conn(path)
    for table in ("task_findings", "task_finding_boards", "task_reviewers"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    try:
        conn.execute("ALTER TABLE tasks DROP COLUMN issue_board_enabled")
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM settings WHERE key='schema.issue_board_v1'")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        assert "issue_board_enabled" in _columns(conn, "tasks")
        assert _table_exists(conn, "task_reviewers")
        assert _table_exists(conn, "task_finding_boards")
        assert _table_exists(conn, "task_findings")
        assert {"status", "severity", "confidence", "poc_status", "decision_note"} <= _columns(
            conn, "task_findings")
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_task_findings_task_status'").fetchone()
        assert idx is not None
        marker = conn.execute(
            "SELECT value FROM settings WHERE key='schema.issue_board_v1'").fetchone()
        assert marker is not None and marker["value"] == "1"
    finally:
        conn.close()

    # 二次启动幂等
    conn = db.connect(path)
    try:
        assert _table_exists(conn, "task_findings")
    finally:
        conn.close()
