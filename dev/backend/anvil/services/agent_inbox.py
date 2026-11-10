"""Agent Inbox：拉模式注意力管理（参照 raft）。

- 消息不直接 push 进 agent 上下文，而是可查询的 inbox 项；
- `check` 只读，不推进 cursor；`commit` 推进 cursor（两阶段/at-least-once）。
"""
from __future__ import annotations

import sqlite3

from ..util import jloads, now_iso


def _slice_value(slice_key: str | None) -> str:
    """'' = task-wide；其它 = 切片 key。"""
    return str(slice_key or "")


def _activity_scope(slice_key: str | None) -> tuple[str, list]:
    if slice_key:
        return ("AND (slice_key=? OR slice_key IS NULL)", [slice_key])
    return ("AND slice_key IS NULL", [])


def _cursor(conn: sqlite3.Connection, role: str, task_id: str,
            slice_key: str | None = None) -> int:
    row = conn.execute(
        "SELECT last_activity_id FROM role_inbox_cursors "
        "WHERE role_slug=? AND task_id=? AND slice_key=?",
        (role, task_id, _slice_value(slice_key)),
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


def items(conn: sqlite3.Connection, role: str, task_id: str, limit: int = 30,
          slice_key: str | None = None) -> list[dict]:
    """cursor 之后、非自己发出的评论/@/步骤事件（按当前切片过滤）。"""
    cur = _cursor(conn, role, task_id, slice_key)
    scope, scope_params = _activity_scope(slice_key)
    rows = conn.execute(
        """SELECT id, role_slug, kind, payload_json, created_at
             FROM activities
            WHERE task_id=? AND id>? AND (role_slug IS NULL OR role_slug<>?)
              """ + scope + """
              AND (
                    kind='comment'
                 OR kind='step'
                 OR (kind='mention' AND json_extract(payload_json, '$.target')=?)
              )
            ORDER BY id LIMIT ?""",
        (task_id, cur, role, *scope_params, role, limit),
    ).fetchall()
    return [{
        "id": r["id"], "role": r["role_slug"] or "system", "kind": r["kind"],
        "text": _text(r), "created_at": r["created_at"],
    } for r in rows]


def unread_count(conn: sqlite3.Connection, role: str, task_id: str,
                 slice_key: str | None = None) -> int:
    cur = _cursor(conn, role, task_id, slice_key)
    scope, scope_params = _activity_scope(slice_key)
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM activities
            WHERE task_id=? AND id>? AND (role_slug IS NULL OR role_slug<>?)
              """ + scope + """
              AND (
                    kind='comment'
                 OR kind='step'
                 OR (kind='mention' AND json_extract(payload_json, '$.target')=?)
              )""",
        (task_id, cur, role, *scope_params, role),
    ).fetchone()
    return int(row["n"])


def commit(conn: sqlite3.Connection, role: str, task_id: str, up_to: int,
           slice_key: str | None = None) -> int:
    """提交 cursor（只前进不回退）；切片与 task-wide 各自独立。"""
    cur = max(_cursor(conn, role, task_id, slice_key), int(up_to))
    conn.execute(
        "INSERT INTO role_inbox_cursors "
        "(role_slug, task_id, slice_key, last_activity_id, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(role_slug, task_id, slice_key) DO UPDATE SET "
        "last_activity_id=excluded.last_activity_id, updated_at=excluded.updated_at",
        (role, task_id, _slice_value(slice_key), cur, now_iso()),
    )
    conn.commit()
    return cur
