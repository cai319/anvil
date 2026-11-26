"""Workflow 评审层：决策/升级/人工评审与任务最终收口。"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, now_iso
from . import dossier, leases, ledger, reviews
from .workflow_common import (
    STEP_DONE, _assert_current_run, _assert_delegations_done, _mention,
    _step_activity, _transitive_downstream, get_workflow, logger,
    workflow_lifecycle_lock,
)


def decide_step(conn: sqlite3.Connection, wid: str, step_id: str, role: str | None,
                approve: bool, feedback: str | None = None,
                run_id: str | None = None,
                packet_id: str | None = None,
                rework_targets: list[str] | None = None,
                reasons: list[str] | None = None,
                override_reason: str | None = None,
                acknowledge_stale: bool = False) -> dict:
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _decide_step_locked(
            conn, wid, step_id, role, approve, feedback, run_id, packet_id,
            rework_targets, reasons, override_reason, acknowledge_stale)


def _decide_step_locked(conn: sqlite3.Connection, wid: str, step_id: str,
                        role: str | None,
                        approve: bool, feedback: str | None = None,
                        run_id: str | None = None,
                        packet_id: str | None = None,
                        rework_targets: list[str] | None = None,
                        reasons: list[str] | None = None,
                        override_reason: str | None = None,
                        acknowledge_stale: bool = False) -> dict:
    """决策角色 approve/reject 一个 decision 步骤。

    reject 时把 on_reject 闭包内的步骤重置为 pending，并把 decision 自身重置为 pending；
    下一轮 advance 会先重跑目标步骤，再重跑 decision。超过 max_rework 时 decision failed
    并 @human（事件 rework_exhausted），不再自动推进。
    """
    now = now_iso()
    note = feedback or ("决策通过" if approve else "请按决策意见修改后重新提交")
    conn.execute("BEGIN IMMEDIATE")
    try:
        wf = get_workflow(conn, wid)
        if wf is None:
            raise LookupError(wid)
        step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
        if step is None:
            raise LookupError(step_id)
        _assert_current_run(step, run_id)
        _assert_delegations_done(conn, wf, step_id)
        if step["status"] == "running":
            leases.assert_step(conn, step, run_id)
        if step.get("kind") != "decision":
            raise ConflictError(f"step {step_id} is not a decision step")
        is_human = role == "human"
        if role and not is_human and role != step["role_slug"]:
            raise PermissionError(f"role {role} cannot decide step {step_id}")
        authority = reviews.authority_of(step)
        expected = ("running", "submitted") if is_human else ("running",)
        if step["status"] not in expected:
            raise ConflictError(
                f"step {step_id} is {step['status']}, expected {'/'.join(expected)}")
        if authority == "human" and not is_human:
            raise PermissionError(f"step {step_id} requires a human decision")
        if authority == "ai" and is_human and not override_reason:
            raise ConflictError(
                f"override_reason_required: human override of ai decision {step_id}")
        packet = reviews.ensure_packet(
            conn, wf, step, created_by=role or "system", commit=False)
        approval_triggers = reviews.escalation_triggers(step, packet)
        if approve and reviews.packet_has_stale_evidence(packet) \
                and not acknowledge_stale and not override_reason:
            raise ConflictError(
                "evidence_stale: acknowledge stale evidence or refresh the packet")
        if approve and not is_human and approval_triggers:
            raise ConflictError(
                f"escalation_required: {', '.join(approval_triggers)}; "
                f"step {step_id} must be escalated instead of approved by AI")

        if authority == "joint" and approve and not is_human:
            # AI may only recommend approval under joint authority. The run is
            # finished, the step waits for a human, and the packet stays open.
            if step["run_id"]:
                conn.execute(
                    "UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                    (now, step["run_id"]))
                ledger.emit(
                    conn, "run.status_changed", task_id=wf["task_id"], workflow_id=wid,
                    execution_no=wf.get("execution_no"), step_id=step_id,
                    run_id=step["run_id"], actor_type="role", actor=step["role_slug"],
                    payload={"status": "completed", "prev": "running"},
                    mirror_activity=True, activity_kind="run",
                    activity_role=step["role_slug"],
                )
            conn.execute(
                "UPDATE workflow_steps SET status='submitted', submitted_at=?, feedback=?, "
                "updated_at=? WHERE workflow_id=? AND step_id=?",
                (now, feedback, now, wid, step_id))
            _step_activity(conn, wf["task_id"], step["role_slug"], step, "submitted",
                           "AI 建议通过，等待人工确认")
            reviews.record_recommendation(
                conn, wf, step, actor=role or step["role_slug"], decision="approve",
                feedback=feedback, commit=False)
            leases.release(conn, step.get("lease_id"), commit=False)
            conn.commit()
            return get_workflow(conn, wid)  # type: ignore[return-value]

        if step["run_id"]:
            # 直接 UPDATE，避免 runs.set_status 内部 commit 打断 BEGIN IMMEDIATE。
            run_row = conn.execute(
                "SELECT status FROM runs WHERE id=?", (step["run_id"],)).fetchone()
            if run_row is None or run_row["status"] != "completed":
                conn.execute("UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                             (now, step["run_id"]))
                ledger.emit(
                    conn, "run.status_changed", task_id=wf["task_id"], workflow_id=wid,
                    execution_no=wf.get("execution_no"), step_id=step_id,
                    run_id=step["run_id"], actor_type="role", actor=step["role_slug"],
                    payload={"status": "completed",
                             "prev": run_row["status"] if run_row else None},
                    mirror_activity=True, activity_kind="run",
                    activity_role=step["role_slug"],
                )

        if approve:
            conn.execute(
                "UPDATE workflow_steps SET status='approved', feedback=COALESCE(?, feedback), "
                "ended_at=?, updated_at=? WHERE workflow_id=? AND step_id=?",
                (feedback, now, now, wid, step_id))
            _step_activity(conn, wf["task_id"], step["role_slug"], step,
                           "approved", feedback or None)
            decision_reasons = reasons
            if is_human and approval_triggers:
                decision_reasons = sorted(
                    set(reasons or []) | set(approval_triggers))
            reviews.record_decision(
                conn, wf, step, actor_type="human" if role == "human" else "role",
                actor=role or step["role_slug"], decision="approve",
                feedback=feedback or note, reasons=decision_reasons,
                packet_id=packet_id,
                override_reason=override_reason or (
                    feedback if role == "human"
                    and (authority != "human" or approval_triggers) else None),
                commit=False,
            )
            leases.release(conn, step.get("lease_id"), commit=False)
            conn.commit()
            _finalize_if_done(conn, wid)
            return get_workflow(conn, wid)  # type: ignore[return-value]

        count = int(step.get("rework_count") or 0)
        max_rework = int(step.get("max_rework") or 0)
        if count >= max_rework:
            conn.execute(
                "UPDATE workflow_steps SET status='failed', feedback=?, "
                "failure_reason='rework_exhausted', ended_at=?, updated_at=? "
                "WHERE workflow_id=? AND step_id=?",
                (note, now, now, wid, step_id))
            _step_activity(conn, wf["task_id"], step["role_slug"], step,
                           "rework_exhausted", note)
            from . import roles as roles_service
            sender = roles_service.notification_sender(
                conn, wf["task_id"], wf.get("created_by"))
            task_row = conn.execute(
                "SELECT title FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
            task_title = (task_row["title"] if task_row else "") or "当前任务"
            ledger.emit_activity(
                conn, task_id=wf["task_id"],
                role_slug=sender,
                kind="mention",
                payload=jdumps({
                    "target": "human",
                    "message": f"任务「{task_title}」的决策步骤「{step['title']}」"
                               f"已超过最大重做次数（{max_rework} 次），需要你决定："
                               "继续返工、调整步骤或终止任务。继续返工会按现有工作流 "
                               "再跑一轮；调整步骤需要修改工作流；终止任务会停止后续步骤。"
                               f"最近反馈：{note}",
                    "event": "rework_exhausted", "workflow_id": wid, "step_id": step_id,
                }),
                now=now)
            reviews.record_decision(
                conn, wf, step, actor_type="human" if role == "human" else "role",
                actor=role or step["role_slug"], decision="escalate",
                feedback=note, reasons=["rework_exhausted"], packet_id=packet_id,
                commit=False,
            )
            leases.release(conn, step.get("lease_id"), commit=False)
            conn.commit()
            return get_workflow(conn, wid)  # type: ignore[return-value]

        count += 1
        by_id = {s["step_id"]: s for s in wf["steps"]}
        for tid in (step.get("on_reject") or []):
            target = by_id.get(tid) or {}
            conn.execute(
                "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
                "ended_at=NULL, summary=NULL, failure_reason=NULL, feedback=?, "
                "recovery_count=0, next_retry_at=NULL, nudge_count=0, last_nudge_at=NULL, "
                "lease_id=NULL, fence_token=0, updated_at=? "
                "WHERE workflow_id=? AND step_id=?",
                (note, now, wid, tid))
            leases.release(conn, target.get("lease_id"), commit=False)
            target_role = target.get("role_slug") or ""
            if target_role:
                ledger.emit_activity(
                    conn, task_id=wf["task_id"],
                    role_slug=step["role_slug"],
                    kind="mention",
                    payload=jdumps({
                        "target": target_role,
                        "message": f"决策打回步骤「{target.get('title', tid)}」：{note}",
                    }),
                    now=now)
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "ended_at=NULL, summary=NULL, feedback=?, rework_count=?, updated_at=?, "
            "lease_id=NULL, fence_token=0 WHERE workflow_id=? AND step_id=?",
            (note, count, now, wid, step_id))
        _step_activity(conn, wf["task_id"], step["role_slug"], step, "rework", note,
                       extra={"rework_count": count})
        reviews.record_decision(
            conn, wf, step, actor_type="human" if role == "human" else "role",
            actor=role or step["role_slug"], decision="reject", feedback=note,
            rework_targets=rework_targets or [str(x) for x in (step.get("on_reject") or [])],
            reasons=reasons, packet_id=packet_id, override_reason=override_reason,
            commit=False,
        )
        leases.release(conn, step.get("lease_id"), commit=False)
        conn.commit()
        return get_workflow(conn, wid)  # type: ignore[return-value]
    except BaseException:
        conn.rollback()
        raise


def escalate_step(conn: sqlite3.Connection, wid: str, step_id: str, role: str | None,
                  reason: str, triggers: list[str] | None = None,
                  proposed_decision: str | None = None,
                  proposed_feedback: str | None = None,
                  run_id: str | None = None) -> dict:
    """AI decider escalates to a human; the packet stays open for human decision."""
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _escalate_step_locked(
            conn, wid, step_id, role, reason, triggers,
            proposed_decision, proposed_feedback, run_id)


def _escalate_step_locked(conn: sqlite3.Connection, wid: str, step_id: str,
                          role: str | None, reason: str,
                          triggers: list[str] | None = None,
                          proposed_decision: str | None = None,
                          proposed_feedback: str | None = None,
                          run_id: str | None = None) -> dict:
    from . import runs as runs_service

    now = now_iso()
    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    if wf.get("status") != "running":
        raise ConflictError(
            f"workflow {wid} is {wf.get('status')}; cannot escalate a step")
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None:
        raise LookupError(step_id)
    _assert_current_run(step, run_id)
    if step["status"] == "running":
        leases.assert_step(conn, step, run_id)
    if step.get("kind") != "decision":
        raise ConflictError(f"step {step_id} is not a decision step")
    if role and role != "human" and role != step["role_slug"]:
        raise PermissionError(f"role {role} cannot escalate step {step_id}")
    if step["status"] not in ("running", "submitted"):
        raise ConflictError(f"step {step_id} is {step['status']}, expected running")
    if not (reason or "").strip():
        raise ConflictError("escalation reason is required")
    packet = reviews.ensure_packet(
        conn, wf, step, created_by=role or "system", commit=False)
    triggers = sorted(set(triggers or []) | set(
        reviews.escalation_triggers(step, packet)))
    if step["run_id"]:
        runs_service.set_status(
            conn, step["run_id"], "completed", ended_at=now,
            from_statuses=("created", "projecting", "running", "suspended"))
    # CAS：决策步骤只能从 running/submitted 升级为 submitted；已 approve 的步骤
    # 不能被并发的 escalate 覆盖回待审。
    cur = conn.execute(
        "UPDATE workflow_steps SET status='submitted', submitted_at=?, feedback=?, "
        "updated_at=? WHERE workflow_id=? AND step_id=? "
        "AND status IN ('running','submitted')",
        (now, reason, now, wid, step_id))
    if cur.rowcount != 1:
        raise ConflictError(
            f"step {step_id} state changed while escalating; refresh and retry")
    _step_activity(conn, wf["task_id"], role or step["role_slug"], step,
                   "escalated", reason, extra={"triggers": triggers or []})
    reviews.record_escalation(
        conn, wf, step, actor=role or step["role_slug"], reason=reason,
        triggers=triggers, proposed_decision=proposed_decision,
        proposed_feedback=proposed_feedback, commit=False)
    leases.release(conn, step.get("lease_id"), commit=False)
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def review_step(conn: sqlite3.Connection, wid: str, step_id: str, approve: bool,
                feedback: str | None = None,
                reasons: list[str] | None = None,
                rework_targets: list[str] | None = None,
                packet_id: str | None = None,
                override_reason: str | None = None,
                acknowledge_stale: bool = False) -> dict:
    """人工审核 gate 步骤：通过则解锁下游；驳回则回到 pending 等待角色重做。

    驳回时可指定返工范围（当前步骤或任意上游祖先）；默认只重做当前步骤。
    返工范围会扩展到下游闭包，避免 approved 步骤依赖 pending 步骤。
    """
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _review_step_locked(
            conn, wid, step_id, approve, feedback, reasons, rework_targets,
            packet_id, override_reason, acknowledge_stale)


def _review_step_locked(conn: sqlite3.Connection, wid: str, step_id: str,
                        approve: bool,
                        feedback: str | None = None,
                        reasons: list[str] | None = None,
                        rework_targets: list[str] | None = None,
                        packet_id: str | None = None,
                        override_reason: str | None = None,
                        acknowledge_stale: bool = False) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        wf = get_workflow(conn, wid)
        if wf is None:
            raise LookupError(wid)
        step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
        if step is None:
            raise LookupError(step_id)
        if step.get("kind") == "decision":
            raise ConflictError(
                "decision steps are decided by their role, not a human gate")
        if step["status"] != "submitted":
            raise ConflictError(
                f"step {step_id} is {step['status']}, expected submitted")
        targets = sorted({step_id, *[str(x) for x in (rework_targets or [])]})
        by_id = {s["step_id"]: s for s in wf["steps"]}
        if not approve:
            ancestors: set[str] = set()
            stack = [str(x) for x in step.get("depends_on") or []]
            while stack:
                cur = stack.pop()
                if cur in ancestors:
                    continue
                ancestors.add(cur)
                stack.extend(
                    str(x) for x in (by_id.get(cur, {}).get("depends_on") or []))
            allowed = ancestors | {step_id}
            for target in targets:
                if target not in allowed:
                    raise ConflictError(
                        f"rework target {target} is not an ancestor of step {step_id}")
                if (by_id.get(target) or {}).get("kind") == "plan_slices":
                    raise ConflictError(
                        f"rework target {target} is a plan_slices step and "
                        "cannot be reset")

        packet = reviews.ensure_packet(
            conn, wf, step, created_by="human", commit=False)
        latest = reviews.latest_packet(conn, wid, step_id)
        if packet_id and (latest is None or latest["id"] != packet_id):
            raise ConflictError("packet_superseded: refresh the review packet")
        if approve and reviews.packet_has_stale_evidence(packet) \
                and not acknowledge_stale and not override_reason:
            raise ConflictError(
                "evidence_stale: acknowledge stale evidence or refresh the packet")

        now = now_iso()
        reset_ids: set[str] = set()
        if approve:
            cur = conn.execute(
                "UPDATE workflow_steps SET status='approved', ended_at=?, "
                "feedback=COALESCE(?, feedback), updated_at=? "
                "WHERE workflow_id=? AND step_id=? AND status='submitted'",
                (now, feedback, now, wid, step_id))
            if cur.rowcount != 1:
                raise ConflictError(
                    f"step {step_id} state changed; refresh before reviewing")
            _step_activity(conn, wf["task_id"], "human", step, "approved",
                           feedback or step.get("summary"))
        else:
            reset_ids = set(targets) | _transitive_downstream(
                wf["steps"], set(targets))
            for sid in reset_ids:
                target_step = by_id.get(sid) or {}
                status = target_step.get("status") or "unknown"
                if status == "running" or (
                        status == "pending" and (
                            target_step.get("run_id") or target_step.get("lease_id"))):
                    raise ConflictError(
                        f"rework target {sid} is running or being dispatched; "
                        "wait for it to finish")
                if status not in ("pending", "submitted", "approved",
                                  "failed", "rejected"):
                    continue
                if status == "pending":
                    continue
                conn.execute(
                    "UPDATE workflow_steps SET status='pending', run_id=NULL, "
                    "started_at=NULL, ended_at=NULL, summary=NULL, "
                    "failure_reason=NULL, recovery_count=0, next_retry_at=NULL, "
                    "nudge_count=0, last_nudge_at=NULL, lease_id=NULL, "
                    "fence_token=0, feedback=COALESCE(?, feedback), updated_at=? "
                    "WHERE workflow_id=? AND step_id=?",
                    (feedback, now, wid, sid))
                conn.execute(
                    "UPDATE gate_packets SET status='superseded' "
                    "WHERE workflow_id=? AND step_id=? "
                    "AND status IN ('pending_human','pending_ai')",
                    (wid, sid))
            _step_activity(conn, wf["task_id"], "human", step, "rejected",
                           feedback or "请按评审意见修改后重新提交")
        reviews.record_decision(
            conn, wf, step, actor_type="human", actor="human",
            decision="approve" if approve else "reject",
            feedback=feedback, reasons=reasons,
            rework_targets=[] if approve else sorted(reset_ids),
            packet_id=packet_id or (packet.get("id") if packet else None),
            override_reason=override_reason, commit=False,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if not approve:
        for target in targets:
            target_step = by_id.get(target) or {}
            if target != step_id and target_step.get("role_slug"):
                _mention(
                    conn, wf["task_id"], target_step["role_slug"],
                    f"评审驳回，需重做步骤「{target_step.get('title', target)}」："
                    f"{feedback or '请按评审意见修改后重新提交'}。")
        _mention(
            conn, wf["task_id"], step["role_slug"],
            f"步骤「{step['title']}」被驳回：{feedback or '请按评审意见修改后重新提交'}。")
        try:
            from . import experience_capture
            experience_capture.capture_rework(
                conn, wf["task_id"], workflow_id=wid, step_id=step_id,
                feedback=feedback, actor="human")
        except Exception:  # noqa: BLE001 —— 捕获失败不影响评审
            pass
        try:
            from . import rsi as rsi_service
            rsi_service.observe_rework(
                conn, wf["task_id"], role=step.get("role_slug"),
                step_id=step_id, feedback=feedback)
        except Exception:  # noqa: BLE001 —— RSI 观测失败不影响评审
            pass
    _finalize_if_done(conn, wid)
    return get_workflow(conn, wid)  # type: ignore[return-value]


def gates(conn: sqlite3.Connection) -> list[dict]:
    """待人工审核的 gate 步骤（供收件箱聚合）。"""
    rows = conn.execute(
        """SELECT s.workflow_id, s.step_id, s.title, s.role_slug, s.summary, s.updated_at,
                  w.task_id, t.title AS task_title
             FROM workflow_steps s
             JOIN workflows w ON w.id = s.workflow_id
             LEFT JOIN tasks t ON t.id = w.task_id
            WHERE s.gate=1 AND s.status='submitted' AND w.status='running'
              AND COALESCE(s.kind,'task')='task'
            ORDER BY s.updated_at"""
    ).fetchall()
    return [dict(r) for r in rows]


def task_review_blocker(conn: sqlite3.Connection, task_id: str) -> str | None:
    """返回 task 不能进入最终评审的原因；None 表示可以进入。"""
    task = conn.execute(
        "SELECT status, issue_board_enabled FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if task is None:
        return "task_missing"
    if task["status"] not in ("in_progress", "todo"):
        return "task_not_in_progress"
    latest = conn.execute(
        "SELECT id, status FROM workflows WHERE task_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)
    ).fetchone()
    # 委派工单未完成时，父任务不能进入最终评审；closed 视为终止，不永久阻塞。
    # 只检查当前 execution；旧 execution 的残留委派不阻塞新执行。
    child_sql = ("SELECT 1 FROM tasks WHERE parent_id=? "
                 "AND COALESCE(type,'') NOT IN ('dispatch','dispatched') "
                 "AND status NOT IN ('done','closed')")
    child_params: list = [task_id]
    if latest is not None and latest["id"]:
        child_sql += " AND (parent_workflow_id IS NULL OR parent_workflow_id=?)"
        child_params.append(latest["id"])
    if conn.execute(child_sql + " LIMIT 1", child_params).fetchone() is not None:
        return "pending_child"
    if task["issue_board_enabled"]:
        from . import findings as findings_service
        if findings_service.board_status(conn, task_id) != "completed":
            return "issue_board"
    if latest is not None and latest["status"] != "done":
        return "workflow_not_done"
    return None


def finalize_task_review_if_ready(conn: sqlite3.Connection, task_id: str) -> bool:
    """把任务推进到最终人工评审；问题板未完成时保持 in_progress。

    幂等：任务已 in_review/done 或最新 workflow 尚未 done 时不做任何事。
    """
    if task_review_blocker(conn, task_id) is not None:
        return False
    task = conn.execute(
        "SELECT title, issue_board_enabled, review_rejected_at FROM tasks WHERE id=?",
        (task_id,)).fetchone()
    if task is not None and task["review_rejected_at"]:
        return False
    now = now_iso()
    from . import tasks as tasks_service
    moved = tasks_service.transition_task(
        conn, task_id, "in_review",
        from_statuses=("in_progress", "todo"),
        fields={"lease_expires_at": None}, now=now)
    if not moved:
        # 并发/重复收口：状态已变化，不再重复 @human。
        return False
    conn.commit()
    from . import roles as roles_service
    sender = roles_service.notification_sender(conn, task_id)
    title = (task["title"] if task else "") or "当前任务"
    board_note = ("问题板（安全发现清单）已就绪。"
                  if task and task["issue_board_enabled"] else "")
    _mention(conn, task_id, "human",
             f"任务「{title}」工作流全部步骤完成。{board_note}"
             "请打开任务会话核对最终产出与交付件清单，然后做最终评审：通过或驳回；"
             "驳回时请写明具体原因。",
             sender=sender)
    conn.commit()
    return True


def _finalize_if_done(conn: sqlite3.Connection, wid: str) -> None:
    """所有步骤完成 → workflow done；问题板完成后再进入最终人工评审。

    读/写必须在同一把 task 生命周期锁和 BEGIN IMMEDIATE 内完成，并且 UPDATE
    带 status='running' 条件，避免与 external_nodes/restart 的收口竞态。
    """
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(conn, task_id, required=False) as acquired:
        if not acquired:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            wf = get_workflow(conn, wid)
            if wf is None or wf["status"] != "running":
                conn.commit()
                return
            if not all(s["status"] in STEP_DONE for s in wf["steps"]):
                conn.commit()
                return
            now = now_iso()
            cur = conn.execute(
                "UPDATE workflows SET status='done', finished_at=?, updated_at=? "
                "WHERE id=? AND status='running'", (now, now, wid))
            if cur.rowcount != 1:
                conn.rollback()
                return
            ledger.emit(
                conn, "execution.finished", task_id=wf["task_id"], workflow_id=wid,
                execution_no=wf.get("execution_no"), actor_type="system",
                actor="workflow",
                payload={"steps": len(wf.get("steps") or [])},
            )
            dossier.persist(conn, wid, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        try:
            if not finalize_task_review_if_ready(conn, wf["task_id"]):
                blocker = task_review_blocker(conn, wf["task_id"])
                if blocker == "issue_board":
                    # 只有问题板未完成才给 reviewer 创建 findings run；其它原因
                    # （task 已 in_review/done、子任务未完成等）不能误派活。
                    from . import findings as findings_service
                    from . import runs as runs_service
                    from . import tasks as tasks_service
                    reviewers = findings_service.reviewer_roles(conn, wf["task_id"])
                    for role in reviewers:
                        tasks_service.mention(
                            conn, wf["task_id"], "system", role,
                            "工作流已完成，但问题板尚未完成；请补齐 findings 并执行 "
                            "`anvil finding complete --task ... --summary ...`。")
                        runs_service.create_run(
                            conn, role, wf["task_id"],
                            prompt=("[anvil:findings] 工作流已完成，但问题板尚未完成。"
                                    "请补齐/复核 findings，冲突或遗漏要与其他角色确认；"
                                    "完成后执行 `anvil finding complete --task "
                                    "$ANVIL_TASK_ID --summary \"...\"`。"))
                    conn.commit()
        except Exception:  # noqa: BLE001 —— 收口已完成，findings 派发失败不回滚 done
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            logger.exception(
                "finalize findings reviewers failed workflow=%s", wid)
