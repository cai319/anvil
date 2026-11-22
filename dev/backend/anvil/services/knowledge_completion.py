"""知识智能补全请求：目标、角色、run/wake 与进度。

看板只读这里的 effective state；真实执行仍由角色队列/scheduler 完成。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ..util import new_id, now_iso

ACTIVE_JOB_STATUSES = ("queued", "running")
COMPLETION_JOB_TTL_SECONDS = 24 * 3600


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def create_job(conn: sqlite3.Connection, *, target_type: str, target_id: str,
               role_slug: str | None = None, run_id: str | None = None,
               wake_request_id: int | None = None,
               status: str = "queued") -> dict:
    """创建补全任务；同一目标只允许一个未结束 job（并发请求复用）。"""
    owns_txn = not conn.in_transaction
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM knowledge_completion_jobs "
            "WHERE target_type=? AND target_id=? "
            "AND status NOT IN ('completed','failed') "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (target_type, target_id)).fetchone()
        if existing is not None:
            # 请求方可能补到了新 wake/run；只补空字段，不把已完成状态回退。
            if wake_request_id and not existing["wake_request_id"]:
                conn.execute(
                    "UPDATE knowledge_completion_jobs SET wake_request_id=?, updated_at=? "
                    "WHERE id=?", (wake_request_id, now_iso(), existing["id"]))
            if run_id and not existing["run_id"]:
                conn.execute(
                    "UPDATE knowledge_completion_jobs SET run_id=?, updated_at=? "
                    "WHERE id=?", (run_id, now_iso(), existing["id"]))
            if owns_txn:
                conn.commit()
            return latest(conn, target_type, target_id) or {}
        jid = new_id()
        now = now_iso()
        conn.execute(
            "INSERT INTO knowledge_completion_jobs "
            "(id, target_type, target_id, role_slug, run_id, wake_request_id, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (jid, target_type, target_id, role_slug, run_id, wake_request_id,
             status, now, now),
        )
        if owns_txn:
            conn.commit()
    except BaseException:
        if owns_txn:
            conn.rollback()
        raise
    return latest(conn, target_type, target_id) or {}


def latest(conn: sqlite3.Connection, target_type: str, target_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM knowledge_completion_jobs "
        "WHERE target_type=? AND target_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (target_type, target_id)).fetchone())


def link_run(conn: sqlite3.Connection, *, wake_request_id: int,
             run_id: str, status: str = "running") -> None:
    """scheduler 把 wake 投递到具体 run 后，回填 run_id 供看板追踪。"""
    row = conn.execute(
        "SELECT id FROM knowledge_completion_jobs WHERE wake_request_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (wake_request_id,)).fetchone()
    if row is None:
        # 极小概率竞态：scheduler 在请求方写完 job 前就投递了 wake。
        # 用 wake reason 里的目标 id 回填最新 job。
        wr = conn.execute(
            "SELECT reason FROM wake_requests WHERE id=?",
            (wake_request_id,)).fetchone()
        target_type = target_id = None
        if wr is not None:
            reason = str(wr["reason"] or "")
            for prefix, kind in (("lesson.complete.", "lesson"),
                                 ("knowledge.complete.", "role_note")):
                if reason.startswith(prefix):
                    target_type, target_id = kind, reason[len(prefix):]
                    break
        if not target_type or not target_id:
            return
        row = conn.execute(
            "SELECT id FROM knowledge_completion_jobs "
            "WHERE target_type=? AND target_id=? AND run_id IS NULL "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (target_type, target_id)).fetchone()
        if row is None:
            return
    # 已完成/已失败的 job 不能被迟到的 wake/run 回填改回运行中。
    conn.execute(
        "UPDATE knowledge_completion_jobs SET run_id=?, status=?, updated_at=? "
        "WHERE id=? AND status NOT IN ('completed','failed')",
        (run_id, status, now_iso(), row["id"]))


def complete(conn: sqlite3.Connection, target_type: str, target_id: str,
             role_slug: str | None = None) -> None:
    """角色/human 实际提交补全后标记完成；没有请求记录时补一条完成记录。"""
    now = now_iso()
    job = latest(conn, target_type, target_id)
    if job is None:
        conn.execute(
            "INSERT INTO knowledge_completion_jobs "
            "(id, target_type, target_id, role_slug, status, created_at, "
            "updated_at, completed_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), target_type, target_id, role_slug, "completed",
             now, now, now))
        return
    conn.execute(
        "UPDATE knowledge_completion_jobs SET status='completed', "
        "completed_at=?, updated_at=? "
        "WHERE id=? AND status NOT IN ('completed','failed')",
        (now, now, job["id"]))


def _inferred_completed() -> dict:
    """历史知识没有补全任务记录、但已满足入库标准：视为补全完成。

    只用于看板/详情展示，不伪造 job/run；`inferred=True` 便于前端区分。
    """
    return {
        "job_id": None, "state": "completed", "active": False,
        "ready": True, "role_slug": None, "run_id": None,
        "wake_request_id": None, "updated_at": None, "completed_at": None,
        "inferred": True,
    }


def effective_state(conn: sqlite3.Connection, job: dict | None,
                    *, ready: bool | None = None) -> dict | None:
    if job is None:
        return _inferred_completed() if ready else None
    state = str(job.get("status") or "queued")
    if state == "completed":
        return {
            "job_id": job["id"], "state": "completed", "active": False,
            "ready": bool(ready) if ready is not None else None,
            "role_slug": job.get("role_slug"), "run_id": job.get("run_id"),
            "wake_request_id": job.get("wake_request_id"),
            "updated_at": job.get("updated_at"),
            "completed_at": job.get("completed_at"),
            "inferred": False,
        }
    run_status = None
    if job.get("run_id"):
        row = conn.execute(
            "SELECT status FROM runs WHERE id=?", (job["run_id"],)).fetchone()
        run_status = row["status"] if row else None
    wake_status = None
    if job.get("wake_request_id"):
        row = conn.execute(
            "SELECT status FROM wake_requests WHERE id=?",
            (job["wake_request_id"],)).fetchone()
        wake_status = row["status"] if row else None
    if run_status in ("created", "projecting"):
        state = "queued"
    elif run_status in ("running", "suspended", "reviewing", "submitted"):
        state = "running"
    elif run_status == "completed":
        state = "completed"
    elif run_status in ("failed", "abandoned"):
        state = "failed"
    elif wake_status in ("pending", "processing"):
        state = "queued"
    elif wake_status in ("failed", "skipped"):
        state = "failed"
    elif wake_status == "done" and not job.get("run_id"):
        state = "running"
    if ready:
        state = "completed"
    return {
        "job_id": job["id"],
        "state": state,
        "active": state in ("queued", "running"),
        "ready": bool(ready) if ready is not None else None,
        "role_slug": job.get("role_slug"),
        "run_id": job.get("run_id"),
        "wake_request_id": job.get("wake_request_id"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
        "inferred": False,
    }


def status_for(conn: sqlite3.Connection, target_type: str, target_id: str,
               *, ready: bool | None = None) -> dict | None:
    return effective_state(conn, latest(conn, target_type, target_id), ready=ready)


def cancel(conn: sqlite3.Connection, target_type: str, target_id: str) -> int:
    """目标被删除/驳回时撤销未结束的补全任务与唤醒，避免角色做无效工作。"""
    jobs = conn.execute(
        "SELECT id, wake_request_id, status FROM knowledge_completion_jobs "
        "WHERE target_type=? AND target_id=?",
        (target_type, target_id)).fetchall()
    now = now_iso()
    for job in jobs:
        if job["wake_request_id"] and job["status"] not in ("completed", "failed"):
            conn.execute(
                "UPDATE wake_requests SET status='skipped', detail=?, claimed_at=? "
                "WHERE id=? AND status='pending'",
                ("knowledge target deleted", now, job["wake_request_id"]))
        conn.execute("DELETE FROM knowledge_completion_jobs WHERE id=?", (job["id"],))
    if jobs:
        conn.commit()
    return len(jobs)


def _target_ready(conn: sqlite3.Connection, target_type: str,
                  target_id: str) -> tuple[bool, bool | None]:
    """返回 (目标仍存在, 是否已满足必须条款)。"""
    if target_type == "lesson":
        from . import memory
        lesson = memory.get_lesson(conn, target_id)
        if lesson is None:
            return False, None
        return True, not memory.lesson_missing_fields(lesson)
    if target_type == "role_note":
        from ..util import jloads
        from . import knowledge_card
        row = conn.execute(
            "SELECT kind, title, tldr, content, fields_json FROM notes "
            "WHERE id=? AND scope='role'", (target_id,)).fetchone()
        if row is None:
            return False, None
        if (row["kind"] or "note") == "task_snapshot":
            return True, None
        errors = knowledge_card.submission_errors(
            row["kind"] or "note", row["title"] or "", row["tldr"] or "",
            row["content"] or "", jloads(row["fields_json"], {}),
            strict_kind=False)
        return True, not errors
    return True, None


def reconcile(conn: sqlite3.Connection, *, ttl_seconds: int = COMPLETION_JOB_TTL_SECONDS) -> dict:
    """对账补全任务：目标达标→completed，删除/失败/超时→failed。

    由 scheduler 周期调用，保证“部分补全但 job 永远 running / 目标删除后仍 active”
    这类状态不会永久残留。
    """
    now_dt = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM knowledge_completion_jobs "
        "WHERE status NOT IN ('completed','failed')").fetchall()
    completed = failed = 0
    for job in rows:
        exists, ready = _target_ready(
            conn, job["target_type"], job["target_id"])
        if not exists:
            state = "failed"
        elif ready:
            state = "completed"
        else:
            state = None
            if job["run_id"]:
                run = conn.execute(
                    "SELECT status FROM runs WHERE id=?", (job["run_id"],)).fetchone()
                if run is not None and run["status"] in ("failed", "abandoned"):
                    state = "failed"
            if state is None and job["wake_request_id"]:
                wake = conn.execute(
                    "SELECT status FROM wake_requests WHERE id=?",
                    (job["wake_request_id"],)).fetchone()
                if wake is not None and wake["status"] in ("failed", "skipped"):
                    state = "failed"
            if state is None:
                try:
                    created = datetime.fromisoformat(
                        str(job["created_at"]).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if (now_dt - created).total_seconds() > ttl_seconds:
                        state = "failed"
                except (ValueError, TypeError):
                    pass
        if state is None:
            continue
        now = now_iso()
        if state == "completed":
            if job["wake_request_id"]:
                conn.execute(
                    "UPDATE wake_requests SET status='skipped', detail=?, claimed_at=? "
                    "WHERE id=? AND status='pending'",
                    ("knowledge target already complete", now,
                     job["wake_request_id"]))
            conn.execute(
                "UPDATE knowledge_completion_jobs SET status='completed', "
                "completed_at=?, updated_at=? "
                "WHERE id=? AND status NOT IN ('completed','failed')",
                (now, now, job["id"]))
            completed += 1
        else:
            conn.execute(
                "UPDATE knowledge_completion_jobs SET status='failed', updated_at=? "
                "WHERE id=? AND status NOT IN ('completed','failed')",
                (now, job["id"]))
            failed += 1
    if completed or failed:
        conn.commit()
    return {"completed": completed, "failed": failed}
