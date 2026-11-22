"""服务端角色状态推导：单一事实来源，前端只做标签/颜色映射。

当前只覆盖“任务内角色状态”（taskRoleState 的服务端等价物）；全局角色状态后续接入。
"""
from __future__ import annotations

from typing import Any


def _workflow_signal(steps: list[dict], role: str, slice_key: str | None) -> dict[str, bool]:
    selected = [
        s for s in steps
        if s.get("role_slug") == role
        and (slice_key is None or (s.get("slice_key") or None) == slice_key)
    ]
    return {
        "has_steps": bool(selected),
        "failed": any(s.get("status") == "failed" for s in selected),
        "rejected": any(s.get("status") == "rejected" for s in selected),
        "retry_wait": any(
            s.get("status") == "pending"
            and (int(s.get("recovery_count") or 0) > 0 or bool(s.get("next_retry_at")))
            for s in selected
        ),
        "running": any(s.get("status") == "running" for s in selected),
        "submitted": any(s.get("status") == "submitted" for s in selected),
        "pending": any(s.get("status") == "pending" for s in selected),
        "all_done": bool(selected) and all(
            s.get("status") in ("approved", "skipped") for s in selected),
    }


def derive_task_role_state(
    *,
    task_status: str,
    claim_status: str | None,
    workflow_status: str | None,
    steps: list[dict],
    role: str,
    slice_key: str | None,
    run_status: str | None,
    return_pending: bool,
    runtime: dict[str, Any],
    related_task_ids: set[str] | list[str],
    has_context: bool,
) -> dict[str, Any]:
    """返回 {state, queued, working_here, busy_elsewhere}。状态优先级与前端一致。"""
    signal = _workflow_signal(steps, role, slice_key)
    related = set(related_task_ids)
    busy = bool(runtime.get("alive") and runtime.get("status") == "busy")
    busy_task = runtime.get("current_task_id")
    working_here = bool(
        (busy and busy_task in related)
        or (runtime.get("alive") and runtime.get("context_task_id") in related
            and signal["running"])
    )
    busy_elsewhere = bool(busy and busy_task and busy_task not in related)
    queued = sum(
        1 for item in (runtime.get("queue") or [])
        if item.get("task_id") in related
    )

    task_closed = task_status == "closed" or workflow_status == "terminated"
    task_done = task_status == "done"
    task_in_review = task_status == "in_review"
    run_failed = run_status == "failed"
    run_waiting = run_status in ("submitted", "reviewing")
    failed = run_failed or signal["failed"] or signal["rejected"]
    waiting_human = task_in_review or run_waiting or signal["submitted"]
    all_done = task_done or claim_status == "approved" or signal["all_done"]
    released = claim_status == "released"
    pending = claim_status == "pending" or (
        signal["has_steps"] and not signal["running"] and not signal["submitted"]
        and not failed and not signal["all_done"]
    )

    if working_here:
        state = "working"
    elif task_closed:
        state = "closed"
    elif task_done:
        state = "done"
    elif failed:
        state = "rework" if signal["rejected"] and not signal["failed"] else "failed"
    elif queued > 0:
        state = "queued"
    elif signal["retry_wait"]:
        state = "retry"
    elif waiting_human:
        state = "waiting_human"
    elif all_done:
        state = "done"
    elif busy_elsewhere and queued == 0:
        state = "not_involved"
    elif released:
        state = "released"
    elif return_pending:
        state = "return_pending"
    elif has_context and not busy_elsewhere:
        state = "idle"
    elif pending:
        state = "todo"
    else:
        state = "not_involved"

    return {
        "state": state,
        "queued": queued,
        "working_here": working_here,
        "busy_elsewhere": busy_elsewhere,
    }


def _runtime_or_empty(manager, role: str) -> dict[str, Any]:
    if manager is not None and getattr(manager, "enabled", False):
        try:
            return manager.status(role) or {}
        except Exception:  # noqa: BLE001 —— 观测失败按无运行时处理
            pass
    return {}


