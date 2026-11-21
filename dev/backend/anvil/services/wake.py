"""跨进程唤醒请求：服务层登记，scheduler 统一解析目标 run/step 后投递。

为什么单独一条队列：
- delegation 的 submit/accept/reject/cancel 发生在服务层（API/CLI/工作流内部），
  这里拿不到 orchestrator，也不能直接 spawn；
- 父 step 可能处于 suspended、尚无 run、或已被重置为 pending 三种形态，
  目标解析必须放在掌握 starter/泵能力的 scheduler 里做；
- 唤醒必须可持久化：服务重启后仍要补投，不能只靠一次内存调用。
"""
from __future__ import annotations

import sqlite3

from ..util import now_iso


def enqueue(conn: sqlite3.Connection, *, task_id: str, role_slug: str,
            reason: str, note: str, workflow_id: str | None = None,
            step_id: str | None = None, slice_key: str | None = None) -> int | None:
    """登记一条唤醒请求；同一 (task, role, workflow, step, reason) 未处理时去重。"""
    if not task_id or not role_slug or not reason or not str(note or "").strip():
        return None
    note = str(note).strip()
    existing = conn.execute(
        "SELECT id, status FROM wake_requests WHERE status='pending' AND task_id=? "
        "AND role_slug=? AND COALESCE(workflow_id,'')=? AND COALESCE(step_id,'')=? "
        "AND reason=? ORDER BY id DESC LIMIT 1",
        (task_id, role_slug, workflow_id or "", step_id or "", reason)).fetchone()
    if existing is not None:
        # 同一 pending 请求：用最新 note 覆盖（例如两次 reject 的不同 feedback）。
        conn.execute(
            "UPDATE wake_requests SET note=? WHERE id=?", (note, existing["id"]))
        return int(existing["id"])
    try:
        cur = conn.execute(
            "INSERT INTO wake_requests (task_id, role_slug, workflow_id, step_id, "
            "slice_key, reason, note, status, attempts, created_at) "
            "VALUES (?,?,?,?,?,?,?,'pending',0,?)",
            (task_id, role_slug, workflow_id, step_id, slice_key, reason,
             note, now_iso()))
    except sqlite3.IntegrityError:
        # 并发 enqueue：复用刚插入的 pending 行并更新 note。
        row = conn.execute(
            "SELECT id FROM wake_requests WHERE status='pending' AND task_id=? "
            "AND role_slug=? AND COALESCE(workflow_id,'')=? "
            "AND COALESCE(step_id,'')=? AND reason=? ORDER BY id DESC LIMIT 1",
            (task_id, role_slug, workflow_id or "", step_id or "", reason)).fetchone()
        if row is None:
            raise
        conn.execute("UPDATE wake_requests SET note=? WHERE id=?",
                     (note, row["id"]))
        return int(row["id"])
    return int(cur.lastrowid)


def pending(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM wake_requests WHERE status='pending' ORDER BY id LIMIT ?",
        (max(1, int(limit)),)).fetchall()]


def mark(conn: sqlite3.Connection, req_id: int, status: str,
         detail: str | None = None) -> None:
    """processing/done/skipped/failed；detail 保留最后一次解析结果，便于排查。"""
    conn.execute(
        "UPDATE wake_requests SET status=?, claimed_at=?, detail=? WHERE id=?",
        (status, now_iso(), (detail or "")[:500] or None, req_id))


def fail(conn: sqlite3.Connection, req_id: int, detail: str) -> None:
    mark(conn, req_id, "failed", detail)


def requeue(conn: sqlite3.Connection, req_id: int, detail: str | None = None) -> None:
    """投递未成功（目标仍在 running / pump 失败）：保留 pending 并累计尝试次数。"""
    try:
        conn.execute(
            "UPDATE wake_requests SET status='pending', claimed_at=NULL, "
            "attempts=COALESCE(attempts,0)+1, detail=COALESCE(?, detail) WHERE id=?",
            ((detail or "")[:500] or None, req_id))
    except sqlite3.IntegrityError:
        # 同目标已有更新的 pending 请求：本请求被 supersede，避免唯一索引冲突。
        conn.execute(
            "UPDATE wake_requests SET status='skipped', claimed_at=?, "
            "detail='superseded by newer pending request' WHERE id=?",
            (now_iso(), req_id))


def reset_stale(conn: sqlite3.Connection, older_than_iso: str) -> int:
    """调度器崩溃在 processing 中的请求重新排队，保证 at-least-once。"""
    cur = conn.execute(
        "UPDATE wake_requests SET status='pending', claimed_at=NULL, "
        "attempts=COALESCE(attempts,0)+1 "
        "WHERE status='processing' AND COALESCE(claimed_at, created_at) < ?",
        (older_than_iso,))
    return int(cur.rowcount or 0)


def prune(conn: sqlite3.Connection, older_than_iso: str) -> int:
    """清理已处理的历史请求，避免队列表无限增长。"""
    cur = conn.execute(
        "DELETE FROM wake_requests WHERE status IN ('done','skipped','failed') "
        "AND COALESCE(claimed_at, created_at) < ?", (older_than_iso,))
    return int(cur.rowcount or 0)
