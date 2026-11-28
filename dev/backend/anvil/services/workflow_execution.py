"""Workflow 执行层：推进、提交、重试/重启、租约续期与失败收口。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch

from .. import config
from ..util import ConflictError, jdumps, jloads, now_iso
from . import leases, ledger, reviews
from .workflow_common import (
    RUN_ACTIVE, STEP_DONE, _assert_current_run, _assert_delegations_done,
    _step_activity, close_step_delegations, get_workflow,
    workflow_lifecycle_lock,
)
from .workflow_definition import _loop_spec
from .workflow_review import _finalize_if_done


def step_prompt(workflow: dict, step: dict, task: dict | None) -> str:
    """注入给角色的步骤说明（替代任务级协议）。"""
    deps = "、".join(step["depends_on"]) or "无"
    acceptance = "\n".join(f"- {a}" for a in step["acceptance"]) or "-（无）"
    contract = (step.get("contract") or "").strip()
    dep_contracts = []
    dep_results: list[str] = []
    by_id = {x["step_id"]: x for x in workflow.get("steps", [])}
    for d in step["depends_on"]:
        up = by_id.get(d) or {}
        if (up.get("contract") or "").strip():
            dep_contracts.append(f"[{d} {up.get('title','')}] {up['contract'].strip()}")
        if (up.get("summary") or "").strip():
            dep_results.append(
                f"- {d} {up.get('title','')}：{up['summary'].strip()}")
        for ev in up.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            ref = ev.get("ref") or ev.get("label")
            if ref:
                dep_results.append(
                    f"  · 证据 [{ev.get('kind', 'note')}] {ref}")
    dep_results_block = ""
    if dep_results:
        dep_results_block = (
            "\n## 上游结果（必须纳入本次处理）\n"
            + "\n".join(dep_results) + "\n")
    contracts_block = ""
    if contract or dep_contracts:
        contracts_block = "\n契约（必须遵守，违反即视为失败）：\n"
        if dep_contracts:
            contracts_block += "上游契约：\n" + "\n".join(f"- {c}" for c in dep_contracts) + "\n"
        if contract:
            contracts_block += f"本步骤契约：\n{contract}\n"
    title = (task or {}).get("title") or ""
    desc = (task or {}).get("description") or ""
    mention_cmd = (f"anvil task mention {task['id']} --to cindy"
                   if task and task.get("id") else "anvil task mention <任务ID> --to cindy")
    base = (
        f"## 工作流步骤 {step['step_id']}：{step['title']}\n"
        f"所属任务：{title}\n"
        f"工作流 ID：{workflow['id']}\n"
        f"你的角色：{step['role_slug']}\n"
        f"依赖步骤（已完成）：{deps}\n"
        f"验收标准：\n{acceptance}\n\n"
        f"任务背景：\n{desc}\n\n"
        f"步骤说明：\n{step['instructions'] or '（无额外说明，按标题与验收标准执行）'}\n"
        f"{contracts_block}\n"
    )
    feedback = (step.get("feedback") or "").strip()
    feedback_block = ""
    if feedback:
        feedback_block = f"\n## 上次决策/评审反馈（必须处理）\n{feedback}\n"

    evidence = step.get("evidence") or []
    evidence_block = ""
    if step.get("evidence_required") or evidence:
        supplied = "\n".join(
            f"- [{e.get('kind', 'note')}] {e.get('ref') or e.get('label') or ''}"
            for e in evidence if isinstance(e, dict)) or "-（尚未提交）"
        evidence_block = (
            "\n## 证据要求\n"
            "提交时必须附可核验证据（文件/测试/命令输出/产物/URL），不要只写摘要。\n"
            "证据 JSON 为数组："
            "[{\"kind\":\"file|test|command|artifact|url|note\",\"ref\":\"...\","
            "\"label\":\"...\"}]；文件证据只能使用 repo 或 agent home 内的相对路径。\n"
            f"已提交证据：\n{supplied}\n")


    failure_reason = (step.get("failure_reason") or "").strip()
    recovery_block = ""
    if int(step.get("recovery_count") or 0) > 0 and failure_reason:
        recovery_block = (
            f"\n## 自动恢复重跑（第 {step['recovery_count']} 次）\n"
            f"上次失败原因：{failure_reason}\n"
            "先确认该原因是否已消除；环境/进程类问题可直接继续，"
            "任务本身的问题请在 comment 中说明并调整方案。\n")

    slice_key = str(step.get("slice_key") or "").strip()
    if slice_key:
        source = str(step.get("source_step_id") or "").strip()
        if not source:
            source = ".".join(step["step_id"].split(".")[:-2])
        slice_prefix = f"{source}.{slice_key}" if source else slice_key
        slice_label = slice_key
    elif step.get("kind") != "external" and "." in step["step_id"]:
        slice_prefix = step["step_id"].split(".", 1)[0]
        slice_label = slice_prefix
    else:
        slice_prefix = ""
        slice_label = ""
    loop_block = ""
    if slice_prefix:
        loop_block = (
            f"\n## 所属切片 {slice_label} 的闭环\n"
            "scope → execute → verify → fix → verify2 → decide。"
            "你只负责其中一步；整片步骤完成后再由决策角色评估，approve 后才进入下一片。"
            "如果决策打回，fix/verify2 会带着反馈重跑。\n"
        )

    if step.get("kind") == "plan_slices":
        loop = (workflow.get("definition") or {}).get("loop") or {}
        phase_lines: list[str] = []
        for phase, label in (
            ("scope", "切片范围（可选）"), ("execute", "执行"), ("verify", "验证"),
            ("fix", "修复"), ("verify2", "复验"), ("decide", "决策"),
        ):
            spec = _loop_spec(loop, phase)
            if not spec and phase == "scope":
                continue
            role = str(spec.get("role") or ("<由切片指定>" if phase != "decide" else ""))
            title = str(spec.get("title") or "").strip()
            phase_lines.append(f"- {label}: 角色 {role}" + (f" · {title}" if title else ""))
        loop_block = (
            "\n## 切片计划职责\n"
            "你负责基于对项目的实际理解（目录结构、模块边界、依赖关系、风险面）把项目切成"
            "若干可独立闭环的切片。先盘点项目并用 `anvil task comment` 发布盘点结论；"
            "本步骤只做切片，不执行切片内的具体工作。\n"
            + ("已定义的闭环模板：\n" + "\n".join(phase_lines) + "\n" if phase_lines else "")
            + "切片写好后执行：\n"
            f"`anvil step plan {workflow['id']} {step['step_id']} --file <slices.json>`\n"
            "JSON 格式：{\"rationale\":\"切片依据\",\"slices\":[{\"id\":\"auth\","
            "\"title\":\"认证与授权模块\",\"scope_paths\":[\"src/auth/**\"],"
            "\"objective\":\"...\",\"instructions\":\"...\",\"acceptance\":[...],"
            "\"execute_steps\":[{\"id\":\"static\","
            "\"title\":\"静态审计\",\"instructions\":\"...\",\"acceptance\":[...]}]}]}。\n"
            "每提交一个 slice 就代表一轮完整 loop（scope → execute → verify → fix → verify2 → decide）；"
            "slices 的数量就是你反馈的 loop 轮数。\n"
            "切片只描述工作范围、粒度与轮数，**不得指定角色或 loop 阶段**"
            "（不要写 execute_role/verify_role/fix_role/verify2_role/decision_role/lead_role，"
            "也不要给 execute_steps 写 role）；角色由 Cindy 在 workflow 的 loop 模板中定义，"
            "服务端会套用该模板。\n"
            f"如果你认为 loop 结构（阶段/角色/轮次）需要调整，用 `{mention_cmd} \"具体诉求\"` "
            "反馈，然后停止本轮等待 Cindy 的新 workflow；不要在旧模板下继续提交切片计划。\n"
            "切片 id 只能使用字母/数字/_/-，且不得重复。服务端会为每片自动生成 "
            "execute → verify → fix → verify2 → decide 闭环；下一片在上一片决策通过后开始。\n"
            "如果项目为空或没有可审计范围，可提交空 slices 并在 rationale 中说明。\n"
            "不要调用 `anvil step submit` / `anvil task submit` / `anvil task claim`。\n"
        )
        return base + loop_block + feedback_block + recovery_block

    if step.get("kind") == "decision":
        targets: list[str] = []
        for tid in (step.get("on_reject") or []):
            t = by_id.get(tid) or {}
            lines = [
                f"- {tid} {t.get('title', '')}（执行角色 {t.get('role_slug', '')}，"
                f"当前状态 {t.get('status', '')}）"
            ]
            if t.get("summary"):
                lines.append(f"  summary: {t['summary']}")
            if t.get("acceptance"):
                lines.append("  acceptance: " + "；".join(t["acceptance"]))
            if t.get("contract"):
                lines.append(f"  contract: {t['contract']}")
            targets.append("\n".join(lines))
        target_block = "\n".join(targets) or "-（无）"
        dep_ids = list(step["depends_on"])
        if slice_prefix:
            # 决策要评估整片，而不只是直接依赖 verify2：把同前缀的步骤全部列出。
            dep_ids = [
                x["step_id"] for x in workflow.get("steps", [])
                if x["step_id"].startswith(slice_prefix + ".")
                and x["step_id"] != step["step_id"]
            ]
        dep_outputs: list[str] = []
        for did in dep_ids:
            up = by_id.get(did) or {}
            lines = [
                f"- {did} {up.get('title', '')}（{up.get('role_slug', '')}，"
                f"{up.get('status', '')}）"
            ]
            if up.get("summary"):
                lines.append(f"  summary: {up['summary']}")
            if up.get("acceptance"):
                lines.append("  acceptance: " + "；".join(up["acceptance"]))
            dep_outputs.append("\n".join(lines))
        dep_output_block = "\n".join(dep_outputs) or "-（无）"
        return (
            base
            + loop_block
            + evidence_block
            + "\n## 本切片依赖步骤产出（决策依据）\n"
            + dep_output_block + "\n"
            + "\n## 决策职责\n"
            "你是本切片的决策角色。只审阅依赖步骤的产出、证据与验收标准，"
            "不要亲自实现具体工作。决策前逐条核对验收标准；通过时写明放行依据与剩余风险，"
            "打回时逐条列出必须修改的问题、验收口径与复验要求。\n"
            "\n## 逐项验收判定\n"
            "引擎已在 verify 阶段重跑锁定检查并写入绿跑证据（验收证据只由引擎产生）。"
            "验收项 verdict 为 met / unmet / n.a.；引擎检查失败的项是 unmet，"
            "无法判定（harness 错误、未跑完）的项是 unknown。存在 unmet 或 unknown 时不能通过。"
            "manual 项必须由你判定，用 --verdicts-file 提交："
            "[{\"id\":\"m1\",\"status\":\"met|unmet|n.a.\",\"evidence_refs\":[\"...\"]}]；"
            "漏交 manual 项会保持 unknown 并阻止通过；引擎 verdict 不可覆盖。\n"
            f"重做次数：{step.get('rework_count', 0)} / {step.get('max_rework', 0)}\n"
            f"可打回的目标步骤（on_reject）：\n{target_block}\n"
            f"决策权限：{reviews.authority_of(step)}"
            "（ai=AI 可决定；human=等待自然人；joint=AI 只能建议通过，自然人确认）\n"
            + feedback_block
            + recovery_block
            + "\n完成后执行：\n"
            f"- 通过：`anvil step decide {workflow['id']} {step['step_id']} "
            "--approve --feedback \"结论\" --verdicts-file verdicts.json`\n"
            f"- 打回：`anvil step decide {workflow['id']} {step['step_id']} "
            "--reject --feedback \"具体问题与修改要求\"`\n"
            f"- 证据不足/风险超限：`anvil step escalate {workflow['id']} {step['step_id']} "
            "--reason \"原因\"`\n"
            "不要调用 `anvil step submit` / `anvil task submit` / `anvil task claim`。\n"
        )

    is_scope = bool(step.get("slice_key")) and step["step_id"].endswith(".scope")
    slice_spec_block = ""
    if is_scope:
        slice_spec_block = (
            "\n## 切片 spec（scope 提交必填）\n"
            "scope 阶段用结构化 spec 固化本切片的执行契约，供引擎在 base 上跑红校验。\n"
            "JSON 字段：acceptance（数组，每项 {id?, kind, criterion, verification|evidence}，"
            "kind 只能是 test/command/file_assert/manual）、seam、out_of_scope、contract、"
            "deliverables、stubs（[{path, signatures:[...]}]）。\n"
            "test 项用结构化验证方式：{\"pytest\":{\"paths\":[\"tests/...\"],"
            "\"args\":[\"-q\"],\"plugins\":[]}}；绿地切片先写抛 NotImplementedError 的 "
            "接口 stub，再写测试。"
            "command 项用 {\"cmd\":\"python -m app --check\",\"exit_code\":1}，"
            "必须声明断言失败退出码，否则不可锁定；"
            "file_assert 项用 {\"path\":\"reports/out.md\",\"contains\":\"PASS\"}，"
            "由引擎原生判定文件缺失/内容不符（红）与匹配（绿）；"
            "manual 项用 {\"judge\":\"human|decide\",\"method\":\"...\"}，"
            "不由引擎执行，留给 decide 阶段判定。"
            "未知字段与非法 kind 会被服务端拒绝。\n")
    submit_cmd = (f"anvil step submit {workflow['id']} {step['step_id']} "
                  "--summary \"完成摘要\"")
    if is_scope:
        submit_cmd = (f"anvil step submit {workflow['id']} {step['step_id']} "
                      "--summary \"完成摘要\" --slice-spec-file slice-spec.json")
    if step.get("evidence_required"):
        submit_cmd += " --evidence-file evidence.json"
    submit_cmd += ' --changed-paths "src/a.py,src/b.py"'
    verify_block = ""
    if (str(step.get("slice_key") or "").strip()
            and step["step_id"].endswith((".verify", ".verify2"))):
        verify_block = (
            "\n## 验证职责\n"
            "你只整理与复核验收证据，不要提交“通过/已通过”结论；引擎会在本步骤提交后"
            "重跑锁定检查，逐项判定由 decide 记录。报告独立复核的证据、问题与无法复现项即可。\n")
    return (
        base
        + loop_block
        + evidence_block
        + dep_results_block
        + slice_spec_block
        + verify_block
        + feedback_block
        + recovery_block
        + f"\n完成后必须执行：`{submit_cmd}`。\n"
        "不要调用 `anvil task submit` / `anvil task claim`。\n"
    )


def explain_dispatch(conn: sqlite3.Connection, wf: dict) -> dict:
    """诊断 pending 步骤为何没有被 advance 激活（只读，不改变状态）。"""
    from . import governance

    steps = wf.get("steps") or []
    by_id = {s["step_id"]: s for s in steps}
    pending: list[dict] = []
    if wf.get("status") != "running":
        return {"workflow_status": wf.get("status"), "pending": pending,
                "note": "workflow is not running; advance will not dispatch"}
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
    now = now_iso()
    for s in steps:
        if s.get("status") != "pending":
            continue
        reasons: list[str] = []
        retry_at = s.get("next_retry_at")
        if retry_at and str(retry_at) > now:
            reasons.append(f"retry_backoff_until={retry_at}")
        for dep in s.get("depends_on") or []:
            up = by_id.get(dep)
            if up is None:
                reasons.append(f"dependency_missing={dep}")
            elif up.get("status") not in STEP_DONE:
                reasons.append(f"dependency={dep}:{up.get('status')}")
        if s.get("kind") == "decision" and reviews.authority_of(s) == "human":
            reasons.append("human_authority_decision=awaiting_human")
        role = s.get("role_slug")
        if not role:
            reasons.append("no_role")
        else:
            busy = conn.execute(
                "SELECT step_id, status FROM workflow_steps WHERE workflow_id=? "
                "AND role_slug=? AND status IN ('running','submitted') "
                "AND step_id<>? LIMIT 1",
                (wf["id"], role, s["step_id"])).fetchone()
            if busy:
                reasons.append(f"role_busy_step={busy['step_id']}:{busy['status']}")
            run = conn.execute(
                "SELECT id, status, task_id, step_id FROM runs WHERE role_slug=? "
                "AND status IN ('created','projecting','running') "
                "ORDER BY started_at DESC LIMIT 1", (role,)).fetchone()
            if run:
                reasons.append(
                    f"active_run={run['id']}:{run['status']}"
                    f":task={run['task_id'] or '-'}:step={run['step_id'] or '-'}")
            if task is not None:
                try:
                    governance.check_claim_allowed(conn, dict(task), role)
                except ConflictError as exc:
                    reasons.append(f"governance={exc}")
            lease = conn.execute(
                "SELECT id, holder, expires_at FROM leases WHERE resource_type='step' "
                "AND resource_key=? AND status='active'",
                (f"{wf['id']}:{s['step_id']}",)).fetchone()
            if lease:
                reasons.append(
                    f"lease_held={lease['id']}:holder={lease['holder'] or '-'}"
                    f":expires={lease['expires_at']}")
        pending.append({
            "step_id": s["step_id"], "role_slug": role,
            "reasons": reasons or ["ready"],
        })
    return {"workflow_status": wf.get("status"), "pending": pending}


def advance(conn: sqlite3.Connection, wid: str, starter=None) -> dict:
    """串行化工作流推进；跨进程用 task 生命周期锁，抢不到锁则本轮跳过。"""
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(conn, task_id, required=False) as acquired:
        if not acquired:
            return {
                "started": [], "waiting_human": [], "held": [], "busy": True,
                "done": bool(wf and wf["status"] == "done"),
            }
        result = _advance_locked(conn, wid, starter)
        if isinstance(result, dict):
            result.setdefault("busy", False)
        return result


def _advance_locked(conn: sqlite3.Connection, wid: str, starter=None) -> dict:
    """推进工作流：为依赖满足且角色空闲的步骤创建 run。

    starter(role, task_id, prompt, step) -> run dict（由 scheduler/API 注入）。
    同一工作流内同角色一次只有 running/submitted 步骤；全局该角色已有活跃 run 时也跳过。
    """
    wf = get_workflow(conn, wid)
    if not wf or wf["status"] != "running":
        return {"started": [], "done": wf is not None and wf["status"] == "done"}
    steps = wf["steps"]
    by_id = {s["step_id"]: s for s in steps}
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
    started: list[str] = []
    waiting_human: list[str] = []
    now_dt = datetime.now(timezone.utc)
    for s in steps:
        if s["status"] != "pending":
            continue
        # 自动恢复的指数退避：未到 next_retry_at 前不重新拉起。
        retry_at = s.get("next_retry_at")
        if retry_at:
            try:
                when = datetime.fromisoformat(retry_at)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when > now_dt:
                    continue
            except (ValueError, TypeError):
                pass
        if any(by_id.get(d, {}).get("status") not in STEP_DONE for d in s["depends_on"]):
            continue
        # human authority decision: no AI decider run at all — dependencies are
        # enough to open the packet and put it in the human inbox.
        if s.get("kind") == "decision" and reviews.authority_of(s) == "human":
            now = now_iso()
            conn.execute(
                "UPDATE workflow_steps SET status='submitted', submitted_at=?, updated_at=?, "
                "summary=COALESCE(summary, ?) WHERE workflow_id=? AND step_id=?",
                (now, now, "等待人工决策", wid, s["step_id"]))
            reviews.ensure_packet(
                conn, wf, {**s, "summary": None, "status": "submitted"},
                created_by="system", commit=False)
            _step_activity(conn, wf["task_id"], None, s, "submitted", "等待人工决策")
            waiting_human.append(s["step_id"])
            continue
        role = s["role_slug"]
        if not role:
            continue
        from . import roles as roles_service
        role_row = roles_service.get(conn, role)
        if role_row is not None and not role_row.get("enabled", True):
            fail_now = now_iso()
            conn.execute(
                "UPDATE workflow_steps SET status='failed', "
                "failure_reason='role_unavailable', summary=?, ended_at=?, "
                "updated_at=? WHERE workflow_id=? AND step_id=? AND status='pending'",
                (f"角色 {role} 已禁用，无法派发", fail_now, fail_now, wid,
                 s["step_id"]))
            _step_activity(
                conn, wf["task_id"], role, s, "failed",
                f"角色 {role} 已禁用，无法派发；启用角色后人工重试该步骤。")
            ledger.emit_activity(
                conn, task_id=wf["task_id"],
                role_slug="workflow",
                kind="comment",
                payload=jdumps({"body": f"步骤「{s.get('title') or s['step_id']}」的角色 "
                                 f"{role} 已禁用，步骤已标记 failed；"
                                 "请启用角色后在任务详情重试。"}),
                now=fail_now)
            continue
        if conn.execute(
            "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND role_slug=? "
            "AND status IN ('running','submitted')", (wid, role)
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM runs WHERE role_slug=? AND status IN (?,?,?)",
            (role, *RUN_ACTIVE),
        ).fetchone():
            continue
        if task:
            from . import governance
            try:
                governance.check_claim_allowed(conn, dict(task), role)
            except ConflictError as exc:
                ledger.emit(
                    conn, "dispatch.deferred", task_id=wf["task_id"],
                    workflow_id=wid, execution_no=wf.get("execution_no"),
                    step_id=s["step_id"], actor_type="system", actor="scheduler",
                    payload={"role": role, "reason": str(exc)},
                )
                continue
        try:
            lease = leases.acquire(
                conn, "step", f"{wid}:{s['step_id']}", holder_type="role",
                holder=role, meta={"task_id": wf["task_id"], "workflow_id": wid,
                                   "step_id": s["step_id"]})
        except ConflictError:
            continue
        now = now_iso()
        claimed = conn.execute(
            "UPDATE workflow_steps SET status='running', run_id=NULL, started_at=?, "
            "ended_at=NULL, summary=NULL, failure_reason=NULL, next_retry_at=NULL, "
            "lease_id=?, fence_token=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=? AND status='pending'",
            (now, lease["id"], int(lease["fence"]), now, wid, s["step_id"]))
        if claimed.rowcount != 1:
            leases.release(conn, lease["id"])
            continue
        prompt = step_prompt(wf, s, dict(task) if task else None)
        run_id = None
        try:
            if starter is not None:
                run = starter(role, wf["task_id"], prompt, s)
                run_id = (run or {}).get("id")
        except BaseException:
            leases.release(conn, lease["id"])
            conn.execute(
                "UPDATE workflow_steps SET status='pending', lease_id=NULL, fence_token=0, "
                "started_at=NULL, updated_at=? WHERE workflow_id=? AND step_id=?",
                (now_iso(), wid, s["step_id"]))
            conn.commit()
            raise
        if run_id:
            conn.execute("UPDATE workflow_steps SET run_id=? WHERE workflow_id=? AND step_id=?",
                         (run_id, wid, s["step_id"]))
            leases.bind_run(conn, lease["id"], run_id)
        _step_activity(
            conn, wf["task_id"], role, s, "running",
            extra={"rework_count": int(s.get("rework_count") or 0),
                   "fence": int(lease["fence"])}
            if int(s.get("rework_count") or 0) else {"fence": int(lease["fence"])},
        )
        started.append(s["step_id"])
    conn.commit()
    _finalize_if_done(conn, wid)
    wf = get_workflow(conn, wid)
    all_done = wf is not None and wf["status"] == "done"
    return {"started": started, "waiting_human": waiting_human, "done": all_done}


def resume_runs_for_task(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """重开任务后需要立即唤醒的步骤 run。

    包含 suspended，以及常驻会话仍标记 running 但可能已空闲的 run；
    调用方需进一步检查进程 busy 状态，避免打断正在工作的角色。
    """
    rows = conn.execute(
        """SELECT s.run_id, s.step_id, s.role_slug, w.id AS workflow_id,
                  r.status AS run_status, r.native_session_id
             FROM workflows w
             JOIN workflow_steps s ON s.workflow_id = w.id
             JOIN runs r ON r.id = s.run_id
            WHERE w.task_id=? AND w.status='running' AND s.status='running'
              AND (r.status='suspended'
                   OR (r.status='running' AND r.native_session_id LIKE 'rs:%'))""",
        (task_id,)).fetchall()
    return [dict(r) for r in rows]


def renew_step_lease_for_run(conn: sqlite3.Connection, run_id: str | None,
                             ttl_seconds: float | None = None) -> bool:
    """任务心跳顺带续租当前步骤 lease；run/step 不匹配或已非 running 时不动。"""
    if not run_id:
        return False
    row = conn.execute(
        "SELECT workflow_id, step_id FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or not row["workflow_id"] or not row["step_id"]:
        return False
    step = conn.execute(
        "SELECT status, run_id, lease_id FROM workflow_steps "
        "WHERE workflow_id=? AND step_id=?",
        (row["workflow_id"], row["step_id"])).fetchone()
    if step is None or step["status"] != "running" \
            or str(step["run_id"] or "") != str(run_id):
        return False
    try:
        leases.renew(conn, step["lease_id"], ttl_seconds=ttl_seconds)
    except ConflictError:
        return False
    return True


def _slice_execute_scope_paths(conn: sqlite3.Connection, wf: dict,
                               step: dict) -> list[str]:
    """本切片 execute 可修改的路径：运行期切片取 plan 的 scope_paths；静态切片取自身。"""
    paths: list[str] = []
    source = step.get("source_step_id")
    slice_key = step.get("slice_key")
    if source:
        row = conn.execute(
            "SELECT plan_json FROM workflow_steps WHERE workflow_id=? AND step_id=?",
            (wf["id"], source)).fetchone()
        if row is not None and row["plan_json"]:
            plan = jloads(row["plan_json"], {})
            for raw_slice in plan.get("slices") or []:
                if (isinstance(raw_slice, dict)
                        and str(raw_slice.get("id")) == str(slice_key)):
                    paths.extend(str(p) for p in raw_slice.get("scope_paths") or [])
                    break
    paths.extend(str(p) for p in step.get("scope_paths") or [])
    return paths


def _is_dependency_path(path: str) -> bool:
    from . import env_integrity
    return env_integrity.is_dependency_file(path)


def _slice_lock_for_step(conn: sqlite3.Connection, wid: str,
                         step: dict) -> dict | None:
    """同一 slice 的 scope 步骤持久化的锁定集合。"""
    slice_key = str(step.get("slice_key") or "").strip()
    if not slice_key:
        return None
    row = conn.execute(
        "SELECT slice_lock_json FROM workflow_steps WHERE workflow_id=? "
        "AND slice_key=? AND step_id LIKE '%.scope' "
        "AND slice_lock_json IS NOT NULL ORDER BY rowid LIMIT 1",
        (wid, slice_key)).fetchone()
    if row is None or not row["slice_lock_json"]:
        return None
    return jloads(row["slice_lock_json"], {}) or None


def _slice_final_verify(conn: sqlite3.Connection, wid: str, step: dict) -> bool:
    """本步骤是否是切片 decide 之前的最终验证门（verify / verify2）。

    绿跑必须覆盖 decide 将要判定的代码状态；fix 之后的 verify2 才是最终门，
    light 强度没有 fix/verify2 时 decide 直接依赖 verify。
    """
    slice_key = str(step.get("slice_key") or "").strip()
    if not slice_key or step.get("kind") == "decision":
        return False
    row = conn.execute(
        "SELECT depends_on_json FROM workflow_steps WHERE workflow_id=? "
        "AND slice_key=? AND kind='decision' ORDER BY rowid LIMIT 1",
        (wid, slice_key)).fetchone()
    if row is None:
        return False
    return step["step_id"] in set(jloads(row["depends_on_json"], []))


def _maybe_create_green_checks(conn: sqlite3.Connection, wf: dict,
                               step: dict, role: str | None) -> None:
    """切片最终验证门提交后，由引擎在 final 代码上重跑锁定检查。"""
    if not _slice_final_verify(conn, wf["id"], step):
        return
    from . import lock_set
    from . import sandbox as sandbox_service
    slice_key = str(step.get("slice_key") or "").strip()
    found = lock_set.for_slice(conn, wf["id"], slice_key)
    if found is None:
        return
    lock, scope_step_id = found
    row = conn.execute(
        "SELECT slice_spec_json FROM workflow_steps WHERE workflow_id=? AND step_id=?",
        (wf["id"], scope_step_id)).fetchone()
    if row is None or not row["slice_spec_json"]:
        return
    spec = jloads(row["slice_spec_json"], {})
    if not isinstance(spec, dict) or not spec:
        return
    sandbox_service.create_slice_green_checks(
        conn, task_id=str(wf["task_id"]), workflow_id=wf["id"],
        slice_key=slice_key, scope_step_id=scope_step_id, spec=spec,
        lock=lock, actor_role=role)


def submit_step(conn: sqlite3.Connection, wid: str, step_id: str, role: str,
                summary: str | None, run_id: str | None = None,
                evidence: list | None = None,
                changed_paths: list | None = None,
                slice_spec: dict | None = None) -> dict:
    """角色提交当前步骤（anvil step submit）。gate 步骤进入人工审核。"""
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    # 与 decide/review/terminate 共用 task 生命周期锁：提交与决策/终止不能并发
    # 把 approved 覆盖回 submitted，也不能在 terminated workflow 上写提交。
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _submit_step_locked(
            conn, wid, step_id, role, summary, run_id, evidence, changed_paths,
            slice_spec)


def _submit_step_locked(conn: sqlite3.Connection, wid: str, step_id: str, role: str,
                        summary: str | None, run_id: str | None = None,
                        evidence: list | None = None,
                        changed_paths: list | None = None,
                        slice_spec: dict | None = None) -> dict:
    from . import runs as runs_service

    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    if wf.get("status") != "running":
        raise ConflictError(
            f"workflow {wid} is {wf.get('status')}; cannot submit a step")
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None:
        raise LookupError(step_id)
    _assert_current_run(step, run_id)
    _assert_delegations_done(conn, wf, step_id)
    leases.assert_step(conn, step, run_id)
    if step["role_slug"] != role:
        raise ConflictError(f"step {step_id} belongs to {step['role_slug']}, not {role}")
    if step["status"] != "running":
        raise ConflictError(f"step {step_id} is {step['status']}, expected running")
    if step.get("kind") == "decision":
        raise ConflictError(
            f"step {step_id} is a decision step; use `anvil step decide`")
    if step.get("kind") == "plan_slices":
        raise ConflictError(
            f"step {step_id} is a plan_slices step; use `anvil step plan`")
    try:
        normalized = reviews.normalize_evidence(evidence) if evidence is not None else []
    except reviews.EvidenceError as exc:
        raise ConflictError(f"evidence_invalid: {exc}")
    if step.get("evidence_required") and not normalized:
        raise ConflictError(
            f"evidence_required: step {step_id} requires at least one evidence item")
    parsed_spec = None
    slice_spec_json = None
    if slice_spec is not None:
        if not str(step.get("slice_key") or "").strip() or not step_id.endswith(".scope"):
            raise ConflictError(
                "slice_spec is only accepted when submitting a slice scope step")
        from . import slice_spec as slice_spec_service
        try:
            parsed_spec = slice_spec_service.parse_slice_spec(slice_spec)
        except slice_spec_service.SliceSpecError as exc:
            raise ConflictError(f"slice_spec_invalid: {exc}") from exc
        # 锁定前校验：command 未声明断言失败退出码的 spec 不可锁定。
        violations = slice_spec_service.lock_violations(parsed_spec)
        if violations:
            raise ConflictError(f"slice_spec_unlockable: {violations[0]}")
        slice_spec_json = jdumps(parsed_spec)
    # scope 锁定时固定锁定集合并校验与 execute scope_paths 不相交；冲突不落库。
    slice_lock_json = None
    if parsed_spec is not None:
        from . import lock_set as lock_set_service
        workspace = config.TASK_WORKSPACES_DIR / str(wf["task_id"])
        execute_paths = _slice_execute_scope_paths(conn, wf, step)
        base_snapshot = (workspace / ".anvil" / "slice-base"
                         / str(step.get("slice_key") or ""))
        try:
            lock = lock_set_service.build(
                workspace, parsed_spec, execute_paths=execute_paths,
                base_snapshot=base_snapshot)
        except lock_set_service.LockConflict as exc:
            raise ConflictError(f"lock_scope_conflict: {exc}") from exc
        # manual 占比在 scope 锁定重算；超过 30% 时该片 decide authority 切 human。
        from . import slice_verdicts
        manual = slice_verdicts.manual_ratio(parsed_spec)
        lock["manual"] = manual
        if manual["authority"] == "human":
            conn.execute(
                "UPDATE workflow_steps SET decision_authority='human', updated_at=? "
                "WHERE workflow_id=? AND slice_key=? AND kind='decision'",
                (now_iso(), wid, str(step.get("slice_key") or "")))
        slice_lock_json = jdumps(lock)
    changed = [leases.normalize_path(p) for p in (changed_paths or [])]
    if changed:
        conflicts = leases.check_paths(conn, wf["task_id"], changed, holder=role)
        if conflicts:
            raise ConflictError(f"path_conflict: {conflicts[0]}")
        scope = step.get("scope_paths") or []
        outside = [p for p in changed
                   if scope and not any(fnmatch(p, pattern) for pattern in scope)]
        if outside:
            raise ConflictError(f"scope_violation: {outside[0]} is outside step scope")
        # execute / fix 不得修改锁定集合：测试文件、运行器配置、stub 签名、
        # 检查目录内新增文件。scope 自身正在写这些文件，跳过。
        if not step_id.endswith(".scope"):
            lock = _slice_lock_for_step(conn, wid, step)
            if lock:
                from . import lock_set as lock_set_service
                workspace = config.TASK_WORKSPACES_DIR / str(wf["task_id"])
                try:
                    lock_set_service.enforce_changes(lock, workspace, changed)
                except lock_set_service.LockConflict as exc:
                    raise ConflictError(f"lock_violation: {exc}") from exc
    # v1.62 #13：提交时记录本次改动的 git numstat（不信任自报数量）。
    from . import run_caps
    task_row = conn.execute(
        "SELECT base_branch FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
    diff_stats = run_caps.collect_numstat(
        conn, wf["task_id"], role, changed, run_id=step.get("run_id"),
        base_branch=(task_row["base_branch"] if task_row else None))
    now = now_iso()
    # CAS：只有仍处于 running 的步骤才能提交，避免与 fail/terminate/rework 竞态
    # 把已结束步骤写回 submitted（形成 orphan run / 重复 packet）。
    cur = conn.execute(
        "UPDATE workflow_steps SET status='submitted', summary=?, evidence_json=?, "
        "slice_spec_json=COALESCE(?, slice_spec_json), "
        "slice_lock_json=COALESCE(?, slice_lock_json), diff_stats_json=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=? AND status='running'",
        (summary, jdumps(normalized or step.get("evidence") or []),
         slice_spec_json, slice_lock_json, jdumps(diff_stats), now, wid, step_id),
    )
    if cur.rowcount != 1:
        raise ConflictError(
            f"step {step_id} state changed while submitting; refresh and retry")
    if step["run_id"]:
        runs_service.set_status(
            conn, step["run_id"], "completed", ended_at=now,
            from_statuses=("created", "projecting", "running", "suspended"))
    if step["gate"]:
        # 审核门条目由收件箱聚合（kind=gate），这里只记时间线，不重复发 mention
        _step_activity(conn, wf["task_id"], role, step, "submitted", summary)
        conn.execute("UPDATE workflows SET updated_at=? WHERE id=?", (now, wid))
        # 运行期可能已生成过空包；提交时必须用最终证据强制刷新。
        reviews.ensure_packet(conn, wf, {
            **step, "summary": summary, "evidence": normalized or step.get("evidence") or [],
            "diff_stats": diff_stats, "status": "submitted"},
            created_by=role, force=True)
    else:
        conn.execute(
            "UPDATE workflow_steps SET status='approved', ended_at=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?", (now, now, wid, step_id))
        _step_activity(conn, wf["task_id"], role, step, "approved", summary)
    # 运行上限超限：升级人工（收件箱 + 评审包 risk），不硬失败、不阻塞推进。
    cap_report = run_caps.evaluate(conn, wf, {**step, "diff_stats": diff_stats})
    run_caps.escalate(conn, wf, {**step, "diff_stats": diff_stats}, cap_report)
    leases.release(conn, step.get("lease_id"), commit=False)
    conn.commit()
    if parsed_spec is not None:
        # 引擎在 base 上按切片 spec 的 test 项创建红跑作业；证据只由引擎写入。
        from . import sandbox as sandbox_service
        sandbox_service.create_slice_checks(
            conn, task_id=wf["task_id"], step_id=step_id,
            slice_key=str(step.get("slice_key") or ""), spec=parsed_spec,
            workflow_id=wid, actor_role=role, actor_is_human=False)
    elif (str(step.get("slice_key") or "").strip()
          and any(_is_dependency_path(p) for p in changed)):
        # 执行/修复改动了依赖声明：记录 delta；测试闭包变化升级人工。
        from . import sandbox as sandbox_service
        sandbox_service.evaluate_dependencies(
            conn, task_id=wf["task_id"], workflow_id=wid,
            slice_key=str(step.get("slice_key") or ""), actor_role=role)
    # 最终验证门提交：引擎在 final 代码上重跑锁定检查（绿跑）。
    _maybe_create_green_checks(conn, wf, step, role)
    _finalize_if_done(conn, wid)
    return get_workflow(conn, wid)  # type: ignore[return-value]


def retry_step(conn: sqlite3.Connection, wid: str, step_id: str) -> dict:
    """人工重试失败/驳回的步骤：重置为 pending，等待调度。"""
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _retry_step_locked(conn, wid, step_id)


def _retry_step_locked(conn: sqlite3.Connection, wid: str, step_id: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_iso()
        wf = get_workflow(conn, wid)
        if wf is None:
            raise LookupError(wid)
        step = next((s for s in wf.get("steps") or [] if s["step_id"] == step_id), None)
        if step is None:
            raise LookupError(step_id)
        if step["status"] not in ("failed", "rejected"):
            raise ConflictError(
                f"step {step_id} is {step['status']}; "
                "only failed/rejected steps can be retried")
        if step and step.get("kind") == "plan_slices":
            expanded = conn.execute(
                "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND source_step_id=? "
                "LIMIT 1", (wid, step_id)).fetchone()
            if expanded:
                raise ConflictError(
                    "slice plan already expanded; create or re-dispatch the workflow "
                    "to re-slice")
        # rework 耗尽后重试 decision：同时重置 on_reject 目标，否则会拿旧产出
        # 再次决策、没有真正返工。
        if step.get("kind") == "decision" \
                and step.get("failure_reason") == "rework_exhausted":
            by_id = {s["step_id"]: s for s in wf["steps"]}
            for tid in step.get("on_reject") or []:
                target = by_id.get(tid) or {}
                if (target.get("status") or "") == "running":
                    raise ConflictError(
                        f"rework target {tid} is running; wait for it to finish")
                conn.execute(
                    "UPDATE workflow_steps SET status='pending', run_id=NULL, "
                    "started_at=NULL, ended_at=NULL, summary=NULL, failure_reason=NULL, "
                    "recovery_count=0, next_retry_at=NULL, nudge_count=0, "
                    "last_nudge_at=NULL, lease_id=NULL, fence_token=0, updated_at=? "
                    "WHERE workflow_id=? AND step_id=?",
                    (now, wid, tid))
                conn.execute(
                    "UPDATE gate_packets SET status='superseded' "
                    "WHERE workflow_id=? AND step_id=? "
                    "AND status IN ('pending_human','pending_ai')",
                    (wid, tid))
                leases.release(conn, target.get("lease_id"), commit=False)
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "ended_at=NULL, summary=NULL, recovery_count=0, next_retry_at=NULL, "
            "nudge_count=0, last_nudge_at=NULL, lease_id=NULL, fence_token=0, "
            "updated_at=? WHERE workflow_id=? AND step_id=?",
            (now, wid, step_id))
        if step and step.get("kind") == "decision":
            conn.execute(
                "UPDATE workflow_steps SET rework_count=0 "
                "WHERE workflow_id=? AND step_id=?", (wid, step_id))
        if step:
            leases.release(conn, step.get("lease_id"), commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return get_workflow(conn, wid)  # type: ignore[return-value]


def check_step_run_restart(conn: sqlite3.Connection, wid: str, step_id: str,
                           run_id: str) -> bool:
    """校验失败 run 能否重新绑定回 workflow step；返回是否需要 rebind。

    这是“重启进程”按钮和 workflow step 状态机之间的桥：失败 run 的 step 已被
    fail_step 重置为 pending/failed，单纯把 run 改回 created 会被 scheduler 当作
    orphaned_step_run 再次回收。必须先恢复 step 的 running/lease/run_id。
    """
    wf = get_workflow(conn, wid)
    if wf is None:
        raise ConflictError(f"workflow not found: {wid}")
    if wf["status"] != "running":
        raise ConflictError(f"workflow {wid} is {wf['status']}; cannot restart its step run")
    task = conn.execute(
        "SELECT status, archived_at FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
    if task is None or task["status"] in ("done", "closed") or task["archived_at"]:
        raise ConflictError("task is terminal; cannot restart its step run")
    step = next((s for s in wf.get("steps") or [] if s["step_id"] == step_id), None)
    if step is None:
        raise ConflictError(f"step not found: {step_id}")
    status = step["status"]
    if status == "running":
        if str(step.get("run_id") or "") != str(run_id):
            raise ConflictError(
                f"step {step_id} is running with another run "
                f"({step.get('run_id')}); retry the step instead")
        return False
    if status in ("failed", "rejected", "pending"):
        other = step.get("run_id")
        if status == "pending" and other and str(other) != str(run_id):
            row = conn.execute("SELECT status FROM runs WHERE id=?", (other,)).fetchone()
            if row is not None and row["status"] not in ("completed", "failed", "abandoned"):
                raise ConflictError(
                    f"step {step_id} is already being retried by run {other}")
        return True
    raise ConflictError(
        f"step {step_id} is {status}; only running/failed/rejected/pending "
        "step runs can be restarted")


def rebind_run_to_step(conn: sqlite3.Connection, wid: str, step_id: str,
                       run_id: str) -> dict:
    """把 manual restart 的 run 重新绑定为 step 的 running run（含新 lease/fence）。"""
    if not check_step_run_restart(conn, wid, step_id, run_id):
        return get_workflow(conn, wid)  # type: ignore[return-value]
    wf = get_workflow(conn, wid)
    step = next(s for s in (wf.get("steps") or []) if s["step_id"] == step_id)
    # 重新绑定后旧 run 不再代表该步骤：清掉指向它的回切标记，避免看门狗
    # 继续把步骤当成“在等临时上下文”。
    from . import dispatch as dispatch_service
    dispatch_service.clear_inbound_context_returns(conn, step.get("run_id"))
    leases.release(conn, step.get("lease_id"), commit=False)
    lease = leases.acquire(
        conn, "step", f"{wid}:{step_id}", holder_type="role",
        holder=step["role_slug"], run_id=run_id,
        meta={"task_id": wf["task_id"], "workflow_id": wid, "step_id": step_id},
        commit=False)
    now = now_iso()
    conn.execute(
        "UPDATE workflow_steps SET status='running', run_id=?, started_at=?, "
        "ended_at=NULL, next_retry_at=NULL, recovery_count=0, nudge_count=0, "
        "last_nudge_at=NULL, lease_id=?, fence_token=?, updated_at=?, "
        "rework_count=CASE WHEN kind='decision' THEN 0 ELSE rework_count END "
        "WHERE workflow_id=? AND step_id=?",
        (run_id, now, lease["id"], int(lease["fence"]), now, wid, step_id))
    ledger.emit(
        conn, "step.run_rebound", task_id=wf["task_id"], workflow_id=wid,
        execution_no=wf.get("execution_no"), step_id=step_id, run_id=run_id,
        actor_type="human", actor="human",
        payload={"lease_id": lease["id"], "fence": lease["fence"],
                 "prev_run_id": step.get("run_id")},
        mirror_activity=True, activity_kind="step", activity_role=step["role_slug"])
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def _retry_backoff_seconds(attempt: int) -> float:
    """指数退避：base, 2×base, 4×base… 上限 max。"""
    base = max(1.0, float(config.AUTO_RECOVER_BACKOFF_SECONDS))
    cap = max(base, float(config.AUTO_RECOVER_MAX_BACKOFF_SECONDS))
    return min(cap, base * (2 ** max(0, int(attempt) - 1)))


def _end_run_for_step(conn: sqlite3.Connection, wf: dict, step: dict, *,
                      now: str, failure_class: str | None = None) -> None:
    """步骤失败/重排时结束其关联 run，避免残留 running 占住角色、阻断 advance 重派。"""
    run_id = step.get("run_id")
    if not run_id:
        return
    row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or row["status"] in ("completed", "failed", "abandoned"):
        return
    # 步骤结束/重排：清掉指向旧 run 的入站回切标记。
    from . import dispatch as dispatch_service
    dispatch_service.clear_inbound_context_returns(conn, run_id)
    conn.execute(
        "UPDATE runs SET status='failed', ended_at=?, "
        "failure_class=COALESCE(?, failure_class) WHERE id=?",
        (now, failure_class, run_id))
    ledger.emit(
        conn, "run.status_changed", task_id=wf["task_id"], workflow_id=wf["id"],
        execution_no=wf.get("execution_no"), step_id=step["step_id"], run_id=run_id,
        actor_type="system", actor="workflow",
        payload={"status": "failed", "prev": row["status"]},
        mirror_activity=True, activity_kind="run", activity_role=step["role_slug"],
    )


def fail_step(conn: sqlite3.Connection, wid: str, step_id: str, reason: str,
              category: str = "agent_error",
              failure_class: str | None = None) -> None:
    """run 失败/失联：先指数退避自动重试，不 @human；预算耗尽才判失败并留非阻塞通知。

    业界实践（Kubernetes restart backoff / Temporal activity retry / GitHub Actions
    rerun）都是“有限次重试 + 指数退避 + 非阻塞告警”，而不是把恢复挂在人工在场。
    评审、gate、审批等 HITL 确认点不在此路径，仍必须自然人处理。
    """
    wf = get_workflow(conn, wid)
    if wf is None:
        return
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None or step["status"] != "running":
        return
    now = now_iso()
    # 先结束旧 run 再清 run_id：否则旧 run 仍是活跃态，advance 会一直跳过该角色。
    _end_run_for_step(conn, wf, step, now=now,
                      failure_class=failure_class or category)
    recovery = int(step.get("recovery_count") or 0) + 1
    max_attempts = max(1, int(config.AUTO_RECOVER_MAX_ATTEMPTS))
    if config.AUTO_RECOVER_ENABLED and recovery <= max_attempts:
        delay = _retry_backoff_seconds(recovery)
        next_at = (datetime.now(timezone.utc)
                   + timedelta(seconds=delay)).isoformat()
        cur = conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "ended_at=NULL, summary=?, failure_reason=?, recovery_count=?, next_retry_at=?, "
            "lease_id=NULL, fence_token=0, nudge_count=0, last_nudge_at=NULL, updated_at=? "
            "WHERE workflow_id=? AND step_id=? AND status='running'",
            (reason, category, recovery, next_at, now, wid, step_id))
        if cur.rowcount != 1:
            # 步骤已被提交/决策抢先推进：保留旧 run 收尾，但不再改写新状态。
            conn.commit()
            return
        _step_activity(conn, wf["task_id"], step["role_slug"], step, "retrying", reason,
                       extra={"failure_reason": category, "attempt": recovery,
                              "retry_at": next_at, "backoff_seconds": int(delay)})
        leases.release(conn, step.get("lease_id"), commit=False)
        conn.commit()
        return
    cur = conn.execute(
        "UPDATE workflow_steps SET status='failed', summary=?, failure_reason=?, "
        "recovery_count=?, next_retry_at=NULL, lease_id=NULL, fence_token=0, "
        "ended_at=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=? AND status='running'",
        (reason, category, recovery, now, now, wid, step_id))
    if cur.rowcount != 1:
        conn.commit()
        return
    _step_activity(conn, wf["task_id"], step["role_slug"], step, "failed", reason,
                   extra={"failure_reason": category, "attempt": recovery})
    # 非阻塞通知：写 comment 而不是 @human。human 回来后在任务时间线能看到；
    # 但系统不等待人工确认才能继续（依赖此步骤的其它步骤仍按依赖规则等待）。
    ledger.emit_activity(
        conn, task_id=wf["task_id"],
        role_slug="workflow",
        kind="comment",
        payload=jdumps({"body": f"工作流步骤「{step['title']}」（{step['role_slug']}）"
                          f"自动恢复 {recovery - 1} 次后仍失败：{reason}。"
                          "已保留现场，可在任务详情重试该步骤。",
                 "event": "step_failed", "workflow_id": wid, "step_id": step_id,
                 "auto_recovered": recovery - 1}),
        now=now)
    # 最终失败：关闭该 step 的 open 委派，避免自动恢复/后续评审被永久阻塞。
    close_step_delegations(conn, wid, step_id, reason="step_failed")
    leases.release(conn, step.get("lease_id"), commit=False)
    conn.commit()
    try:
        from . import experience_capture
        experience_capture.capture_failure(
            conn, wf["task_id"], run_id=step.get("run_id"), step_id=step_id,
            failure_class=category, actor=step.get("role_slug"))
    except Exception:  # noqa: BLE001 —— 捕获失败不影响失败处理
        pass
    try:
        from . import rsi as rsi_service
        rsi_service.observe_failure(
            conn, wf["task_id"], role=step.get("role_slug"), step_id=step_id,
            run_id=step.get("run_id"), failure_class=category)
    except Exception:  # noqa: BLE001 —— RSI 观测失败不影响失败处理
        pass
