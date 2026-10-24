"""流程语义事件账本（append-only）。

状态表（workflows/workflow_steps/runs/tasks）仍是权威状态；events 只记录
流程语义变化，供时间线、指标、审计和执行档案消费。activities/audit 继续
保留为会话与治理投影，账本通过 source/source_id 支持幂等回填。
"""
from __future__ import annotations

import sqlite3

from ..util import jdumps, jloads, now_iso


def emit(conn: sqlite3.Connection, event_type: str, *,
         task_id: str | None = None,
         workflow_id: str | None = None,
         execution_no: int | None = None,
         step_id: str | None = None,
         slice_key: str | None = None,
         run_id: str | None = None,
         actor_type: str = "system",
         actor: str | None = None,
         payload: dict | None = None,
         causation_id: int | None = None,
         correlation_id: str | None = None,
         source: str | None = None,
         source_id: int | None = None,
         mirror_activity: bool = False,
         activity_kind: str | None = None,
         activity_role: str | None = None,
         mirror_audit: bool = False,
         audit_action: str | None = None) -> int:
    """写入一条事件；可选同步写 activities 旧投影。"""
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO events (ts, event_type, task_id, workflow_id, execution_no, "
        "step_id, slice_key, run_id, actor_type, actor, causation_id, correlation_id, "
        "source, source_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, event_type, task_id, workflow_id, execution_no, step_id, slice_key,
         run_id, actor_type, actor, causation_id, correlation_id, source, source_id,
         jdumps(payload or {})),
    )
    event_id = int(cur.lastrowid)
    if mirror_activity:
        conn.execute(
            "INSERT INTO activities (task_id, role_slug, run_id, kind, payload_json, "
            "created_at, event_id, workflow_id, step_id, slice_key, execution_no) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, activity_role or (actor if actor_type == "role" else "workflow"),
             run_id, activity_kind or event_type.split(".", 1)[0], jdumps(payload or {}),
             now, event_id, workflow_id, step_id, slice_key, execution_no),
        )
    if mirror_audit:
        cur_audit = conn.execute(
            "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (actor, task_id, audit_action or event_type, jdumps(payload or {}), now),
        )
        conn.execute(
            "UPDATE events SET source='audit', source_id=? WHERE id=?",
            (int(cur_audit.lastrowid), event_id),
        )
    return event_id


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["payload"] = jloads(d.pop("payload_json", "{}"), {})
    return d


def list_task_events(conn: sqlite3.Connection, task_id: str, *,
                     after_id: int = 0, limit: int = 100,
                     kinds: list[str] | None = None,
                     workflow_id: str | None = None) -> dict:
    limit = max(1, min(int(limit), 500))
    clauses = ["task_id=?", "id>?"]
    params: list = [task_id, int(after_id or 0)]
    if workflow_id:
        clauses.append("workflow_id=?")
        params.append(workflow_id)
    if kinds:
        clauses.append("(" + " OR ".join("event_type LIKE ?" for _ in kinds) + ")")
        params.extend(f"{k}%" for k in kinds)
    q = ("SELECT * FROM events WHERE " + " AND ".join(clauses) +
         " ORDER BY id LIMIT ?")
    rows = conn.execute(q, (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_row(r) for r in rows],
        "next_after_id": rows[-1]["id"] if rows else int(after_id or 0),
        "has_more": has_more,
    }


def list_events(conn: sqlite3.Connection, *, task_id: str | None = None,
                workflow_id: str | None = None, event_type: str | None = None,
                after_id: int = 0, limit: int = 100) -> dict:
    limit = max(1, min(int(limit), 500))
    clauses = ["id>?"]
    params: list = [int(after_id or 0)]
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if workflow_id:
        clauses.append("workflow_id=?")
        params.append(workflow_id)
    if event_type:
        clauses.append("event_type=?")
        params.append(event_type)
    rows = conn.execute(
        "SELECT * FROM events WHERE " + " AND ".join(clauses) +
        " ORDER BY id LIMIT ?", (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_row(r) for r in rows],
        "next_after_id": rows[-1]["id"] if rows else int(after_id or 0),
        "has_more": has_more,
    }


def _activity_event_type(kind: str, payload: dict) -> str:
    if kind in ("step", "run"):
        status = str(payload.get("status") or "").strip()
        return f"{kind}.{status}" if status else kind
    if kind in ("comment", "mention"):
        event = str(payload.get("event") or "").strip()
        return f"message.{event or kind}"
    return kind


def backfill(conn: sqlite3.Connection) -> int:
    """把历史 activities/audit 幂等回填到 events。返回新增数量。"""
    added = 0
    for r in conn.execute(
        "SELECT id, task_id, role_slug, run_id, kind, payload_json, created_at "
        "FROM activities WHERE event_id IS NULL ORDER BY id"
    ).fetchall():
        payload = jloads(r["payload_json"], {})
        event_type = _activity_event_type(str(r["kind"]), payload)
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (ts, event_type, task_id, run_id, actor_type, "
            "actor, source, source_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["created_at"], event_type, r["task_id"], r["run_id"],
             "role" if r["role_slug"] and r["role_slug"] not in ("workflow", "human", "system") else "system",
             r["role_slug"], "activity", r["id"], jdumps(payload)),
        )
        if cur.rowcount:
            added += 1
    for r in conn.execute(
        "SELECT id, task_id, role_slug, action, payload_json, created_at "
        "FROM audit ORDER BY id"
    ).fetchall():
        payload = jloads(r["payload_json"], {})
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (ts, event_type, task_id, actor_type, actor, "
            "source, source_id, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (r["created_at"], f"audit.{r['action']}", r["task_id"],
             "human" if r["role_slug"] == "human" else "role", r["role_slug"],
             "audit", r["id"], jdumps(payload)),
        )
        if cur.rowcount:
            added += 1
    conn.commit()
    return added


def backfill_once(conn: sqlite3.Connection) -> int:
    try:
        marker = conn.execute(
            "SELECT value FROM settings WHERE key='schema.events_backfill_v1'"
        ).fetchone()
        if marker:
            return 0
        added = backfill(conn)
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("schema.events_backfill_v1", "1", now_iso()),
        )
        conn.commit()
        return added
    except sqlite3.OperationalError:
        return 0
