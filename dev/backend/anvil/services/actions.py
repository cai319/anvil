"""Action Cards：Agent 准备操作、自然人批准后以人的身份执行。

白名单动作：
- task.cancel：人工终止任务（closed），仅接受非终态任务
- task.reopen：重新打开已终止任务（closed → todo）
危险/管理操作先 prepare，避免 Agent 直接改世界。
"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, jloads, new_id, now_iso

KIND_LABEL = {
    "task.cancel": "人工终止任务",
    "task.reopen": "重新打开任务",
}
ALLOWED = set(KIND_LABEL)


def create(conn: sqlite3.Connection, role: str, kind: str, task_id: str | None,
           payload: dict | None = None, reason: str | None = None) -> dict:
    if kind not in ALLOWED:
        raise ConflictError(f"action kind {kind} is not allowed")
    if not task_id:
        raise ConflictError("task_id is required")
    task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise LookupError(task_id)
    status = task["status"]
    if kind == "task.cancel" and status in ("done", "closed"):
        raise ConflictError(f"task {task_id} is {status}; cannot be manually terminated")
    if kind == "task.reopen" and status != "closed":
        raise ConflictError(f"task {task_id} is {status}; only a terminated task can be reopened")
    cid = new_id()
    conn.execute(
        "INSERT INTO action_cards (id, role_slug, task_id, kind, payload_json, reason, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (cid, role, task_id, kind, jdumps(payload or {}), reason, "pending", now_iso()),
    )
    conn.commit()
    return get(conn, cid)  # type: ignore[return-value]


def get(conn: sqlite3.Connection, cid: str) -> dict | None:
    r = conn.execute("SELECT * FROM action_cards WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = jloads(d.pop("payload_json"), {})
    d["result"] = jloads(d.get("result_json"), {})
    d["label"] = KIND_LABEL.get(d["kind"], d["kind"])
    return d


def list_cards(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    q = "SELECT id FROM action_cards"
    params: list = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY created_at DESC"
    return [get(conn, r["id"]) for r in conn.execute(q, params).fetchall()]  # type: ignore[misc]


def pending_for_inbox(conn: sqlite3.Connection) -> list[dict]:
    return list_cards(conn, "pending")


def decide(conn: sqlite3.Connection, cid: str, approve: bool, by: str = "human") -> dict:
    card = get(conn, cid)
    if card is None:
        raise LookupError(cid)
    if card["status"] != "pending":
        raise ConflictError(f"action {cid} is {card['status']}, expected pending")
    now = now_iso()
    if not approve:
        conn.execute("UPDATE action_cards SET status='rejected', decided_at=? WHERE id=?",
                     (now, cid))
        conn.commit()
        return get(conn, cid)  # type: ignore[return-value]
    from . import tasks as tasks_service

    try:
        if card["kind"] == "task.cancel":
            result = tasks_service.cancel(conn, card["task_id"], "human")
        elif card["kind"] == "task.reopen":
            result = tasks_service.reopen(conn, card["task_id"], "human")
        else:  # pragma: no cover - guarded by create
            raise ConflictError(f"unsupported action {card['kind']}")
        conn.execute(
            "UPDATE action_cards SET status='executed', result_json=?, decided_at=? WHERE id=?",
            (jdumps({"task_status": result.get("status")}), now, cid))
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "UPDATE action_cards SET status='failed', result_json=?, decided_at=? WHERE id=?",
            (jdumps({"error": str(exc)}), now, cid))
        conn.commit()
        raise
    conn.commit()
    return get(conn, cid)  # type: ignore[return-value]
