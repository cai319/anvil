"""任务共享知识：任务参与角色共享；子任务知识回流父任务并打标记。"""
from __future__ import annotations

import sqlite3

from ..util import jdumps, jloads, new_id, now_iso
from . import knowledge_card
from .tasks import ConflictError, get as get_task


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["fields"] = jloads(d.pop("fields_json", "{}"), {})
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    return d


def get(conn: sqlite3.Connection, knowledge_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM task_knowledge WHERE id=?", (knowledge_id,)
    ).fetchone())


def list_for_task(conn: sqlite3.Connection, task_id: str, *,
                  include_subtasks: bool = False, deep: bool = False) -> list[dict]:
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    ids = [task_id]
    if include_subtasks or deep:
        if deep:
            frontier = [task_id]
            seen = {task_id}
            while frontier:
                nxt = []
                for parent in frontier:
                    for row in conn.execute(
                        "SELECT id FROM tasks WHERE parent_id=?", (parent,)
                    ).fetchall():
                        if row["id"] not in seen:
                            seen.add(row["id"])
                            ids.append(row["id"])
                            nxt.append(row["id"])
                frontier = nxt
        else:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE parent_id=?", (task_id,)
            ).fetchall()
            ids.extend(r["id"] for r in rows)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM task_knowledge WHERE task_id IN ({placeholders}) "
        "AND status='active' ORDER BY created_at DESC",
        ids,
    ).fetchall()
    return [_row(r) for r in rows]  # type: ignore[list-item]


def _like_pattern(text: str) -> str:
    escaped = (str(text).replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


def list_all(conn: sqlite3.Connection, *, status: str | None = None,
             workspace_id: str | None = None, q: str | None = None,
             limit: int = 200, offset: int = 0) -> dict:
    """跨任务知识列表（人工审计视图）。"""
    limit = max(1, min(500, int(limit or 200)))
    offset = max(0, int(offset or 0))
    clauses: list[str] = []
    params: list = []
    if status and status != "all":
        clauses.append("tk.status=?")
        params.append(status)
    if workspace_id:
        clauses.append("t.workspace_id=?")
        params.append(workspace_id)
    if q and str(q).strip():
        pattern = _like_pattern(str(q).strip())
        clauses.append(
            "(tk.title LIKE ? ESCAPE '\\' OR tk.tldr LIKE ? ESCAPE '\\' "
            "OR tk.body_md LIKE ? ESCAPE '\\' OR tk.tags_json LIKE ? ESCAPE '\\' "
            "OR COALESCE(t.title, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 5)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM task_knowledge tk "
        "LEFT JOIN tasks t ON t.id=tk.task_id "
        f"{where}", params,
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT tk.*, t.title AS task_title, t.status AS task_status, "
        "t.workspace_id AS workspace_id, t.archived_at AS task_archived_at, "
        "w.name AS workspace_name "
        "FROM task_knowledge tk "
        "LEFT JOIN tasks t ON t.id=tk.task_id "
        "LEFT JOIN workspaces w ON w.id=t.workspace_id "
        f"{where} ORDER BY tk.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = [_row(r) for r in rows]  # type: ignore[list-item]
    return {"items": items, "total": total, "limit": limit,
            "offset": offset, "has_more": offset + len(items) < total}


def create(conn: sqlite3.Connection, task_id: str, *,
           title: str, tldr: str, kind: str,
           fields: dict | None = None, tags: list[str] | None = None,
           source_role: str | None = None) -> dict:
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    if kind not in knowledge_card.KINDS:
        raise ConflictError(f"unknown knowledge kind: {kind}")
    fields = knowledge_card.normalize_fields(fields)
    body_md = knowledge_card.render_markdown(title, tldr, kind, fields)
    check = knowledge_card.validate_card(kind, fields, for_approval=False)
    parent_task_id = task.get("parent_id")
    kid = new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO task_knowledge "
        "(id, task_id, parent_task_id, source_subtask_id, origin, kind, title, tldr, "
        "body_md, fields_json, tags_json, source_role, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            kid, task_id, parent_task_id,
            task_id if parent_task_id else None,
            "subtask" if parent_task_id else "task",
            kind, title, tldr, body_md, jdumps(fields), jdumps(tags or []),
            source_role, "active", now, now,
        ),
    )
    record = get(conn, kid)
    assert record is not None
    record["completeness"] = check["completeness"]
    conn.commit()
    return record


def render_for_task(conn: sqlite3.Connection, task_id: str, *,
                    budget_chars: int = 800) -> str:
    """渲染任务共享知识（含直接子任务来源）。"""
    from .. import config
    if not config.TASK_KNOWLEDGE_ENABLED:
        return ""
    cards = list_for_task(conn, task_id, include_subtasks=True)
    lines: list[str] = []
    used = 0
    for card in cards:
        origin = (f"来自子任务 {card['source_subtask_id']}"
                  if card.get("source_subtask_id") else "任务共享")
        line = f"- [{origin}] {card['title']}：{card['tldr']}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "## 任务共享知识\n" + "\n".join(lines)


def archive_for_task(conn: sqlite3.Connection, task_id: str) -> None:
    """任务删除时保留知识行，仅标记 archived。"""
    conn.execute(
        "UPDATE task_knowledge SET status='archived', updated_at=? "
        "WHERE task_id=? OR parent_task_id=?",
        (now_iso(), task_id, task_id),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, knowledge_id: str) -> bool:
    cur = conn.execute("DELETE FROM task_knowledge WHERE id=?", (knowledge_id,))
    conn.commit()
    return cur.rowcount > 0
