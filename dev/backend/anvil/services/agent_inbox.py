"""Agent Inbox：拉模式注意力管理（参照 raft）。

- 消息不直接 push 进 agent 上下文，而是可查询的 inbox 项；
- `check` 只读，不推进 cursor；`commit` 推进 cursor（两阶段/at-least-once）。
"""
from __future__ import annotations

import sqlite3

from ..util import jloads, now_iso


def _cursor(conn: sqlite3.Connection, role: str, task_id: str) -> int:
    row = conn.execute(
        "SELECT last_activity_id FROM role_inbox_cursors WHERE role_slug=? AND task_id=?",
        (role, task_id),
    ).fetchone()
    return int(row["last_activity_id"]) if row else 0


def _text(row) -> str:
    p = jloads(row["payload_json"], {})
    kind = row["kind"]
    if kind == "comment":
        return str(p.get("body", ""))
    if kind == "mention":
        return f"@{p.get('target', '')}: {p.get('message', '')}"
    if kind == "step":
        return f"步骤 {p.get('step', '')} → {p.get('status', '')}：{p.get('summary', '')}"
    return kind


def items(conn: sqlite3.Connection, role: str, task_id: str, limit: int = 30) -> list[dict]:
    """cursor 之后、非自己发出的评论/@/步骤事件（按时间正序）。"""
    cur = _cursor(conn, role, task_id)
    rows = conn.execute(
        """SELECT id, role_slug, kind, payload_json, created_at
             FROM activities
            WHERE task_id=? AND id>? AND (role_slug IS NULL OR role_slug<>?)
              AND (
                    kind='comment'
                 OR kind='step'
                 OR (kind='mention' AND json_extract(payload_json, '$.target')=?)
              )
            ORDER BY id LIMIT ?""",
        (task_id, cur, role, role, limit),
    ).fetchall()
    return [{
        "id": r["id"], "role": r["role_slug"] or "system", "kind": r["kind"],
        "text": _text(r), "created_at": r["created_at"],
    } for r in rows]


def unread_count(conn: sqlite3.Connection, role: str, task_id: str) -> int:
    cur = _cursor(conn, role, task_id)
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM activities
            WHERE task_id=? AND id>? AND (role_slug IS NULL OR role_slug<>?)
              AND (
                    kind='comment'
                 OR kind='step'
                 OR (kind='mention' AND json_extract(payload_json, '$.target')=?)
              )""",
        (task_id, cur, role, role),
    ).fetchone()
    return int(row["n"])


def commit(conn: sqlite3.Connection, role: str, task_id: str, up_to: int) -> int:
    """提交 cursor（只前进不回退）。返回新的 cursor。"""
    cur = max(_cursor(conn, role, task_id), int(up_to))
    conn.execute(
        "INSERT INTO role_inbox_cursors (role_slug, task_id, last_activity_id, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(role_slug, task_id) DO UPDATE SET "
        "last_activity_id=excluded.last_activity_id, updated_at=excluded.updated_at",
        (role, task_id, cur, now_iso()),
    )
    conn.commit()
    return cur
