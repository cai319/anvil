"""Workflow 评审层：决策/升级/人工评审与任务最终收口。"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, jloads, now_iso
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
                acknowledge_stale: bool = False,
                verdicts: list[dict] | None = None) -> dict:
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _decide_step_locked(
            conn, wid, step_id, role, approve, feedback, run_id, packet_id,
            rework_targets, reasons, override_reason, acknowledge_stale,
            verdicts)


def _decide_step_locked(conn: sqlite3.Connection, wid: str, step_id: str,
                        role: str | None,
                        approve: bool, feedback: str | None = None,
                        run_id: str | None = None,
                        packet_id: str | None = None,
                        rework_targets: list[str] | None = None,
                        reasons: list[str] | None = None,
                        override_reason: str | None = None,
                        acknowledge_stale: bool = False,
                        verdicts: list[dict] | None = None) -> dict:
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
        # 逐项判定：引擎 verdict 由绿跑证据决定；manual verdict 只能由 decide 提交。
        from . import slice_verdicts
        try:
            built_verdicts = slice_verdicts.build_verdicts(
                conn, wf, step, verdicts)
        except slice_verdicts.VerdictError as exc:
            raise ConflictError(f"verdict_invalid: {exc}") from exc
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
        # 逐项判定：存在 unknown / unmet 时任何决策者都不能通过。
        blocking = slice_verdicts.blocking_verdicts(
            built_verdicts, approve=approve)
        if blocking:
            raise ConflictError(blocking)

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
                verdicts=built_verdicts,
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
            verdicts=built_verdicts, commit=False,
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


def _reset_rework_steps(conn: sqlite3.Connection, wf: dict, by_id: dict,
                        targets: list[str] | set[str], feedback: str | None,
                        now: str) -> set[str]:
    """复用 reject-to-ancestor 重置：目标与其下游闭包回到 pending。

    replan 与人工驳回共用这套语义，避免出现第二套状态族。
    """
    reset_ids = set(targets) | _transitive_downstream(wf["steps"], set(targets))
    for sid in reset_ids:
        target_step = by_id.get(sid) or {}
        status = target_step.get("status") or "unknown"
        if status == "running" or (
                status == "pending" and (
                    target_step.get("run_id") or target_step.get("lease_id"))):
            raise ConflictError(
                f"rework target {sid} is running or being dispatched; "
                "wait for it to finish")
        if status not in ("pending", "submitted", "approved", "failed",
                          "rejected"):
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
            (feedback, now, wf["id"], sid))
        conn.execute(
            "UPDATE gate_packets SET status='superseded' "
            "WHERE workflow_id=? AND step_id=? "
            "AND status IN ('pending_human','pending_ai')",
            (wf["id"], sid))
        if target_step.get("kind") == "plan_slices":
            # 计划被驳回：清空 plan_json 与 packet 指针，角色可重新提交。
            conn.execute(
                "UPDATE workflow_steps SET plan_json=NULL, packet_id=NULL, "
                "submitted_at=NULL WHERE workflow_id=? AND step_id=?",
                (wf["id"], sid))
    return reset_ids


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
        is_plan_gate = step.get("kind") == "plan_slices"
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
                # 已展开的 plan_slices 步骤不能被更宽范围的返工重置；只有 plan 门
                # 自身（尚未展开）可以在驳回时回到 pending 等待重新提交。
                if ((by_id.get(target) or {}).get("kind") == "plan_slices"
                        and target != step_id):
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
        if approve and is_plan_gate:
            from .workflow_definition import build_plan_review
            plan = jloads(step.get("plan_json") or "{}", {})
            if not isinstance(plan, dict) or not isinstance(plan.get("slices"), list):
                raise ConflictError(
                    "plan_gate_blocked: plan is missing a `slices` list")
            review = build_plan_review(conn, wf, plan)
            if review["blocking"]:
                raise ConflictError(
                    "plan_gate_blocked: " + "; ".join(review["blocking"]))

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
            if is_plan_gate:
                # 人工批准后才展开切片并改挂下游依赖。
                from .workflow_definition import expand_approved_plan
                expand_approved_plan(conn, wf, step, now)
            _step_activity(conn, wf["task_id"], "human", step, "approved",
                           feedback or step.get("summary"))
        else:
            reset_ids = _reset_rework_steps(
                conn, wf, by_id, targets, feedback, now)
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


# ---- 显式 replan 与弱化检测（v1.62 #10）------------------------------------

def _slice_scope_step(wf: dict, step: dict) -> dict:
    """把任意切片步骤解析为该切片的 scope 步骤。"""
    slice_key = str(step.get("slice_key") or "").strip()
    if not slice_key:
        raise ConflictError("replan requires a slice step")
    scope = next(
        (s for s in wf["steps"]
         if s.get("slice_key") == slice_key
         and str(s.get("step_id") or "").endswith(".scope")), None)
    if scope is None:
        raise ConflictError(f"slice {slice_key} has no scope step")
    return scope


def _replan_record(scope: dict) -> dict:
    record = scope.get("slice_replan")
    return record if isinstance(record, dict) else {}


def _red_evidence_ids(conn: sqlite3.Connection, wf: dict, slice_key: str,
                      red_evidence) -> tuple[list[str], list[str]]:
    """校验新红证据只来自引擎检查作业；返回 (覆盖的检查 id, 作业 id)。"""
    if not red_evidence:
        return [], []
    from . import sandbox as job_service
    if isinstance(red_evidence, str):
        raw_items = [red_evidence]
    elif isinstance(red_evidence, list):
        raw_items = red_evidence
    elif isinstance(red_evidence, dict):
        raw_items = [x for value in red_evidence.values()
                     for x in (value if isinstance(value, list) else [value])
                     if x]
    else:
        raise ConflictError("red_evidence must be a list of check-run ids")
    covered: set[str] = set()
    job_ids: list[str] = []
    for raw in raw_items:
        job_id = str(raw or "").strip()
        if not job_id:
            continue
        job = job_service.get_job(conn, job_id)
        if job is None or job.get("kind") != "check":
            raise ConflictError(
                f"red_evidence_invalid: {job_id} is not an engine check run")
        spec = job.get("spec") or {}
        ref = spec.get("lock_ref") if isinstance(spec, dict) else None
        if not isinstance(ref, dict):
            ref = spec.get("green_ref") if isinstance(spec, dict) else None
        if not isinstance(ref, dict) or str(ref.get("workflow_id")) != str(wf["id"]):
            raise ConflictError(
                f"red_evidence_invalid: {job_id} belongs to another workflow")
        if str(ref.get("slice_key") or slice_key) != str(slice_key):
            raise ConflictError(
                f"red_evidence_invalid: {job_id} belongs to another slice")
        result = job.get("check_result") or {}
        if (str(job.get("status")) in ("queued", "running", "waiting_approval")
                or result.get("classification") not in
                ("valid_red", "trivial_red")):
            raise ConflictError(
                f"red_evidence_invalid: {job_id} is not a valid red run")
        check_id = str(result.get("check_id") or "")
        step_id = str(ref.get("step_id") or ref.get("scope_step_id") or "")
        if step_id and check_id.startswith(step_id + "."):
            covered.add(check_id[len(step_id) + 1:])
        job_ids.append(job_id)
    return sorted(covered), job_ids


def _cancel_slice_check_jobs(conn: sqlite3.Connection, wid: str,
                             scope_step_id: str, slice_key: str,
                             now: str) -> None:
    """作废未完成的红/绿跑，避免 replan 后旧证据被 worker 继续写回。"""
    rows = conn.execute(
        "SELECT id, spec_json, status FROM sandbox_jobs "
        "WHERE kind='check' AND status IN "
        "('queued','running','waiting_approval')").fetchall()
    for row in rows:
        try:
            spec = jloads(row["spec_json"] or "{}", {})
        except (TypeError, ValueError):
            continue
        ref = spec.get("lock_ref") if isinstance(spec, dict) else None
        if not isinstance(ref, dict):
            ref = spec.get("green_ref") if isinstance(spec, dict) else None
        if not isinstance(ref, dict):
            continue
        if str(ref.get("workflow_id") or "") != str(wid):
            continue
        if (str(ref.get("step_id") or ref.get("scope_step_id") or "")
                != str(scope_step_id)
                and str(ref.get("slice_key") or "") != str(slice_key)):
            continue
        conn.execute(
            "UPDATE sandbox_jobs SET status='cancelled', "
            "failure_reason='replanned', updated_at=? WHERE id=? AND status IN "
            "('queued','running','waiting_approval')", (now, row["id"]))


def replan_slice(conn: sqlite3.Connection, wid: str, step_id: str,
                 role: str | None, reason: str, *,
                 new_spec: dict | None = None,
                 red_evidence=None,
                 actor_is_human: bool | None = None,
                 approve: bool = False) -> dict:
    """显式 replan：重开切片 scope 与下游，记录原因与新旧 spec 哈希。

    - 复用 reject-to-ancestor 重置，不新增 workflow 状态族；
    - 每片最多 1 次，第二次必须由 human 执行；
    - 删除检查 / 降为 manual / 修改断言但没有新红证据 → 必须由 human 执行；
    - scope 重新提交后引擎会重建锁定集合并重跑红校验。
    """
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _replan_slice_locked(
            conn, wid, step_id, role, reason, new_spec=new_spec,
            red_evidence=red_evidence, approve=approve,
            actor_is_human=(role == "human" if actor_is_human is None
                            else actor_is_human))


def _replan_slice_locked(conn: sqlite3.Connection, wid: str, step_id: str,
                         role: str | None, reason: str, *,
                         new_spec: dict | None,
                         red_evidence,
                         actor_is_human: bool,
                         approve: bool = False) -> dict:
    from . import slice_spec as slice_spec_service

    # approve 只能由 human 身份使用：角色不能通过请求体自批弱化或第二次 replan。
    if approve and not actor_is_human:
        raise PermissionError(
            "replan approve requires human identity; roles cannot self-approve "
            "a weakening or repeated replan")
    reason = (reason or "").strip()
    if not reason:
        raise ConflictError("replan_reason_required: a replan must record a reason")
    conn.execute("BEGIN IMMEDIATE")
    try:
        wf = get_workflow(conn, wid)
        if wf is None:
            raise LookupError(wid)
        if wf.get("status") != "running":
            raise ConflictError(
                f"workflow {wid} is {wf.get('status')}; cannot replan")
        step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
        if step is None:
            raise LookupError(step_id)
        participant_roles = {str(s.get("role_slug") or "") for s in wf["steps"]}
        if role and role != "human" and role not in participant_roles:
            raise PermissionError(
                f"role {role} is not a participant of workflow {wid}")
        scope = _slice_scope_step(wf, step)
        old_spec = scope.get("slice_spec") or {}
        if not isinstance(old_spec, dict) or not old_spec:
            raise ConflictError(
                "replan_no_slice_spec: the slice has no locked spec to replan")
        old_hash = slice_spec_service.spec_hash(old_spec)

        parsed_new = None
        if new_spec is not None:
            try:
                parsed_new = slice_spec_service.parse_slice_spec(new_spec)
            except slice_spec_service.SliceSpecError as exc:
                raise ConflictError(f"slice_spec_invalid: {exc}") from exc
            violations = slice_spec_service.lock_violations(parsed_new)
            if violations:
                raise ConflictError(
                    f"slice_spec_unlockable: {violations[0]}")
        baseline_spec = parsed_new if parsed_new is not None else old_spec
        new_hash = slice_spec_service.spec_hash(baseline_spec)

        covered, job_ids = _red_evidence_ids(
            conn, wf, str(scope.get("slice_key") or ""), red_evidence)
        weakening = (slice_spec_service.weakening_diff(
            old_spec, baseline_spec, red_evidence=covered)
            if parsed_new is not None else [])

        record = _replan_record(scope)
        count = int(record.get("count") or 0)
        now = now_iso()
        if not actor_is_human and (weakening or count >= 1):
            code = ("replan_weakening_requires_human" if weakening
                    else "replan_limit_escalated")
            detail = ("; ".join(f"{w['id']}:{w['type']}" for w in weakening)
                      if weakening else f"slice already replanned {count} time(s)")
            # 事件名刻意不含 replan：检查台账只把真正应用的 replan 计入指标，
            # 升级人工的尝试单独审计，不虚增 replan 次数。
            ledger.emit(
                conn, "slice.human_approval_required",
                task_id=wf["task_id"], workflow_id=wid,
                execution_no=wf.get("execution_no"),
                step_id=scope["step_id"], slice_key=scope.get("slice_key"),
                actor_type="role", actor=role or "unknown",
                payload={"action": "replan", "reason": reason,
                         "old_spec_hash": old_hash,
                         "new_spec_hash": new_hash, "weakening": weakening,
                         "count": count, "code": code},
                mirror_audit=True, audit_action="slice.human_approval_required")
            conn.commit()
            _mention(
                conn, wf["task_id"], "human",
                f"切片「{scope.get('slice_key')}」请求 replan 但需要人工批准"
                f"（{code}）：{reason}。原因：{detail}。")
            raise ConflictError(
                f"{code}: {detail}; a human must approve this replan")

        by_id = {s["step_id"]: s for s in wf["steps"]}
        reset_ids = _reset_rework_steps(
            conn, wf, by_id, {scope["step_id"]}, reason, now)
        # 旧锁定集合与未完成检查作业全部作废；重新提交 scope 时重建并重跑红校验。
        conn.execute(
            "UPDATE workflow_steps SET slice_lock_json=NULL "
            "WHERE workflow_id=? AND step_id=?",
            (wid, scope["step_id"]))
        _cancel_slice_check_jobs(
            conn, wid, scope["step_id"], str(scope.get("slice_key") or ""), now)

        updated = {
            "count": count + 1,
            "reason": reason,
            "old_spec_hash": old_hash,
            "new_spec_hash": new_hash,
            "weakening": weakening,
            "approved_by": "human" if actor_is_human else (role or "system"),
            "human_approved": bool(actor_is_human),
            "red_evidence": job_ids,
            "red_evidence_checks": covered,
            "baseline_spec": baseline_spec,
            "reset_steps": sorted(reset_ids),
            "replanned_at": now,
        }
        conn.execute(
            "UPDATE workflow_steps SET slice_replan_json=?, feedback=?, "
            "updated_at=? WHERE workflow_id=? AND step_id=?",
            (jdumps(updated), reason, now, wid, scope["step_id"]))
        _step_activity(
            conn, wf["task_id"], role or scope.get("role_slug"), scope,
            "replanned", reason,
            extra={"old_spec_hash": old_hash, "new_spec_hash": new_hash,
                   "weakening": weakening, "replan_count": count + 1})
        ledger.emit(
            conn, "slice.replanned",
            task_id=wf["task_id"], workflow_id=wid,
            execution_no=wf.get("execution_no"),
            step_id=scope["step_id"], slice_key=scope.get("slice_key"),
            actor_type="human" if actor_is_human else "role",
            actor=role or "system",
            payload={"reason": reason, "old_spec_hash": old_hash,
                     "new_spec_hash": new_hash, "weakening": weakening,
                     "count": count + 1, "approved_by": updated["approved_by"],
                     "human_approved": bool(actor_is_human),
                     "red_evidence": job_ids},
            mirror_audit=True, audit_action="slice.replanned")
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    return get_workflow(conn, wid)  # type: ignore[return-value]


# 兼容命名：step 维度的 replan 入口。
replan_step = replan_slice


def gates(conn: sqlite3.Connection) -> list[dict]:
    """待人工审核的 gate 步骤（供收件箱聚合）。"""
    rows = conn.execute(
        """SELECT s.workflow_id, s.step_id, s.title, s.role_slug, s.summary, s.updated_at,
                  w.task_id, t.title AS task_title
             FROM workflow_steps s
             JOIN workflows w ON w.id = s.workflow_id
             LEFT JOIN tasks t ON t.id = w.task_id
            WHERE s.gate=1 AND s.status='submitted' AND w.status='running'
              AND COALESCE(s.kind,'task') IN ('task','plan_slices')
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
