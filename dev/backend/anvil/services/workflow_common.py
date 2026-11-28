"""Workflow 共享内核：状态查询、租约锁、活动/提及与委派读写。"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

from .. import config
from ..util import ConflictError, jloads, now_iso
from ..workflow_payload import normalize_weight, normalize_workflow_payload
from . import leases, ledger

# 保持原有 logger 名称，拆分不改变日志通道。
logger = logging.getLogger("anvil.services.workflow")


RUN_ACTIVE = ("created", "projecting", "running")


STEP_DONE = ("approved", "skipped")


def _assert_current_run(step: dict, run_id: str | None) -> None:
    """旧 run 在步骤重派后不能继续提交/决策；run_id 为空时保留测试/旧客户端兼容。"""
    current = str(step.get("run_id") or "")
    if run_id and current and str(run_id) != current:
        raise ConflictError(
            f"stale_run: step {step.get('step_id')} is bound to run {current}, "
            f"not {run_id}")
    if run_id and not current:
        raise ConflictError(f"stale_run: step {step.get('step_id')} has no active run")


def open_delegations(conn: sqlite3.Connection, wf: dict, step_id: str,
                     limit: int | None = None) -> list[dict]:
    """当前 step 仍未解除的委派工单（done/closed 视为已解除）。

    只检查当前 workflow/execution 的委派；旧 execution 的残留委派不阻塞新执行。
    """
    clauses = [
        "parent_id=?", "parent_step_id=?",
        "COALESCE(type,'') NOT IN ('dispatch','dispatched')",
        "status NOT IN ('done','closed')",
    ]
    params: list = [wf["task_id"], step_id]
    if wf.get("id"):
        clauses.append("(parent_workflow_id IS NULL OR parent_workflow_id=?)")
        params.append(wf["id"])
    if wf.get("execution_no") is not None:
        clauses.append("(parent_execution_no IS NULL OR parent_execution_no=?)")
        params.append(int(wf["execution_no"]))
    sql = ("SELECT id, title, status, assignee, created_at FROM tasks WHERE "
           + " AND ".join(clauses) + " ORDER BY created_at, id")
    if limit is not None:
        sql += f" LIMIT {max(1, int(limit))}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def child_task_wait_state(conn: sqlite3.Connection, wf: dict, step_id: str,
                          run_id: str | None = None) -> dict:
    """父 step 的显式 child-task 等待状态，供看门狗/续跑提示共用。

    state:
    - none：没有可识别的等待；
    - delegation：委派负责人在 todo/in_progress，父 step 应休眠等待；
    - delegation_review：委派已提交 in_review，等父 step 角色验收（应唤醒父 step）；
    - dispatch：派遣回切已就绪/投递中；
    - context_return：临时切片等待回切。
    """
    children = open_delegations(conn, wf, step_id)
    if children:
        review = [c for c in children if c["status"] == "in_review"]
        state = "delegation_review" if review else "delegation"
        return {"state": state, "waiting": not review,
                "review_pending": bool(review), "children": children}
    if wf.get("id"):
        row = conn.execute(
            "SELECT id, target_task_id, target_role, status FROM task_dispatches "
            "WHERE source_task_id=? AND COALESCE(source_workflow_id,'')=? "
            "AND COALESCE(source_step_id,'')=? "
            "AND status IN ('return_ready','returning') LIMIT 1",
            (wf["task_id"], wf["id"], step_id)).fetchone()
        if row is not None:
            return {"state": "dispatch", "waiting": True, "review_pending": False,
                    "children": [dict(row)]}
    if run_id:
        from . import dispatch as dispatch_service
        if dispatch_service.context_return_is_blocking(conn, run_id):
            return {"state": "context_return", "waiting": True,
                    "review_pending": False, "children": []}
    return {"state": "none", "waiting": False, "review_pending": False,
            "children": []}


def _assert_delegations_done(conn: sqlite3.Connection, wf: dict, step_id: str) -> None:
    """委派工单未完成前，父 step 不能收口。

    closed/cancelled 视为已解除（human 明确终止），不再永久阻塞父 step。
    """
    pending = open_delegations(conn, wf, step_id, limit=1)
    if pending:
        raise ConflictError(
            f"delegations not all done: {pending[0]['id']} is still open")


def get_workflow(conn: sqlite3.Connection, wid: str) -> dict | None:
    r = conn.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["definition"] = jloads(d.get("definition_json") or "{}", {})
    d["steps"] = _steps(conn, wid)
    return normalize_workflow_payload(d)


def get_for_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    r = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return get_workflow(conn, r["id"]) if r else None


def list_for_task(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """任务的执行历史（含已终止的旧执行），新到旧排序。"""
    rows = conn.execute(
        """SELECT w.id, w.task_id, w.status, w.created_by, w.created_at, w.updated_at,
                  w.termination_reason, w.terminated_at, w.previous_workflow_id,
                  w.execution_no, w.template_id, w.template_version,
                  w.weight, w.weight_source, w.weight_reason, w.definition_hash,
                  w.finished_at,
                  COUNT(s.step_id) AS step_count,
                  SUM(CASE WHEN s.status='approved' THEN 1 ELSE 0 END) AS approved_count
             FROM workflows w
             LEFT JOIN workflow_steps s ON s.workflow_id = w.id
            WHERE w.task_id=?
            GROUP BY w.id
            ORDER BY w.execution_no DESC, w.created_at DESC""",
        (task_id,),
    ).fetchall()
    from . import metrics as metrics_service

    out = []
    for row in rows:
        item = dict(row)
        item["weight"] = normalize_weight(item.get("weight"))
        item["metrics"] = metrics_service.compute(conn, item["id"])
        out.append(item)
    return out


def _steps(conn: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT s.*, l.holder AS lease_holder, l.expires_at AS lease_expires_at, "
            "l.fence AS lease_fence FROM workflow_steps s "
            "LEFT JOIN leases l ON l.id=s.lease_id "
            "WHERE s.workflow_id=? ORDER BY s.rowid", (wid,)):
        d = dict(r)
        d["workflow_id"] = wid
        d["depends_on"] = jloads(d.get("depends_on_json") or "[]", [])
        d["acceptance"] = jloads(d.get("acceptance_json") or "[]", [])
        d["gate"] = bool(d["gate"])
        d["kind"] = d.get("kind") or "task"
        d["on_reject"] = jloads(d.get("on_reject_json") or "[]", [])
        d["max_rework"] = int(d.get("max_rework") or 0)
        d["rework_count"] = int(d.get("rework_count") or 0)
        d["reversible"] = bool(d.get("reversible", 1))
        d["evidence_required"] = bool(d.get("evidence_required"))
        d["evidence"] = jloads(d.get("evidence_json") or "[]", [])
        d["decision_options"] = jloads(d.get("decision_options_json") or "[]", [])
        d["escalate"] = jloads(d.get("escalate_json") or "[]", [])
        d["scope_paths"] = jloads(d.get("scope_paths_json") or "[]", [])
        d["slice_spec"] = jloads(d.get("slice_spec_json") or "{}", {}) or None
        d["slice_lock"] = jloads(d.get("slice_lock_json") or "{}", {}) or None
        d["slice_replan"] = jloads(d.get("slice_replan_json") or "{}", {}) or None
        d["diff_stats"] = jloads(d.get("diff_stats_json") or "{}", {}) or {}
        out.append(d)
    return out


def _step_activity(conn: sqlite3.Connection, task_id: str, role: str | None,
                   step: dict, status: str, summary: str | None = None,
                   extra: dict | None = None) -> None:
    """把步骤状态变化写入任务会话，保证进展/问题对其它角色与人类实时可见。"""
    payload = {
        "step": step.get("step_id"),
        "title": step.get("title") or step.get("step_id"),
        "status": status,
    }
    if summary:
        payload["summary"] = summary
    if extra:
        payload.update(extra)
    workflow_id = str(step.get("workflow_id") or "") or None
    execution_no = None
    if workflow_id:
        row = conn.execute(
            "SELECT execution_no FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        execution_no = row["execution_no"] if row else None
    ledger.emit(
        conn, f"step.{status}", task_id=task_id, workflow_id=workflow_id,
        execution_no=execution_no, step_id=step.get("step_id"),
        slice_key=step.get("slice_key"), run_id=step.get("run_id"),
        actor_type="role" if role else "system", actor=role or "workflow",
        payload=payload, mirror_activity=True, activity_kind="step",
        activity_role=role or "workflow",
    )


_ADVANCE_LOCK = threading.RLock()


_LIFECYCLE_HELD = threading.local()


def _held_lifecycles() -> set[str]:
    held = getattr(_LIFECYCLE_HELD, "keys", None)
    if held is None:
        held = set()
        _LIFECYCLE_HELD.keys = held
    return held


@contextmanager
def workflow_lifecycle_lock(conn: sqlite3.Connection, task_id: str | None, *,
                            required: bool = True, wait_seconds: float = 0.0):
    """task 级 workflow 生命周期锁：进程内 RLock + 跨进程 DB lease。

    必须在 BEGIN IMMEDIATE / 任何写操作之前进入。嵌套调用（advance → finalize、
    review_step → finalize）通过线程本地集合重入，不会重复申请 DB lease。
    required=False 时抢不到锁返回 False，由调用方跳过本轮。
    """
    key = str(task_id or "").strip()
    if not key:
        yield True
        return
    held = _held_lifecycles()
    if key in held:
        yield True
        return

    # 进程内先互斥，避免同进程线程空转 DB lease。
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        if _ADVANCE_LOCK.acquire(blocking=False):
            break
        if time.monotonic() >= deadline:
            if required:
                raise ConflictError(
                    "workflow_busy: another lifecycle operation is running")
            yield False
            return
        time.sleep(0.05)

    lease = None
    try:
        while True:
            try:
                lease = leases.acquire(
                    conn, "workflow", f"lifecycle:{key}",
                    holder_type="system", holder="workflow",
                    ttl_seconds=config.WORKFLOW_LOCK_TTL_SECONDS,
                    commit=True)
                break
            except ConflictError:
                if time.monotonic() >= deadline:
                    if required:
                        raise ConflictError(
                            "workflow_busy: another process is operating on this task")
                    yield False
                    return
                time.sleep(0.05)
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
            try:
                if conn.in_transaction:
                    conn.rollback()
                leases.release(conn, lease["id"] if lease else None, commit=True)
            except sqlite3.Error:
                pass
    finally:
        _ADVANCE_LOCK.release()


def _transitive_downstream(steps: list[dict], roots: set[str]) -> set[str]:
    """返回依赖 roots（传递）的步骤，不含 roots 本身。"""
    dependents: dict[str, list[str]] = {}
    for step in steps:
        for dep in step.get("depends_on") or []:
            dependents.setdefault(str(dep), []).append(step["step_id"])
    out: set[str] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for nxt in dependents.get(cur, []):
            if nxt in roots or nxt in out:
                continue
            out.add(nxt)
            stack.append(nxt)
    return out


def _close_child_task_rows(conn: sqlite3.Connection, ids: list[str], now: str) -> None:
    from . import tasks as tasks_service
    for tid in ids:
        tasks_service.transition_task(
            conn, tid, "closed",
            from_statuses=("todo", "in_progress", "in_review"),
            fields={"lease_expires_at": None}, now=now)
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, "
            "updated_at=? WHERE task_id=? AND status IN ('pending','working')",
            (now, tid))
        conn.execute(
            "UPDATE runs SET status='abandoned', ended_at=? WHERE task_id=? "
            "AND status IN ('created','projecting','running','suspended',"
            "'submitted','reviewing')", (now, tid))


def close_step_delegations(conn: sqlite3.Connection, wid: str, step_id: str,
                           *, reason: str) -> list[str]:
    """step failed/execution terminated 时关闭其 open 委派，避免永久阻塞。"""
    wf = conn.execute(
        "SELECT task_id, execution_no FROM workflows WHERE id=?", (wid,)).fetchone()
    if wf is None:
        return []
    # 只关闭本次 execution 的委派；同名 step_id 在新 execution 里的子任务必须保留。
    rows = conn.execute(
        "SELECT id FROM tasks WHERE parent_id=? AND parent_step_id=? "
        "AND COALESCE(type,'') NOT IN ('dispatch','dispatched') "
        "AND status NOT IN ('done','closed') "
        "AND (parent_workflow_id IS NULL OR parent_workflow_id=?) "
        "AND (parent_execution_no IS NULL OR parent_execution_no=?)",
        (wf["task_id"], step_id, wid, wf["execution_no"])).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    now = now_iso()
    _close_child_task_rows(conn, ids, now)
    ledger.emit(
        conn, "delegation.closed_by_workflow", task_id=wf["task_id"],
        workflow_id=wid, step_id=step_id, actor_type="system", actor="workflow",
        payload={"children": ids, "reason": reason},
        mirror_activity=True, activity_kind="comment", activity_role="workflow")
    return ids


def _mention(conn, task_id: str, target: str, message: str,
             sender: str | None = None) -> None:
    """系统通知：发送者必须是人类能 @ 回去的真实角色（planner/Cindy/assignee）。"""
    from . import roles as roles_service
    from . import tasks as tasks_service
    who = sender or roles_service.notification_sender(conn, task_id)
    tasks_service.mention(conn, task_id, who, target, message)