def _task_contexts(conn, role: str, task_id: str) -> list[dict]:
    from . import daemon as daemon_service

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM role_task_sessions WHERE role_slug=?", (role,)).fetchall()]
    contexts = []
    for row in rows:
        tid = row.get("task_id")
        slice_key = row.get("slice_key")
        if not tid:
            tid, slice_key = daemon_service.context_parts(row.get("task_key") or "")
            if tid == daemon_service.GLOBAL_TASK_KEY:
                tid = None
        if tid == task_id:
            contexts.append({**row, "task_id": tid, "slice_key": slice_key})
    return contexts


def task_role_states(conn, task_id: str, manager=None) -> list[dict]:
    """任务内每个角色一份 {role_slug, state, queued, working_here, busy_elsewhere}。"""
    from . import tasks as tasks_service
    from . import workflow as workflow_service

    task = tasks_service.get(conn, task_id)
    if task is None:
        return []
    workflow = workflow_service.get_for_task(conn, task_id)
    steps = (workflow or {}).get("steps") or []

    try:
        from . import dispatch as dispatch_service
        dispatches = dispatch_service.list_dispatches(conn, task_id)
    except Exception:  # noqa: BLE001 —— 派遣信息失败不阻断角色状态
        dispatches = []
    related = {task_id}
    try:
        related.update(d["id"] for d in tasks_service.list_delegations(conn, task_id))
    except Exception:  # noqa: BLE001
        pass
    related.update(
        d["target_task_id"] for d in dispatches if d.get("target_task_id"))

    runs = [dict(r) for r in conn.execute(
        "SELECT id, role_slug, status, started_at, slice_key FROM runs WHERE task_id=?",
        (task_id,)).fetchall()]
    claims = {c["role_slug"]: c["status"] for c in (task.get("role_claims") or [])}
    roles = list(dict.fromkeys([
        *(task.get("assignees") or []),
        *claims.keys(),
        *(s.get("role_slug") for s in steps if s.get("role_slug")),
        *(d.get("target_role") for d in dispatches if d.get("target_role")),
    ]))

    out = []
    for role in roles:
        runtime = _runtime_or_empty(manager, role)
        contexts = _task_contexts(conn, role, task_id)
        has_context = any(c.get("session_id") for c in contexts)
        wf_slice = None
        if contexts:
            wanted_key = runtime.get("context_key")
            wanted_slice = runtime.get("context_slice_key")
            chosen = next(
                (c for c in contexts if wanted_key and c.get("context_key") == wanted_key),
                None)
            if chosen is None and wanted_slice is not None:
                chosen = next(
                    (c for c in contexts
                     if (c.get("slice_key") or None) == wanted_slice), None)
            chosen = chosen or contexts[0]
            wf_slice = chosen.get("slice_key")

        run_status = None
        current_run_id = runtime.get("current_run_id")
        if current_run_id and runtime.get("current_task_id") == task_id:
            match = next((r for r in runs if r["id"] == current_run_id), None)
            run_status = match["status"] if match else None
        if run_status is None:
            candidates = [
                r for r in runs if r.get("role_slug") == role
                and (not contexts or (r.get("slice_key") or None) == (wf_slice or None))
            ]
            if not candidates:
                candidates = [r for r in runs if r.get("role_slug") == role]
            candidates.sort(
                key=lambda r: str(r.get("started_at") or ""), reverse=True)
            run_status = candidates[0]["status"] if candidates else None

        return_pending = any(
            d.get("target_role") == role
            and d.get("status") in ("return_ready", "returning")
            for d in dispatches)
        derived = derive_task_role_state(
            task_status=task.get("status") or "",
            claim_status=claims.get(role),
            workflow_status=(workflow or {}).get("status"),
            steps=steps,
            role=role,
            slice_key=wf_slice,
            run_status=run_status,
            return_pending=return_pending,
            runtime=runtime,
            related_task_ids=related,
            has_context=has_context,
        )
        out.append({"role_slug": role, **derived})
    return out
