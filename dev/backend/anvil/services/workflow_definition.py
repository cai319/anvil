"""Workflow 定义层：切片展开、DAG/loop 校验、参与者同步与创建。"""
from __future__ import annotations

import re
import sqlite3

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from ..workflow_payload import normalize_definition_payload
from . import dossier, leases, ledger, reviews, weights
from .workflow_common import (
    _assert_current_run, _assert_delegations_done, _step_activity,
    close_step_delegations, get_workflow, logger, workflow_lifecycle_lock,
)
from .workflow_review import _finalize_if_done


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _hint_flag(value, where: str) -> int:
    """subagent_hint 只接受 JSON bool（内部生成步骤也允许 0/1）。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ConflictError(f"{where}.subagent_hint must be a JSON boolean (true|false)")


def _role_runtime_id(conn: sqlite3.Connection, role_slug: str | None) -> str | None:
    """角色绑定的 runtime id；未绑定时回退系统默认 runtime。"""
    if role_slug:
        row = conn.execute(
            "SELECT definition_json FROM roles WHERE slug=?", (role_slug,)).fetchone()
        if row is not None:
            definition = jloads(row["definition_json"], {})
            runtime = definition.get("runtime")
            if isinstance(runtime, dict):
                runtime = runtime.get("id")
            if runtime:
                return str(runtime)
    return config.DEFAULT_RUNTIME


def _runtime_supports_subagents(conn: sqlite3.Connection,
                                role_slug: str | None) -> bool:
    """与 `graph.nodes._supports_subagents` 同口径：DB capabilities → YAML 定义。"""
    from . import runtime as runtime_service

    runtime_id = _role_runtime_id(conn, role_slug)
    if not runtime_id:
        return False
    row = runtime_service.get(conn, runtime_id) or {}
    caps = row.get("capabilities") or {}
    if isinstance(caps, str):
        caps = jloads(caps, {})
    if isinstance(caps, dict) and "supports_subagents" in caps:
        return bool(caps["supports_subagents"])
    definition = runtime_service.load_definitions().get(runtime_id) or {}
    return bool((definition.get("capabilities") or {}).get("supports_subagents"))


def _effective_hint(conn: sqlite3.Connection, role_slug: str | None, value,
                    *, where: str, task_id: str | None = None,
                    workflow_id: str | None = None) -> int:
    """能力感知的 hint：角色 runtime 不支持时一律降为 false 并审计（不拒绝草案）。"""
    requested = _hint_flag(value, where)
    if not requested:
        return 0
    if _runtime_supports_subagents(conn, role_slug):
        return 1
    runtime_id = _role_runtime_id(conn, role_slug)
    try:
        ledger.emit(
            conn, "subagent_hint.downgraded", task_id=task_id,
            workflow_id=workflow_id, actor_type="system", actor="validator",
            payload={
                "where": where, "role": role_slug or None,
                "runtime": runtime_id, "requested": True, "effective": False,
                "reason": "runtime does not support subagents",
            })
    except Exception:  # noqa: BLE001 —— 审计失败不阻断降级
        pass
    logger.info(
        "subagent_hint downgraded: %s role=%s runtime=%s", where, role_slug, runtime_id)
    return 0


def _decide_stage(decision_role: str, verify_role: str,
                  verify2_role: str | None = None) -> str | None:
    """decide 与验证角色重合时返回 stage 槽名。

    上下文隔离只避免同一会话自判，并不隔离模型错误：相同角色的两段会话
    仍是同一模型，可能犯同类错误。
    """
    role = str(decision_role or "").strip()
    if role and role in {str(verify_role or "").strip(),
                         str(verify2_role or "").strip()}:
        return "decide"
    return None


def _expand_slices(definition: dict) -> dict:
    """把 Cindy 的 slices 定义展开成每个切片的固定闭环。

    每片展开为：
      <id>.scope → <id>.execute[...] → <id>.verify → <id>.fix
      → <id>.verify2 → <id>.decide（kind=decision）

    下一片的 scope 默认依赖上一片的 decide，形成
    「切片 → 多角色闭环 → 决策 → 下一片」；把闭环结构交给服务端，
    避免模型自由发挥时退化成一条串行长链。
    """
    slices = definition.get("slices")
    if not isinstance(slices, list) or not slices:
        raise ConflictError("workflow slices must be a non-empty list")

    steps: list[dict] = []
    seen: set[str] = set()
    previous_decide: str | None = None
    slice_decide: dict[str, str] = {}
    current_slice: str | None = None

    def add(step: dict) -> str:
        sid = str(step.get("id") or "").strip()
        if not sid:
            raise ConflictError("workflow slice step missing id")
        if sid in seen:
            raise ConflictError(f"duplicate workflow step id: {sid}")
        seen.add(sid)
        if current_slice is not None:
            # 静态切片同样按 slice 隔离上下文；source_step_id 只属于运行期动态切片。
            step.setdefault("slice_key", current_slice)
        step["instructions"] = _append_collab_rule(step.get("instructions"))
        steps.append(step)
        return sid

    for index, raw_slice in enumerate(slices):
        if not isinstance(raw_slice, dict):
            raise ConflictError(f"workflow slice #{index + 1} must be an object")
        base = str(raw_slice.get("id") or f"S{index + 1}").strip()
        if not SLICE_ID_RE.match(base):
            raise ConflictError(
                f"invalid slice id {base!r}: use letters/digits/_/-, max 64 chars")
        current_slice = base
        title = str(raw_slice.get("title") or base).strip()
        execute_role = str(raw_slice.get("execute_role") or raw_slice.get("role") or "").strip()
        verify_role = str(
            raw_slice.get("verify_role") or raw_slice.get("reviewer") or "").strip()
        fix_role = str(raw_slice.get("fix_role") or execute_role).strip()
        decision_role = str(
            raw_slice.get("decision_role") or raw_slice.get("lead_role") or "").strip()
        lead_role = str(raw_slice.get("lead_role") or decision_role).strip()
        slice_hint = _hint_flag(raw_slice.get("subagent_hint"), f"slice {base}")
        scope_spec = raw_slice.get("scope") if isinstance(raw_slice.get("scope"), dict) else {}

        if not execute_role:
            raise ConflictError(f"slice {base}: execute_role is required")
        if not verify_role:
            raise ConflictError(f"slice {base}: verify_role is required")
        if not decision_role:
            raise ConflictError(f"slice {base}: decision_role is required")
        if execute_role == verify_role:
            raise ConflictError(
                f"slice {base}: verify_role must differ from execute_role")
        if decision_role in (execute_role, fix_role):
            raise ConflictError(
                f"slice {base}: decision_role must differ from execute/fix role")
        if fix_role == verify_role:
            raise ConflictError(
                f"slice {base}: fix_role must differ from verify_role")

        if raw_slice.get("depends_on") is not None:
            # 允许写切片 id（如 "A"），自动映射到该切片的 decide 步骤。
            scope_deps = [slice_decide.get(str(d), str(d))
                          for d in _as_list(raw_slice.get("depends_on"))]
        else:
            scope_deps = [previous_decide] if previous_decide else []
        objective = str(raw_slice.get("objective") or "").strip()
        scope_paths = [str(p) for p in _as_list(raw_slice.get("scope_paths"))]
        scope_default = (
            f"确认切片「{title}」的范围、文件清单、风险面与验收标准。"
            + (f"切片目标：{objective}。" if objective else "")
            + (f"文件范围：{'、'.join(scope_paths)}；越界修改先 @ {decision_role} 确认。"
               if scope_paths else "")
            + "用 `anvil task comment` 在任务会话发布切片计划；"
            f"@ {execute_role} 确认范围可执行，@ {verify_role} 确认验收可验证。"
            "列出排除项与依赖，不要开始实现。"
        )
        scope_id = add({
            "id": f"{base}.scope",
            "title": str(raw_slice.get("scope_title") or f"{title} · 切片范围与验收基线"),
            "role": lead_role,
            "depends_on": scope_deps,
            "instructions": str(raw_slice.get("scope_instructions") or scope_default),
            "acceptance": _as_list(raw_slice.get("scope_acceptance")) or [
                "切片范围、文件/目录清单与排除项明确。",
                "执行角色与验证角色已确认范围可执行、可验证。",
                "验收标准可直接用于后续 verify/decide 判定。",
            ],
            "subagent_hint": 1 if (
                slice_hint or _hint_flag(scope_spec.get("subagent_hint"),
                                         f"slice {base}.scope")) else 0,
        })

        execute_specs = raw_slice.get("execute_steps") or raw_slice.get("tasks") or []
        if isinstance(execute_specs, dict):
            execute_specs = [execute_specs]
        if not isinstance(execute_specs, list) or not execute_specs:
            execute_specs = [{
                "title": raw_slice.get("execute_title") or f"{title} · 执行",
                "instructions": raw_slice.get("instructions") or "",
                "acceptance": raw_slice.get("acceptance") or [],
                "contract": raw_slice.get("contract") or "",
            }]
        execute_ids: list[str] = []
        previous_execute = scope_id
        for offset, raw_execute in enumerate(execute_specs):
            if not isinstance(raw_execute, dict):
                raw_execute = {"instructions": str(raw_execute)}
            slug = str(raw_execute.get("id") or (
                "execute" if offset == 0 else f"execute{offset + 1}")).strip()
            execute_id = f"{base}.{slug}"
            deps = raw_execute.get("depends_on")
            if deps is None:
                deps = [scope_id] if raw_execute.get("parallel") else [previous_execute]
            add({
                "id": execute_id,
                "title": str(raw_execute.get("title") or f"{title} · 执行 {offset + 1}"),
                "role": str(raw_execute.get("role") or execute_role).strip(),
                "depends_on": _as_list(deps),
                "instructions": str(raw_execute.get("instructions") or (
                    "按切片范围完成本步骤的产出；开始与关键节点用 `anvil task comment` "
                    "发布进展，发现问题立即 `anvil task mention` 相关角色。")),
                "acceptance": _as_list(raw_execute.get("acceptance")),
                "contract": str(raw_execute.get("contract")
                                or raw_slice.get("contract") or "") or None,
                "subagent_hint": 1 if (
                    slice_hint
                    or _hint_flag(raw_execute.get("subagent_hint"),
                                  f"slice {base}.{slug}")
                    or _hint_flag(raw_slice.get("execute_hint"),
                                  f"slice {base}.execute")) else 0,
            })
            execute_ids.append(execute_id)
            previous_execute = execute_id

        verify_spec = raw_slice.get("verify") if isinstance(raw_slice.get("verify"), dict) else {}
        verify_id = f"{base}.verify"
        add({
            "id": verify_id,
            "title": str(verify_spec.get("title") or f"{title} · 独立验证"),
            "role": str(verify_spec.get("role") or verify_role).strip(),
            "depends_on": execute_ids,
            "instructions": str(verify_spec.get("instructions") or (
                "独立复核执行步骤的全部产出与证据：逐条核对验收标准，复现关键结论，"
                "标记误报、漏报与证据不足。发现问题 @ 执行角色并给出定位与证据；"
                "不要直接修改执行产出。")),
            "acceptance": _as_list(verify_spec.get("acceptance")) or [
                "逐条核对执行步骤的验收标准并给出通过/不通过结论。",
                "关键结论已独立复现，或说明无法复现的原因。",
                "发现的问题已 @ 执行角色并附定位与证据。",
            ],
            "subagent_hint": 1 if (slice_hint or _hint_flag(verify_spec.get("subagent_hint"), f"slice {base}.verify")) else 0,
        })

        fix_spec = raw_slice.get("fix") if isinstance(raw_slice.get("fix"), dict) else {}
        fix_id = f"{base}.fix"
        add({
            "id": fix_id,
            "title": str(fix_spec.get("title") or f"{title} · 修复与补证"),
            "role": str(fix_spec.get("role") or fix_role).strip(),
            "depends_on": [verify_id],
            "instructions": str(fix_spec.get("instructions") or (
                "按验证意见逐条修复、补证或标注误报；每项处置附证据与回归方式。"
                "若修改范围超出本切片，先在任务会话说明并 @ 决策角色确认。"
                "完成后 @ 验证角色复核。")),
            "acceptance": _as_list(fix_spec.get("acceptance")) or [
                "验证提出的问题逐条有处置结论（修复/补证/误报）。",
                "每项修复附回归方式或复验证据。",
                "未解决的阻塞项已 @ 决策角色并说明风险。",
            ],
            "subagent_hint": 1 if (slice_hint or _hint_flag(fix_spec.get("subagent_hint"), f"slice {base}.fix")) else 0,
        })

        verify2_spec = (raw_slice.get("verify2")
                        if isinstance(raw_slice.get("verify2"), dict) else {})
        verify2_id = f"{base}.verify2"
        add({
            "id": verify2_id,
            "title": str(verify2_spec.get("title") or f"{title} · 复验"),
            "role": str(verify2_spec.get("role") or verify_role).strip(),
            "depends_on": [fix_id],
            "instructions": str(verify2_spec.get("instructions") or (
                "针对修复/补证结果重新验证：复跑关键用例，确认原问题关闭且未引入回归。"
                "逐条给出关闭/未关闭结论与证据。")),
            "acceptance": _as_list(verify2_spec.get("acceptance")) or [
                "原验证问题逐条确认关闭或仍存在，并附证据。",
                "关键回归用例通过，未引入新问题。",
            ],
            "subagent_hint": 1 if (slice_hint or _hint_flag(verify2_spec.get("subagent_hint"), f"slice {base}.verify2")) else 0,
        })

        decide_spec = (raw_slice.get("decide")
                       if isinstance(raw_slice.get("decide"), dict) else {})
        try:
            max_rework = int(raw_slice.get("max_rework", 2))
        except (TypeError, ValueError):
            raise ConflictError(f"slice {base}: invalid max_rework")
        if max_rework < 0:
            raise ConflictError(f"slice {base}: max_rework must be >= 0")
        decide_id = f"{base}.decide"
        add({
            "id": decide_id,
            "kind": "decision",
            "title": str(decide_spec.get("title") or f"{title} · 切片决策"),
            "role": decision_role,
            "stage": _decide_stage(
                decision_role,
                str(verify_spec.get("role") or verify_role).strip(),
                str(verify2_spec.get("role") or verify_role).strip()),
            "depends_on": [verify2_id],
            "on_reject": [fix_id, verify2_id],
            "max_rework": max_rework,
            "instructions": str(decide_spec.get("instructions") or (
                "你是本切片的决策角色。只审阅依赖步骤的产出、证据与验收标准，"
                "不要亲自实现。逐条给出通过/不通过结论；通过时写明放行依据与剩余风险，"
                "打回时逐条列出必须修改的问题与复验要求。")),
            "acceptance": _as_list(decide_spec.get("acceptance")) or [
                "逐条核对本切片验收标准并给出结论。",
                "通过时写明放行依据与剩余风险；打回时列出可执行的返工要求。",
                "决策结论已写入任务会话，下一片据此决定是否开工。",
            ],
            "subagent_hint": 1 if (slice_hint or _hint_flag(decide_spec.get("subagent_hint"), f"slice {base}.decide")) else 0,
        })
        slice_decide[base] = decide_id
        previous_decide = decide_id

    return {**definition, "steps": steps}


def normalize_definition(conn: sqlite3.Connection, definition: dict) -> dict:
    """兼容 flat steps / 静态 slices / 动态 plan_slices+loop 三种写法。

    - flat steps：小型工作流；
    - slices：切片清单已确定，服务端确定性展开（保留兼容）；
    - plan_slices + loop：Cindy 只定义工作流骨架，由运行期角色盘点项目后产出切片，
      服务端再按 loop 模板生成每片闭环（大型任务推荐）。
    """
    if not isinstance(definition, dict):
        raise ConflictError("workflow definition must be an object")
    if definition.get("slices") and definition.get("loop"):
        raise ConflictError("workflow cannot use both `slices` and `loop`")
    if definition.get("steps") or definition.get("children"):
        return normalize_definition_payload(definition)
    if definition.get("slices"):
        return normalize_definition_payload(_expand_slices(definition))
    return normalize_definition_payload(definition)


def _plan_steps(conn: sqlite3.Connection, steps: list[dict], *,
                task_id: str | None = None,
                workflow_id: str | None = None):
    """预校验 id/依赖/环/decision 元数据，返回 raw 与落库计划。"""
    from . import roles as roles_service

    raw: dict[str, dict] = {}
    for s in steps:
        sid = str(s.get("id") or s.get("step_id") or "")
        if not sid:
            raise ConflictError("workflow step missing id")
        if sid in raw:
            raise ConflictError(f"duplicate workflow step id: {sid}")
        raw[sid] = s
    for sid, s in raw.items():
        for dep in (s.get("depends_on") or []):
            if str(dep) not in raw:
                raise ConflictError(f"step {sid}: unknown dependency {dep}")
    state: dict[str, int] = {}

    def visit(sid: str, stack: list[str]) -> None:
        if state.get(sid) == 1:
            raise ConflictError("workflow dependency cycle: " + " -> ".join(stack + [sid]))
        if state.get(sid) == 2:
            return
        state[sid] = 1
        for dep in (raw[sid].get("depends_on") or []):
            visit(str(dep), stack + [sid])
        state[sid] = 2

    for sid in raw:
        visit(sid, [])

    def ancestors(sid: str) -> set[str]:
        found: set[str] = set()
        stack = [str(x) for x in (raw[sid].get("depends_on") or [])]
        while stack:
            cur = stack.pop()
            if cur in found:
                continue
            found.add(cur)
            stack.extend(str(x) for x in (raw[cur].get("depends_on") or []))
        return found

    plan: list[tuple[str, str, str, str, int, list[str], int, dict]] = []
    for sid, s in raw.items():
        role = str(s.get("role") or s.get("role_slug") or "")
        if role and roles_service.is_onboarding(conn, role):
            raise ConflictError(f"step {sid}: onboarding assistant cannot execute workflow steps")
        reviewer = str(s.get("reviewer") or "").strip() or None
        if reviewer and reviewer == role:
            raise ConflictError(
                f"step {sid}: reviewer must differ from implementer ({role})")
        kind = str(s.get("kind") or "task")
        if kind not in ("task", "decision", "plan_slices"):
            raise ConflictError(f"step {sid}: invalid kind {kind!r}")
        gate = 1 if s.get("gate") else 0
        authority = str(s.get("decision_authority") or "").strip().lower()
        if authority not in ("", "ai", "human", "joint"):
            raise ConflictError(f"step {sid}: invalid decision_authority {authority!r}")
        if gate and authority == "ai":
            raise ConflictError(f"step {sid}: a gate cannot use ai authority")
        risk_level = str(s.get("risk_level") or "normal").strip().lower()
        if risk_level not in ("normal", "high", "critical"):
            raise ConflictError(f"step {sid}: invalid risk_level {risk_level!r}")
        reversible = 1 if s.get("reversible", True) else 0
        if kind == "decision" and not reversible and authority == "ai":
            raise ConflictError(
                f"step {sid}: irreversible decisions cannot use ai authority")
        if kind == "decision" and risk_level == "critical" and authority == "ai":
            raise ConflictError(
                f"step {sid}: critical-risk decisions cannot use ai authority")
        triggers = s.get("escalate") or []
        if not isinstance(triggers, list):
            raise ConflictError(f"step {sid}: escalate must be a list")
        options = s.get("decision_options") or []
        if not isinstance(options, list):
            raise ConflictError(f"step {sid}: decision_options must be a list")
        for opt in options:
            if not isinstance(opt, dict):
                raise ConflictError(f"step {sid}: each decision option must be an object")
            if str(opt.get("decision") or "") not in ("approve", "reject", "escalate"):
                raise ConflictError(
                    f"step {sid}: option decision must be approve|reject|escalate")
        question = str(s.get("decision_question") or "").strip() or None
        scope_paths = s.get("scope_paths") or []
        if not isinstance(scope_paths, list):
            raise ConflictError(f"step {sid}: scope_paths must be a list")
        meta = {
            "decision_authority": authority,
            "risk_level": risk_level,
            "reversible": reversible,
            "escalate": [str(x) for x in triggers],
            "decision_question": question,
            "decision_options": options,
            "evidence_required": 1 if s.get("evidence_required") else 0,
            "scope_paths": [str(x) for x in scope_paths],
            "subagent_hint": _effective_hint(
                conn, role, s.get("subagent_hint"), where=f"step {sid}",
                task_id=task_id, workflow_id=workflow_id),
        }
        on_reject: list[str] = []
        max_rework = 0
        if kind == "plan_slices":
            # plan_slices 允许作为可选人工门（v1.62 唯一的门禁规则放宽）：
            # 门开启后只有 plan_json 与评审包落库，人工批准后才展开切片。
            if gate and authority in ("ai", "joint"):
                raise ConflictError(
                    f"plan_slices step {sid}: a plan gate cannot use "
                    f"{authority} authority; human approval is required")
            if s.get("on_reject"):
                raise ConflictError(f"step {sid}: on_reject is only valid for decision steps")
        elif kind == "decision":
            if gate:
                raise ConflictError(f"decision step {sid} cannot be a human gate")
            if not (s.get("depends_on") or []):
                raise ConflictError(f"decision step {sid} requires depends_on")
            targets = [str(x) for x in (s.get("on_reject") or [])]
            if not targets:
                raise ConflictError(f"decision step {sid} requires on_reject")
            anc = ancestors(sid)
            for t in targets:
                if t not in raw:
                    raise ConflictError(f"step {sid}: unknown on_reject target {t}")
                if t == sid:
                    raise ConflictError(f"step {sid}: on_reject cannot target itself")
                if t not in anc:
                    raise ConflictError(
                        f"step {sid}: on_reject target {t} is not an ancestor")
                if raw[t].get("gate"):
                    raise ConflictError(
                        f"step {sid}: on_reject target {t} is a gate step")
            # 闭包：目标 + 目标到 decision 之间所有需要一起重做的下游步骤。
            reset = set(targets)
            changed = True
            while changed:
                changed = False
                for a in anc:
                    if a in reset:
                        continue
                    if any(str(d) in reset for d in (raw[a].get("depends_on") or [])):
                        reset.add(a)
                        changed = True
            on_reject = sorted(reset)
            try:
                max_rework = int(s.get("max_rework", 2))
            except (TypeError, ValueError):
                raise ConflictError(f"decision step {sid}: invalid max_rework")
            if max_rework < 0:
                raise ConflictError(f"decision step {sid}: max_rework must be >= 0")
        elif s.get("on_reject"):
            raise ConflictError(f"step {sid}: on_reject is only valid for decision steps")
        if kind == "decision" and authority == "joint" and not role:
            raise ConflictError(f"step {sid}: joint authority requires a decider role")
        plan.append((sid, role, kind, reviewer or "", gate, on_reject, max_rework, meta))

    # 大型多角色工作流必须有决策点，不能退化成一条纯串行链。
    roles = {str(s.get("role") or s.get("role_slug") or "") for s in raw.values()}
    roles.discard("")
    decisions = [s for s in raw.values()
                 if str(s.get("kind") or "task") in ("decision", "plan_slices")]
    if len(raw) >= 6 and len(roles) >= 2 and not decisions:
        raise ConflictError(
            f"workflow is a flat serial chain ({len(raw)} steps, {len(roles)} roles, "
            "no decision step). Large multi-role workflows must be organized as per-slice "
            "loops: <slice>.scope → <slice>.execute → <slice>.verify → <slice>.fix → "
            "<slice>.verify2 → <slice>.decide, with the next slice depending on the "
            "previous decide. Use a `plan_slices` step + `loop` template (runtime "
            "slicing), a `slices` definition (static slicing), or add kind=decision steps.")
    return raw, plan


LOOP_PHASES = ("execute", "verify", "fix", "verify2", "decide")


LOOP_HINT_PHASES = ("scope", *LOOP_PHASES)


def _require_loop_subagent_decisions(loop: dict) -> None:
    """Cindy 必须对每个写出的 loop 阶段显式决定是否注入 subagent 提示。

    提示词是软约束，首个真实审计任务里 Cindy 直接漏写了该字段，导致
    verify/fix/verify2 全部按 false 生成。这里把「决策」变成草案校验的一部分：
    可以写 false，但不能不写，避免静默退化。
    """
    missing = [
        phase for phase in LOOP_HINT_PHASES
        if isinstance(loop.get(phase), dict) and "subagent_hint" not in loop[phase]
    ]
    if missing:
        raise ConflictError(
            "workflow loop phases must explicitly set `subagent_hint` (true|false): "
            + ", ".join(f"loop.{phase}" for phase in missing)
            + ". Set true only when the phase must verify multiple independent conditions, "
              "trace source/sink across modules, or read enough surrounding code to overflow "
              "the checker context; otherwise false.")
    invalid = [
        phase for phase in LOOP_HINT_PHASES
        if isinstance(loop.get(phase), dict)
        and "subagent_hint" in loop[phase]
        and not isinstance(loop[phase]["subagent_hint"], bool)
    ]
    if invalid:
        raise ConflictError(
            "workflow loop phase subagent_hint must be a JSON boolean (true|false): "
            + ", ".join(f"loop.{phase}" for phase in invalid))


# 存量模板（升级前发布/已落库的 workflow 定义）可能让 decide 与 verify 共用角色。
# 这类定义只能由服务端在套用旧模板 / 重启旧 workflow 时放行；新的 loop 模板
# 必须显式分离角色，避免同一角色自判。放行后 decide 步骤走 stage 槽。
LEGACY_EQUAL_ROLE_MARKER = "legacy_equal_decide_verify"


def _equal_decide_verify_allowed(definition: dict,
                                 allow_equal_decide_verify: bool = False) -> bool:
    return bool(allow_equal_decide_verify
                or (isinstance(definition, dict)
                    and definition.get(LEGACY_EQUAL_ROLE_MARKER)))


def _validate_loop(conn: sqlite3.Connection, definition: dict,
                   raw: dict[str, dict], *, task_id: str | None = None,
                   workflow_id: str | None = None,
                   allow_equal_decide_verify: bool = False) -> None:
    """校验 plan_slices + loop 模板；切片由运行期角色产出，模板由 Cindy 定义。"""
    from . import roles as roles_service

    loop = definition.get("loop")
    plan_steps = [sid for sid, s in raw.items()
                  if str(s.get("kind") or "task") == "plan_slices"]
    if not loop:
        if plan_steps:
            raise ConflictError(
                f"plan_slices step {plan_steps[0]} requires a top-level `loop` template")
        return
    if not isinstance(loop, dict):
        raise ConflictError("workflow loop must be an object")
    if len(plan_steps) != 1:
        raise ConflictError(
            "workflow must contain exactly one plan_slices step when `loop` is set")
    plan_id = plan_steps[0]
    if str(loop.get("from") or "") != plan_id:
        raise ConflictError(f"workflow loop.from must reference plan_slices step {plan_id}")

    def role_of(phase: str, default: str = "") -> str:
        spec = loop.get(phase)
        if spec is None and default:
            return default
        if not isinstance(spec, dict):
            raise ConflictError(f"workflow loop.{phase} must be an object")
        role = str(spec.get("role") or default).strip()
        if not role:
            raise ConflictError(f"workflow loop.{phase}.role is required")
        if roles_service.is_onboarding(conn, role):
            raise ConflictError(
                f"workflow loop.{phase}: onboarding assistant cannot execute steps")
        if spec.get("gate"):
            raise ConflictError(f"workflow loop.{phase} cannot be a human gate")
        if spec.get("on_reject"):
            raise ConflictError(
                f"workflow loop.{phase}: on_reject is managed by the generated decide step")
        return role

    execute_role = role_of("execute")
    verify_role = role_of("verify")
    fix_role = role_of("fix", execute_role)
    verify2_role = role_of("verify2", verify_role)
    decision_role = role_of("decide")
    if execute_role == verify_role:
        raise ConflictError("workflow loop: verify.role must differ from execute.role")
    if fix_role == verify_role:
        raise ConflictError("workflow loop: fix.role must differ from verify.role")
    if decision_role in (execute_role, fix_role):
        raise ConflictError(
            "workflow loop: decide.role must differ from execute/fix roles")
    if (decision_role == verify_role
            and not _equal_decide_verify_allowed(definition,
                                                 allow_equal_decide_verify)):
        raise ConflictError(
            "workflow loop: decide.role must differ from verify.role; "
            "the same role must not judge work it verified. "
            "Existing templates with equal roles keep running in a stage-keyed "
            "context slot, but new loop templates must separate the roles.")
    scope = loop.get("scope")
    scope_role = decision_role
    if scope is not None:
        if not isinstance(scope, dict):
            raise ConflictError("workflow loop.scope must be an object")
        scope_role = str(scope.get("role") or decision_role).strip()
        if roles_service.is_onboarding(conn, scope_role):
            raise ConflictError(
                "workflow loop.scope: onboarding assistant cannot execute steps")
        if scope.get("gate"):
            raise ConflictError("workflow loop.scope cannot be a human gate")
        if scope.get("on_reject"):
            raise ConflictError(
                "workflow loop.scope: on_reject is managed by the generated decide step")
    try:
        max_rework = int((loop.get("decide") or {}).get("max_rework", 2))
    except (TypeError, ValueError):
        raise ConflictError("workflow loop.decide.max_rework must be an integer")
    if max_rework < 0:
        raise ConflictError("workflow loop.decide.max_rework must be >= 0")
    # 能力感知：角色 runtime 不支持 subagent 时，hint 一律降为 false 并审计。
    # 这里就地改写 definition；create_workflow 随后会把它写入 definition_json，
    # 运行期切片展开读取的就是降级后的值。
    phase_roles = {
        "scope": scope_role, "execute": execute_role, "verify": verify_role,
        "fix": fix_role, "verify2": verify2_role, "decide": decision_role,
    }
    for phase in LOOP_HINT_PHASES:
        spec = loop.get(phase)
        if isinstance(spec, dict) and "subagent_hint" in spec:
            effective = _effective_hint(
                conn, phase_roles.get(phase), spec["subagent_hint"],
                where=f"workflow loop.{phase}", task_id=task_id,
                workflow_id=workflow_id)
            if not effective:
                spec["subagent_hint"] = False


SLICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


LOOP_RESERVED_IDS = {"scope", "verify", "fix", "verify2", "decide"}


def _merge_unique(*groups) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in _as_list(group):
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


SLICE_COLLAB_RULE = (
    "协作路由（先判断再行动）：先查 workflow 是否已排定该产出（有则别重复建单；顺序不对 @cindy）；"
    "本 step 验收依赖的结果：自己做 / 同角色只读用 subagent / 仅评审用 mention / "
    "另一角色独立验收用委派；本 step 不依赖、切片外且需独立上下文的工作用 dispatch；"
    "都不适用就记录，不新建工作。"
)


def _append_collab_rule(text: str) -> str:
    text = str(text or "").strip()
    if SLICE_COLLAB_RULE in text:
        return text
    return (text + "\n\n" + SLICE_COLLAB_RULE) if text else SLICE_COLLAB_RULE


def _slice_context(slice: dict) -> str:
    lines = [f"切片：{slice.get('title') or slice.get('id')}（id={slice.get('id')}）"]
    if slice.get("objective"):
        lines.append(f"目标：{slice['objective']}")
    if slice.get("scope_paths"):
        lines.append("范围：" + "、".join(str(p) for p in slice["scope_paths"]))
    if slice.get("instructions"):
        lines.append("切片说明：\n" + str(slice["instructions"]))
    return "\n".join(lines)


def _loop_spec(loop: dict, phase: str) -> dict:
    spec = loop.get(phase)
    return spec if isinstance(spec, dict) else {}


def _join_instructions(spec: dict, context: str, extra: str = "") -> str:
    parts: list[str] = []
    if str(spec.get("instructions") or "").strip():
        parts.append(str(spec["instructions"]).strip())
    if extra.strip():
        parts.append(extra.strip())
    if context.strip():
        parts.append(context.strip())
    return _append_collab_rule("\n\n".join(parts))


def _dynamic_slice_steps(conn: sqlite3.Connection, plan_step_id: str, loop: dict,
                         slices: list[dict], existing_ids: set[str],
                         weight: str = "standard",
                         previous_decide: str | None = None,
                         known_slice_ids: set[str] | None = None) -> list[dict]:
    """按 loop 模板把运行期切片展开成每片小闭环。

    生成 ID：<plan_step>.<slice_id>.<phase>，便于决策步骤按切片前缀汇总。
    `previous_decide` 用于增量修订：新切片从已有最后一片的 decide 之后续接。
    """
    from . import roles as roles_service

    steps: list[dict] = []
    seen: set[str] = set(existing_ids)

    execute_default = str(_loop_spec(loop, "execute").get("role") or "").strip()
    verify_default = str(_loop_spec(loop, "verify").get("role") or "").strip()
    fix_default = str(_loop_spec(loop, "fix").get("role") or execute_default).strip()
    verify2_default = str(_loop_spec(loop, "verify2").get("role") or verify_default).strip()
    decision_default = str(_loop_spec(loop, "decide").get("role") or "").strip()
    scope_spec = _loop_spec(loop, "scope")
    scope_default = str(scope_spec.get("role") or decision_default).strip()

    def add(step: dict) -> str:
        sid = str(step.get("id") or "").strip()
        if not sid:
            raise ConflictError("generated workflow step missing id")
        if sid in seen:
            raise ConflictError(f"duplicate workflow step id: {sid}")
        if weight == "strict" and str(step.get("kind") or "task") == "task":
            step.setdefault("evidence_required", True)
        seen.add(sid)
        steps.append(step)
        return sid

    def check_role(role: str, slice_id: str, phase: str) -> str:
        role = str(role or "").strip()
        if not role:
            raise ConflictError(f"slice {slice_id}: {phase} role is required")
        if roles_service.is_onboarding(conn, role):
            raise ConflictError(
                f"slice {slice_id}: onboarding assistant cannot execute workflow steps")
        return role

    def phase_hint(spec: dict | None) -> int:
        # 只认 Cindy 的 loop 模板；运行期切片计划不得覆盖提示编排。
        return _hint_flag((spec or {}).get("subagent_hint"), "workflow loop phase")

    declared_ids = set(known_slice_ids or ())
    declared_ids.update(str(s.get("id") or "").strip()
                        for s in slices if isinstance(s, dict))
    for raw_slice in slices:
        if not isinstance(raw_slice, dict):
            raise ConflictError("each slice must be an object")
        slice_id = str(raw_slice.get("id") or "").strip()
        if not SLICE_ID_RE.match(slice_id):
            raise ConflictError(
                f"invalid slice id {slice_id!r}: use letters/digits/_/-, max 64 chars")
        title = str(raw_slice.get("title") or slice_id).strip()
        slice_acceptance = _merge_unique(raw_slice.get("acceptance"))
        # enabling 切片：在 plan 审批时显式声明，必须写明它支撑的下游切片，
        # 并由检查保证「现有测试保持通过」。运行时不得追加（修订层会拒绝）。
        enabling = bool(raw_slice.get("enabling"))
        if enabling:
            enables = [str(x).strip() for x in _as_list(raw_slice.get("enables"))
                       if str(x).strip()]
            missing = [x for x in enables if x == slice_id or x not in declared_ids]
            if not enables:
                raise ConflictError(
                    f"enabling slice {slice_id}: `enables` must declare the "
                    "downstream slices it supports")
            if missing:
                raise ConflictError(
                    f"enabling slice {slice_id}: declared downstream slices do not "
                    f"exist in this plan: {', '.join(missing)}")
            slice_acceptance = _merge_unique(slice_acceptance, ["现有测试保持通过"])
        context = _slice_context(raw_slice)

        # 角色分配是 Cindy（planner）在 loop 模板中的专属职责；运行期切片计划
        # 只能决定「这一片做什么、多少轮」，不能重新分配角色或 loop 阶段。
        overrides = [
            key for key in (
                "role", "execute_role", "verify_role", "fix_role", "verify2_role",
                "decision_role", "lead_role", "reviewer",
            ) if key in raw_slice
        ]
        if overrides:
            raise ConflictError(
                f"slice {slice_id}: role fields are not allowed in a runtime slice plan "
                f"({', '.join(overrides)}); roles are fixed by Cindy's loop template. "
                "If the loop structure must change, mention Cindy to revise the workflow.")
        if "subagent_hint" in raw_slice:
            raise ConflictError(
                f"slice {slice_id}: subagent_hint is controlled by Cindy's loop template; "
                "runtime slice plans must not set it.")
        execute_role = check_role(execute_default, slice_id, "execute")
        verify_role = check_role(verify_default, slice_id, "verify")
        fix_role = check_role(fix_default or execute_role, slice_id, "fix")
        verify2_role = check_role(verify2_default or verify_role, slice_id, "verify2")
        decision_role = check_role(decision_default, slice_id, "decide")
        lead_role = check_role(scope_default or decision_role, slice_id, "scope")
        if execute_role == verify_role:
            raise ConflictError(
                f"slice {slice_id}: verify role must differ from execute role")
        if fix_role == verify_role:
            raise ConflictError(
                f"slice {slice_id}: fix role must differ from verify role")
        if verify2_role in (execute_role, fix_role):
            raise ConflictError(
                f"slice {slice_id}: verify2 role must differ from execute/fix roles")
        if decision_role in (execute_role, fix_role):
            raise ConflictError(
                f"slice {slice_id}: decision role must differ from execute/fix roles")

        first_dep = previous_decide or plan_step_id
        external_deps = [
            str(x) for x in (raw_slice.get("external_deps") or [])]
        external_added = False
        cursor = first_dep
        if scope_spec:
            scope_id = add({
                "id": f"{plan_step_id}.{slice_id}.scope",
                "title": f"{title} · {scope_spec.get('title') or '切片范围确认'}",
                "role": lead_role,
                "depends_on": _merge_unique([first_dep, *external_deps]),
                "instructions": _join_instructions(scope_spec, context),
                "acceptance": _merge_unique(scope_spec.get("acceptance")),
                "source_step_id": plan_step_id,
                "slice_key": slice_id,
                "subagent_hint": phase_hint(scope_spec),
            })
            external_added = True
            cursor = scope_id

        execute_specs = raw_slice.get("execute_steps")
        if isinstance(execute_specs, dict):
            execute_specs = [execute_specs]
        if not isinstance(execute_specs, list) or not execute_specs:
            execute_specs = [{}]
        execute_ids: list[str] = []
        previous_execute = cursor
        for offset, raw_execute in enumerate(execute_specs):
            if not isinstance(raw_execute, dict):
                raw_execute = {"instructions": str(raw_execute)}
            if "subagent_hint" in raw_execute:
                raise ConflictError(
                    f"slice {slice_id}: execute step "
                    f"{raw_execute.get('id') or offset + 1} must not set subagent_hint; "
                    "it is controlled by Cindy's loop template.")
            slug = str(raw_execute.get("id") or (
                "execute" if offset == 0 else f"execute{offset + 1}")).strip()
            if slug in LOOP_RESERVED_IDS or not SLICE_ID_RE.match(slug):
                raise ConflictError(
                    f"slice {slice_id}: invalid execute step id {slug!r} "
                    f"(reserved: {sorted(LOOP_RESERVED_IDS)})")
            execute_id = f"{plan_step_id}.{slice_id}.{slug}"
            deps = raw_execute.get("depends_on")
            if deps is None:
                deps = [cursor] if raw_execute.get("parallel") else [previous_execute]
            else:
                deps = [
                    str(d) if str(d).startswith(plan_step_id + ".") or str(d) == plan_step_id
                    else f"{plan_step_id}.{slice_id}.{d}"
                    for d in _as_list(deps)
                ]
            if offset == 0 and external_deps and not external_added:
                deps = _merge_unique([*deps, *external_deps])
            execute_spec = _loop_spec(loop, "execute")
            execute_acceptance = _merge_unique(
                slice_acceptance, raw_execute.get("acceptance"),
                execute_spec.get("acceptance"))
            if not execute_acceptance:
                execute_acceptance = ["按切片范围完成产出，并在提交摘要中附证据与遗留问题"]
            if raw_execute.get("role"):
                raise ConflictError(
                    f"slice {slice_id}: execute step {slug} must not specify a role; "
                    "Cindy's loop.execute.role applies to all execute steps.")
            ex_role = execute_role
            add({
                "id": execute_id,
                "title": f"{title} · {raw_execute.get('title') or execute_spec.get('title') or '执行'}",
                "role": ex_role,
                "depends_on": _as_list(deps),
                "instructions": _join_instructions(
                    execute_spec, context, str(raw_execute.get("instructions") or "")),
                "acceptance": execute_acceptance,
                "contract": str(raw_execute.get("contract") or raw_slice.get("contract")
                                or execute_spec.get("contract") or "") or None,
                "source_step_id": plan_step_id,
                "slice_key": slice_id,
                "subagent_hint": phase_hint(execute_spec),
            })
            execute_ids.append(execute_id)
            previous_execute = execute_id

        verify_spec = _loop_spec(loop, "verify")
        verify_id = add({
            "id": f"{plan_step_id}.{slice_id}.verify",
            "title": f"{title} · {verify_spec.get('title') or '独立验证'}",
            "role": verify_role,
            "depends_on": execute_ids,
            "instructions": _join_instructions(verify_spec, context),
            "acceptance": _merge_unique(
                verify_spec.get("acceptance"),
                [f"本切片「{title}」验收标准已逐条核对并给出结论"]),
            "contract": str(verify_spec.get("contract") or "") or None,
            "source_step_id": plan_step_id,
            "slice_key": slice_id,
            "subagent_hint": phase_hint(verify_spec),
        })

        fix_id = None
        verify2_id = None
        if weight != "light":
            fix_spec = _loop_spec(loop, "fix")
            fix_id = add({
                "id": f"{plan_step_id}.{slice_id}.fix",
                "title": f"{title} · {fix_spec.get('title') or '修复与补证'}",
                "role": fix_role,
                "depends_on": [verify_id],
                "instructions": _join_instructions(fix_spec, context),
                "acceptance": _merge_unique(
                    fix_spec.get("acceptance"),
                    ["验证提出的问题逐条有处置结论（修复/补证/误报）"]),
                "contract": str(fix_spec.get("contract") or "") or None,
                "source_step_id": plan_step_id,
                "slice_key": slice_id,
                "subagent_hint": phase_hint(fix_spec),
            })

            verify2_spec = _loop_spec(loop, "verify2")
            verify2_id = add({
                "id": f"{plan_step_id}.{slice_id}.verify2",
                "title": f"{title} · {verify2_spec.get('title') or '复验'}",
                "role": verify2_role,
                "depends_on": [fix_id],
                "instructions": _join_instructions(verify2_spec, context),
                "acceptance": _merge_unique(
                    verify2_spec.get("acceptance"),
                    ["原验证问题逐条确认关闭或仍存在，并附证据"]),
                "contract": str(verify2_spec.get("contract") or "") or None,
                "source_step_id": plan_step_id,
                "slice_key": slice_id,
                "subagent_hint": phase_hint(verify2_spec),
            })

        decide_spec = _loop_spec(loop, "decide")
        try:
            max_rework = int(decide_spec.get("max_rework", 2))
        except (TypeError, ValueError):
            raise ConflictError(f"slice {slice_id}: invalid decide.max_rework")
        if max_rework < 0:
            raise ConflictError(f"slice {slice_id}: decide.max_rework must be >= 0")
        decide_acceptance = _merge_unique(slice_acceptance, decide_spec.get("acceptance"))
        if not decide_acceptance:
            decide_acceptance = [
                f"逐条核对本切片「{title}」的验收标准并给出结论",
                "通过时写明放行依据与剩余风险；打回时列出可执行的返工要求",
                "决策结论已写入任务会话，下一片据此决定是否开工",
            ]
        decide_id = add({
            "id": f"{plan_step_id}.{slice_id}.decide",
            "kind": "decision",
            "title": f"{title} · {decide_spec.get('title') or '切片决策'}",
            "role": decision_role,
            "stage": _decide_stage(decision_role, verify_role, verify2_role),
            "depends_on": [verify2_id or verify_id],
            "on_reject": ([fix_id, verify2_id] if verify2_id else
                          [*execute_ids, verify_id]),
            "max_rework": max_rework,
            "instructions": _join_instructions(decide_spec, context),
            "acceptance": decide_acceptance,
            "source_step_id": plan_step_id,
            "slice_key": slice_id,
            "subagent_hint": phase_hint(decide_spec),
        })
        previous_decide = decide_id

    return steps


def _validate_generated_deps(generated: list[dict], known_ids: set[str]) -> None:
    """生成步骤的依赖必须存在且无环；防止角色写错 execute_steps.depends_on 后永久卡住。"""
    generated_ids = {g["id"] for g in generated}
    all_ids = set(known_ids) | generated_ids
    for step in generated:
        for dep in step.get("depends_on") or []:
            if str(dep) not in all_ids:
                raise ConflictError(
                    f"generated step {step['id']}: unknown dependency {dep}")
    state: dict[str, int] = {}
    by_id = {g["id"]: g for g in generated}

    def visit(sid: str, stack: list[str]) -> None:
        if state.get(sid) == 1:
            raise ConflictError("generated workflow dependency cycle: "
                                + " -> ".join(stack + [sid]))
        if state.get(sid) == 2:
            return
        state[sid] = 1
        for dep in (by_id.get(sid) or {}).get("depends_on") or []:
            if str(dep) in generated_ids:
                visit(str(dep), stack + [sid])
        state[sid] = 2

    for sid in generated_ids:
        visit(sid, [])


# ---- plan gate：Brief 覆盖矩阵与评审包 ------------------------------------

_PATHISH_RE = re.compile(r"[/*\\]")


def _acceptance_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("criterion") or item.get("text") or "").strip()
    return str(item or "").strip()


def _acceptance_kind(item) -> str:
    if isinstance(item, dict):
        return str(item.get("kind") or item.get("type") or "").strip().lower()
    return ""


def _norm_text(text) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def _slice_acceptance_texts(raw_slice: dict) -> list[str]:
    items = list(_as_list(raw_slice.get("acceptance")))
    for execute in _as_list(raw_slice.get("execute_steps")):
        if isinstance(execute, dict):
            items.extend(_as_list(execute.get("acceptance")))
    return [text for text in (_acceptance_text(x) for x in items) if text]


def _slice_brief_refs(raw_slice: dict) -> list[str]:
    for key in ("brief_refs", "covers", "addresses", "verifies", "coverage"):
        if raw_slice.get(key) is not None:
            return [str(x).strip() for x in _as_list(raw_slice.get(key))
                    if str(x).strip()]
    return []


def _deferred_reason(entry: dict) -> tuple[str, str]:
    """返回 (brief 验收引用, deferred 理由)。"""
    ref = ""
    for key in ("acceptance", "criterion", "item", "ref", "text"):
        if entry.get(key) is not None:
            ref = str(entry.get(key)).strip()
            break
    return ref, str(entry.get("reason") or entry.get("rationale") or "").strip()


def _looks_like_path(text) -> bool:
    return bool(_PATHISH_RE.search(str(text)))


def _path_prefix(pattern: str) -> str:
    text = str(pattern).strip().rstrip("/")
    for token in ("/**", "/*", "*"):
        if text.endswith(token):
            text = text[: -len(token)]
    return text.rstrip("/")


def _paths_overlap(left: str, right: str) -> bool:
    """粗略判断两个 glob 路径是否可能覆盖同一路径。"""
    if str(left).strip() == str(right).strip():
        return True
    a, b = _path_prefix(left), _path_prefix(right)
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def build_plan_review(conn: sqlite3.Connection, wf: dict, plan: dict) -> dict:
    """生成 plan 审批评审数据：切片表、Brief 覆盖矩阵、manual 预估、enabling 标记。

    只在 plan 人工门（gate=1）时用于阻断批准；未覆盖的 Brief 验收必须被显式
    deferred 并写明理由，non-goals 与切片 out-of-scope / scope_paths 交叉校验。
    """
    from . import clarification as clarification_service

    slices = [s for s in _as_list(plan.get("slices")) if isinstance(s, dict)]
    brief = clarification_service.confirmed_brief(conn, wf.get("task_id"))
    acceptance_items: list[dict] = []
    non_goals: list[str] = []
    if brief:
        for index, raw in enumerate(_as_list(brief.get("acceptance"))):
            text = _acceptance_text(raw)
            if text:
                acceptance_items.append({"index": index + 1, "text": text})
        non_goals = [str(x).strip() for x in _as_list(brief.get("non_goals"))
                     if str(x).strip()]

    deferred_entries = [d for d in _as_list(plan.get("deferred"))
                        if isinstance(d, dict)]
    # 兼容按切片声明延期理由的写法。
    for raw_slice in slices:
        deferred_entries.extend(
            d for d in _as_list(raw_slice.get("deferred")) if isinstance(d, dict))
    coverage: list[dict] = []
    for item in acceptance_items:
        text = item["text"]
        normalized = _norm_text(text)
        covering: list[str] = []
        for raw_slice in slices:
            refs = _slice_brief_refs(raw_slice)
            accepted = _slice_acceptance_texts(raw_slice)
            if (text in refs or str(item["index"]) in refs
                    or text in accepted
                    or normalized in {_norm_text(x) for x in accepted}):
                covering.append(str(raw_slice.get("id") or ""))
        deferred_reason = None
        deferred_valid = False
        for entry in deferred_entries:
            ref, reason = _deferred_reason(entry)
            if ref in (text, str(item["index"])) or _norm_text(ref) == normalized:
                deferred_reason = reason
                deferred_valid = bool(reason)
                break
        if covering:
            status = "covered"
        elif deferred_valid:
            status = "deferred"
        else:
            status = "uncovered"
        coverage.append({
            "acceptance": text,
            "index": item["index"],
            "status": status,
            "slices": covering,
            "deferred_reason": deferred_reason,
        })

    non_goal_conflicts: list[dict] = []
    for non_goal in non_goals:
        if not _looks_like_path(non_goal):
            continue
        for raw_slice in slices:
            sid = str(raw_slice.get("id") or "")
            acknowledged = {_norm_text(x)
                            for x in _as_list(raw_slice.get("out_of_scope"))}
            if _norm_text(non_goal) in acknowledged:
                continue
            paths = [str(x) for x in _as_list(raw_slice.get("scope_paths"))]
            hits = [p for p in paths if _paths_overlap(non_goal, p)]
            if hits:
                non_goal_conflicts.append({
                    "non_goal": non_goal, "slice_id": sid, "scope_paths": hits})

    manual = total = 0
    table: list[dict] = []
    enabling: list[dict] = []
    for raw_slice in slices:
        items = list(_as_list(raw_slice.get("acceptance")))
        for execute in _as_list(raw_slice.get("execute_steps")):
            if isinstance(execute, dict):
                items.extend(_as_list(execute.get("acceptance")))
        texts = [t for t in (_acceptance_text(x) for x in items) if t]
        total += len(texts)
        manual += sum(1 for x in items if _acceptance_kind(x) == "manual")
        sid = str(raw_slice.get("id") or "")
        enables = [str(x) for x in _as_list(raw_slice.get("enables"))]
        is_enabling = bool(raw_slice.get("enabling"))
        table.append({
            "id": sid,
            "title": str(raw_slice.get("title") or sid),
            "scope_paths": [str(x) for x in _as_list(raw_slice.get("scope_paths"))],
            "out_of_scope": [str(x) for x in _as_list(raw_slice.get("out_of_scope"))],
            "acceptance": texts,
            "brief_refs": _slice_brief_refs(raw_slice),
            "enabling": is_enabling,
            "enables": enables,
            "execute_steps": len(_as_list(raw_slice.get("execute_steps"))),
        })
        if is_enabling:
            enabling.append({
                "slice_id": sid,
                "title": str(raw_slice.get("title") or sid),
                "enables": enables,
                "scope_paths": [str(x) for x in _as_list(raw_slice.get("scope_paths"))],
                "checks": ["existing_tests_green", "downstream_exists"],
            })

    ratio = (manual / total) if total else 0.0
    blocking: list[str] = []
    for entry in coverage:
        if entry["status"] == "uncovered":
            blocking.append(
                f"brief_acceptance_uncovered: {entry['acceptance']}")
    for conflict in non_goal_conflicts:
        blocking.append(
            f"non_goal_conflict: {conflict['non_goal']} "
            f"vs slice {conflict['slice_id']}")
    return {
        "brief": {
            "present": bool(brief),
            "acceptance_count": len(acceptance_items),
            "non_goals": non_goals,
        },
        "slices": table,
        "slice_table": table,
        "coverage": coverage,
        "coverage_matrix": coverage,
        "manual_estimate": {
            "total": total,
            "manual": manual,
            "ratio": round(ratio, 4),
            "threshold": 0.3,
            "exceeds_threshold": ratio > 0.3,
        },
        "enabling": enabling,
        "enabling_slices": enabling,
        "non_goal_conflicts": non_goal_conflicts,
        "blocking": blocking,
    }


def _generated_steps_for_plan(conn: sqlite3.Connection, wf: dict, step_id: str,
                              plan: dict) -> list[dict]:
    loop = (wf.get("definition") or {}).get("loop")
    if not isinstance(loop, dict):
        raise ConflictError("workflow has no loop template")
    existing = {s["step_id"] for s in wf["steps"]}
    generated = _dynamic_slice_steps(
        conn, step_id, loop, plan["slices"], existing,
        weight=str(wf.get("weight") or "standard"))
    _validate_generated_deps(generated, existing)
    return generated


def _insert_generated_steps(conn: sqlite3.Connection, wid: str, step_id: str,
                            generated: list[dict], now: str) -> str | None:
    """落库生成的切片步骤，并把依赖 plan 步骤的下游静态步骤改挂到最后一片 decide。"""
    last_decide = None
    for raw_step in generated:
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, "
            "instructions, depends_on_json, acceptance_json, gate, reviewer, contract, "
            "kind, on_reject_json, max_rework, rework_count, source_step_id, slice_key, "
            "stage, evidence_required, subagent_hint, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, raw_step["id"], raw_step.get("title") or raw_step["id"],
             raw_step.get("role") or "", raw_step.get("instructions") or "",
             jdumps(raw_step.get("depends_on") or []),
             jdumps(raw_step.get("acceptance") or []),
             1 if raw_step.get("gate") else 0,
             raw_step.get("reviewer"), raw_step.get("contract"),
             raw_step.get("kind") or "task",
             jdumps(raw_step.get("on_reject") or []),
             int(raw_step.get("max_rework") or 0), 0,
             raw_step.get("source_step_id"), raw_step.get("slice_key"),
             raw_step.get("stage"),
             1 if raw_step.get("evidence_required") else 0,
             1 if raw_step.get("subagent_hint") else 0,
             "pending", now),
        )
        if str(raw_step.get("kind") or "task") == "decision":
            last_decide = raw_step["id"]

    # 依赖 plan 步骤的静态下游步骤：改挂到最后一片 decide；没有切片则解除依赖。
    for row in conn.execute(
        "SELECT step_id, depends_on_json FROM workflow_steps WHERE workflow_id=? "
        "AND COALESCE(source_step_id,'')='' AND step_id<>? "
        "AND COALESCE(kind,'task')<>'external'",
        (wid, step_id),
    ).fetchall():
        deps = jloads(row["depends_on_json"], [])
        if step_id not in deps:
            continue
        if last_decide:
            new_deps = [last_decide if str(d) == step_id else d for d in deps]
        else:
            new_deps = [d for d in deps if str(d) != step_id]
        conn.execute(
            "UPDATE workflow_steps SET depends_on_json=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (jdumps(new_deps), now, wid, row["step_id"]))
    return last_decide


def _complete_plan_run(conn: sqlite3.Connection, wf: dict, step: dict,
                       step_id: str, now: str) -> None:
    if not step["run_id"]:
        return
    run_row = conn.execute(
        "SELECT status FROM runs WHERE id=?", (step["run_id"],)).fetchone()
    conn.execute("UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                 (now, step["run_id"]))
    ledger.emit(
        conn, "run.status_changed", task_id=wf["task_id"], workflow_id=wf["id"],
        execution_no=wf.get("execution_no"), step_id=step_id,
        run_id=step["run_id"], actor_type="role", actor=step["role_slug"],
        payload={"status": "completed",
                 "prev": run_row["status"] if run_row else None},
        mirror_activity=True, activity_kind="run",
        activity_role=step["role_slug"],
    )


def expand_approved_plan(conn: sqlite3.Connection, wf: dict, step: dict,
                         now: str) -> str | None:
    """人工批准 plan 门后展开切片；调用方负责事务与步骤状态。"""
    step_id = step["step_id"]
    plan = jloads(step.get("plan_json") or "{}", {})
    slices = plan.get("slices")
    if not isinstance(slices, list):
        raise ConflictError("approved plan has no `slices` list")
    existing = conn.execute(
        "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND source_step_id=? LIMIT 1",
        (wf["id"], step_id)).fetchone()
    if existing is not None:
        return None  # 幂等：已展开
    fresh = get_workflow(conn, wf["id"])
    if fresh is None:
        raise LookupError(wf["id"])
    generated = _generated_steps_for_plan(conn, fresh, step_id, plan)
    last_decide = _insert_generated_steps(conn, wf["id"], step_id, generated, now)
    titles = "、".join(str(s.get("title") or s.get("id")) for s in slices[:8])
    summary = f"切片计划：{len(slices)} 片"
    if titles:
        summary += f"（{titles}{'…' if len(slices) > 8 else ''}）"
    conn.execute(
        "UPDATE workflow_steps SET summary=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=?",
        (summary, now, wf["id"], step_id))
    return last_decide


def apply_slice_plan(conn: sqlite3.Connection, wid: str, step_id: str,
                     role: str | None, plan: dict, run_id: str | None = None) -> dict:
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _apply_slice_plan_locked(
            conn, wid, step_id, role, plan, run_id)


def _apply_slice_plan_locked(conn: sqlite3.Connection, wid: str, step_id: str,
                             role: str | None, plan: dict,
                             run_id: str | None = None) -> dict:
    """运行期角色提交切片计划：校验后按 loop 模板生成每片小闭环。

    切片由执行角色基于对项目的实际理解产出；Cindy 只定义工作流骨架与 loop 模板。
    生成后把依赖 plan 步骤的下游静态步骤改挂到最后一片 decide，保证「全部切片
    决策通过后才进入最终验收」。
    """
    if not isinstance(plan, dict):
        raise ConflictError("slice plan must be an object")
    slices = plan.get("slices")
    if not isinstance(slices, list):
        raise ConflictError("slice plan must contain a `slices` list")
    if len(slices) > 100:
        raise ConflictError("too many slices (max 100)")
    for raw in slices:
        if isinstance(raw, dict) and "external_deps" in raw:
            raise ConflictError(
                "external_deps is controlled by external_nodes; "
                "runtime slice plans must not set it")

    now = now_iso()
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
        if step.get("kind") != "plan_slices":
            raise ConflictError(f"step {step_id} is not a plan_slices step")
        if role and role != "human" and role != step["role_slug"]:
            raise PermissionError(f"role {role} cannot plan step {step_id}")

        # 幂等：已经展开过则直接返回，避免 CLI 重试导致重复步骤。
        applied = conn.execute(
            "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND source_step_id=? LIMIT 1",
            (wid, step_id)).fetchone()
        if applied:
            conn.commit()
            return get_workflow(conn, wid)  # type: ignore[return-value]
        # 门禁模式下计划已提交待人工批准：重复提交保持幂等，不重复建 packet。
        if step.get("gate") and step["status"] == "submitted" and step.get("plan_json"):
            conn.commit()
            return get_workflow(conn, wid)  # type: ignore[return-value]
        # workflow/修订草案待审时，禁止继续按旧 loop 展开切片；等待审批结果。
        pending = conn.execute(
            "SELECT 1 FROM board_drafts WHERE source_task=? "
            "AND kind IN ('workflow','workflow_revision') "
            "AND status='pending' LIMIT 1", (wf["task_id"],)).fetchone()
        if pending:
            raise ConflictError(
                "a workflow draft or revision is pending approval; wait for it before "
                "submitting the slice plan (loop roles are owned by the planner)")
        if step["status"] != "running":
            raise ConflictError(f"step {step_id} is {step['status']}, expected running")

        # 先校验切片结构并生成步骤（也验证 enabling 的声明的下游切片存在），
        # 但只有非门禁路径才立即落库；门禁路径要等人工批准。
        generated = _generated_steps_for_plan(conn, wf, step_id, plan)
        review = build_plan_review(conn, wf, plan)
        gated = bool(step.get("gate"))

        titles = "、".join(str(s.get("title") or s.get("id")) for s in slices[:8])
        summary = f"切片计划：{len(slices)} 片"
        if titles:
            summary += f"（{titles}{'…' if len(slices) > 8 else ''}）"

        if gated:
            # 未批准不产生任何可执行步骤：只保存 plan_json 与评审包。
            conn.execute(
                "UPDATE workflow_steps SET status='submitted', summary=?, plan_json=?, "
                "submitted_at=?, updated_at=? WHERE workflow_id=? AND step_id=?",
                (summary + "（待人工批准）", jdumps(plan), now, now, wid, step_id))
            updated = next(s for s in get_workflow(conn, wid)["steps"]
                           if s["step_id"] == step_id)
            reviews.ensure_packet(
                conn, wf, updated, created_by=role or "system", commit=False,
                plan_review=review, force=True)
            _complete_plan_run(conn, wf, step, step_id, now)
            _step_activity(
                conn, wf["task_id"], step["role_slug"], step, "submitted",
                "切片计划已提交，等待人工批准后展开",
                extra={"slice_ids": [str(s.get("id")) for s in slices]})
            leases.release(conn, step.get("lease_id"), commit=False)
            conn.commit()
            return get_workflow(conn, wid)  # type: ignore[return-value]

        # 非门禁 = 自动批准：没有人工审批可阻断，但 enabling 切片仍不得与
        # Brief 非目标冲突。
        if not gated:
            enabling_ids = {entry["slice_id"] for entry in review["enabling"]}
            hard = [conflict for conflict in review["non_goal_conflicts"]
                    if conflict["slice_id"] in enabling_ids]
            if hard:
                raise ConflictError(
                    "plan_gate_blocked: " + "; ".join(
                        f"non_goal_conflict: {c['non_goal']} "
                        f"vs slice {c['slice_id']}" for c in hard))

        _insert_generated_steps(conn, wid, step_id, generated, now)
        conn.execute(
            "UPDATE workflow_steps SET status='approved', summary=?, plan_json=?, "
            "ended_at=?, updated_at=? WHERE workflow_id=? AND step_id=?",
            (summary, jdumps(plan), now, now, wid, step_id))
        _complete_plan_run(conn, wf, step, step_id, now)
        _step_activity(
            conn, wf["task_id"], step["role_slug"], step, "planned", summary,
            extra={"slice_ids": [str(s.get("id")) for s in slices]})
        leases.release(conn, step.get("lease_id"), commit=False)
        conn.commit()
        _finalize_if_done(conn, wid)
        return get_workflow(conn, wid)  # type: ignore[return-value]
    except BaseException:
        conn.rollback()
        raise


def _phase_role(loop: dict, phase: str, default: str = "") -> str:
    spec = loop.get(phase)
    if isinstance(spec, dict):
        return str(spec.get("role") or default).strip()
    return default


def _workflow_roles(definition: dict) -> set[str]:
    """Collect every role that can take part in a workflow definition."""
    roles: set[str] = set()
    for step in definition.get("steps") or definition.get("children") or []:
        if isinstance(step, dict):
            role = str(step.get("role") or step.get("role_slug") or "").strip()
            if role:
                roles.add(role)
    loop = definition.get("loop")
    if isinstance(loop, dict):
        execute = _phase_role(loop, "execute")
        verify = _phase_role(loop, "verify")
        decide = _phase_role(loop, "decide")
        roles.update(r for r in (
            _phase_role(loop, "scope", decide),
            execute,
            verify,
            _phase_role(loop, "fix", execute),
            _phase_role(loop, "verify2", verify),
            decide,
        ) if r)
    for raw_slice in definition.get("slices") or []:
        if not isinstance(raw_slice, dict):
            continue
        for key in ("role", "execute_role", "verify_role", "reviewer",
                    "fix_role", "decision_role", "lead_role"):
            role = str(raw_slice.get(key) or "").strip()
            if role:
                roles.add(role)
        for ex in raw_slice.get("execute_steps") or []:
            if isinstance(ex, dict):
                role = str(ex.get("role") or "").strip()
                if role:
                    roles.add(role)
    return roles


def _validate_issue_board(conn: sqlite3.Connection, task_id: str | None,
                          definition: dict) -> None:
    """问题板任务：必须有 reviewer，且 reviewer 必须出现在 workflow 中。"""
    if not task_id:
        return
    task = conn.execute(
        "SELECT issue_board_enabled FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if task is None or not task["issue_board_enabled"]:
        return
    reviewers = [r["role_slug"] for r in conn.execute(
        "SELECT role_slug FROM task_reviewers WHERE task_id=?", (task_id,)
    ).fetchall()]
    if not reviewers:
        raise ConflictError(
            "issue board is enabled but no reviewer is bound; "
            "use `anvil task reviewers <id> --assignees <role>` first")
    roles = _workflow_roles(definition)
    missing = [r for r in reviewers if r not in roles]
    if missing:
        raise ConflictError(
            "workflow must assign every issue-board reviewer to a step or loop phase; "
            "missing: " + ", ".join(missing))


def _participant_roles(conn: sqlite3.Connection, task_id: str) -> set[str]:
    participants = {
        row["role_slug"] for row in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (task_id,)
        ).fetchall()
    }
    task = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is not None and task["assignee"]:
        participants.add(task["assignee"])
    return participants


def _definition_roles(definition: dict) -> set[str]:
    roles = set(_workflow_roles(definition))
    for node in definition.get("external_nodes") or []:
        if isinstance(node, dict):
            role = str(node.get("role") or "").strip()
            if role:
                roles.add(role)
    return roles


def definition_roles(definition: dict) -> set[str]:
    """公开只读视图：workflow definition 中声明的全部角色。"""
    return _definition_roles(definition)


def preview_participant_sync(conn: sqlite3.Connection, task_id: str,
                             definition: dict) -> dict:
    """最终审批前预览：workflow 角色 vs 任务参与者。"""
    from . import roles as roles_service

    roles = _definition_roles(definition)
    participants = _participant_roles(conn, task_id)
    invalid: list[str] = []
    for role in roles:
        row = roles_service.get(conn, role)
        if row is None or not row.get("enabled", True):
            invalid.append(role)
    return {
        "roles": sorted(roles),
        "participants": sorted(participants),
        "add": sorted(roles - participants),
        "missing": sorted(participants - roles),
        "invalid": invalid,
    }


def sync_workflow_participants(conn: sqlite3.Connection, task_id: str,
                               definition: dict) -> list[str]:
    """最终审批时同步参与者：加 workflow 角色、阻止缺 must-participate。"""
    preview = preview_participant_sync(conn, task_id, definition)
    if preview["invalid"]:
        raise ConflictError(
            "workflow references unknown or disabled roles: "
            + ", ".join(preview["invalid"]))
    if preview["missing"]:
        raise ConflictError(
            "workflow does not assign every task participant to a step/phase; "
            "missing: " + ", ".join(preview["missing"])
            + ". Add them to the workflow or remove them from the task first.")
    if not preview["add"]:
        return []
    from . import tasks as tasks_service
    tasks_service.add_assignees(conn, task_id, preview["add"])
    ledger.emit(
        conn, "workflow.participants_synced", task_id=task_id,
        actor_type="system", actor="approval",
        payload={"added": preview["add"], "roles": preview["roles"]})
    conn.commit()
    return preview["add"]


def validate_role_coverage(conn: sqlite3.Connection, task_id: str | None,
                           definition: dict) -> None:
    """切片类 workflow 必须让任务绑定的每个角色都参与。

    角色分配是 planner（Cindy）的专属职责；运行期切片计划不能新增/切换角色，
    这条校验也防止 planner 在 loop 模板里静默漏掉某个已绑定角色。
    """
    if not task_id:
        return
    _validate_issue_board(conn, task_id, definition)
    if not (definition.get("loop") or definition.get("slices")):
        return
    assignees = [
        row["role_slug"] for row in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at",
            (task_id,))
    ]
    task = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task and task["assignee"] and task["assignee"] not in assignees:
        assignees.append(task["assignee"])
    if not assignees:
        return
    roles = _workflow_roles(definition)
    missing = [role for role in assignees if role not in roles]
    if missing:
        raise ConflictError(
            "workflow must assign every bound task role to a step or loop phase; "
            "missing: " + ", ".join(missing) + ". Add them explicitly, or remove "
            "unneeded roles from the task before planning.")


def validate_definition(conn: sqlite3.Connection, definition: dict, *,
                        require_subagent_decisions: bool = False,
                        task_id: str | None = None,
                        allow_equal_decide_verify: bool = False) -> None:
    """落板/草案提交前的结构校验。

    关键约束：大型多角色工作流不能是一条没有决策点的串行长链。Cindy 必须按
    「切片 → 多角色闭环 → 决策 → 下一片」组织；否则打回并给出可执行的修正要求。

    require_subagent_decisions=True 时（agent 提交的 workflow 草案），每个写出的
    loop 阶段都必须显式带 subagent_hint，禁止静默省略。
    """
    definition = normalize_definition(conn, definition)
    steps = definition.get("steps") or definition.get("children") or []
    if not steps:
        raise ConflictError("workflow has no steps")
    if require_subagent_decisions:
        loop = definition.get("loop")
        if isinstance(loop, dict):
            _require_loop_subagent_decisions(loop)
    raw, _plan = _plan_steps(conn, steps, task_id=task_id)
    _validate_loop(conn, definition, raw, task_id=task_id,
                   allow_equal_decide_verify=allow_equal_decide_verify)


def terminate_running_workflows(conn: sqlite3.Connection, task_id: str,
                                reason: str) -> int:
    """人类强制收口时终止该 task 所有 running workflow（保留历史）。"""
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        rows = conn.execute(
            "SELECT id FROM workflows WHERE task_id=? AND status='running'",
            (task_id,)).fetchall()
        now = now_iso()
        for row in rows:
            _terminate_running_workflow(conn, row["id"], reason, now)
        conn.commit()
        return len(rows)


def _terminate_running_workflow(conn: sqlite3.Connection, wid: str, reason: str,
                                now: str) -> None:
    """终止一次执行但保留其 workflow/step/run 历史。终态 workflow 不可再终止。"""
    from . import runs as runs_service
    current = conn.execute(
        "SELECT status FROM workflows WHERE id=?", (wid,)).fetchone()
    if current is None or current["status"] != "running":
        return
    for st in conn.execute(
        "SELECT run_id FROM workflow_steps WHERE workflow_id=? "
        "AND status IN ('running','submitted')", (wid,)
    ).fetchall():
        if st["run_id"]:
            try:
                # 终态不可逆：已完成/失败的 submitted gate run 不能再改 abandoned。
                runs_service.set_status(
                    conn, st["run_id"], "abandoned", ended_at=now,
                    from_statuses=("created", "projecting", "running",
                                   "suspended", "submitted", "reviewing"))
            except Exception:  # noqa: BLE001
                pass
    for lease in leases.for_workflow(conn, wid):
        if lease.get("status") == "active":
            leases.release(conn, lease["id"], commit=False)
    conn.execute(
        "UPDATE workflow_steps SET status='failed', failure_reason='terminated', "
        "ended_at=?, updated_at=? WHERE workflow_id=? AND status IN ('running','submitted')",
        (now, now, wid),
    )
    conn.execute(
        "UPDATE workflow_steps SET status='skipped', failure_reason='terminated', "
        "ended_at=?, updated_at=? WHERE workflow_id=? AND status IN ('pending','rejected')",
        (now, now, wid),
    )
    for row in conn.execute(
            "SELECT step_id FROM workflow_steps WHERE workflow_id=?", (wid,)).fetchall():
        close_step_delegations(
            conn, wid, row["step_id"], reason="execution_terminated")
    wf = conn.execute(
        "SELECT task_id, execution_no FROM workflows WHERE id=?", (wid,)).fetchone()
    conn.execute(
        "UPDATE workflows SET status='terminated', termination_reason=?, terminated_at=?, "
        "finished_at=?, updated_at=? WHERE id=? AND status='running'",
        (reason, now, now, now, wid),
    )
    if wf:
        ledger.emit(
            conn, "execution.terminated", task_id=wf["task_id"], workflow_id=wid,
            execution_no=wf["execution_no"], actor_type="human",
            payload={"reason": reason},
        )
        dossier.persist(conn, wid, commit=False)


def _clear_restarted_slice_contexts(conn: sqlite3.Connection, task_id: str) -> None:
    """新 execution 不复用上一次执行的 slice 会话；task 级上下文保留。"""
    roles = [r["role_slug"] for r in conn.execute(
        "SELECT DISTINCT role_slug FROM role_task_sessions WHERE task_id=? "
        "AND (slice_key IS NOT NULL OR task_key LIKE '%::slice::%')",
        (task_id,)).fetchall()]
    if not roles:
        return
    from . import daemon as daemon_service
    mgr = daemon_service.get_manager()
    for role in roles:
        try:
            if mgr is not None and getattr(mgr, "enabled", False):
                mgr.reset_task_slices(role, task_id)
            else:
                daemon_service.clear_slice_sessions(conn, role, task_id)
        except Exception:  # noqa: BLE001 —— 上下文清理失败不阻断新执行落板
            pass


def create_workflow(conn: sqlite3.Connection, task_id: str, definition: dict,
                    created_by: str = "agent:cindy",
                    *, enforce_role_coverage: bool = True,
                    restart_running: bool = False,
                    restart_reason: str | None = None,
                    restart_note: str | None = None,
                    draft_id: str | None = None,
                    sync_participants: bool = False,
                    allow_equal_decide_verify: bool = False) -> dict:
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _create_workflow_locked(
            conn, task_id, definition, created_by,
            enforce_role_coverage=enforce_role_coverage,
            restart_running=restart_running,
            restart_reason=restart_reason,
            restart_note=restart_note,
            draft_id=draft_id,
            sync_participants=sync_participants,
            allow_equal_decide_verify=allow_equal_decide_verify)


def _create_workflow_locked(conn: sqlite3.Connection, task_id: str, definition: dict,
                            created_by: str = "agent:cindy",
                            *, enforce_role_coverage: bool = True,
                            restart_running: bool = False,
                            restart_reason: str | None = None,
                            restart_note: str | None = None,
                            draft_id: str | None = None,
                            sync_participants: bool = False,
                            allow_equal_decide_verify: bool = False) -> dict:
    """落板 workflow（Cindy 草案批准后调用）：创建步骤并把任务置为进行中。

    decision 步骤（kind=decision）由决策角色评审后 approve/reject；reject 时把
    on_reject 目标及其到 decision 之间的下游闭包一起重置为 pending。
    `enforce_role_coverage=False` 仅供 human 明确确认“模板未覆盖任务角色”后使用。

    运行中的 workflow 不允许被中途替换。需要换流程时，human 显式确认
    `restart_running=True`：旧执行被标记为 terminated 并保留历史，新执行从第一步重新开始。
    """
    task_row = conn.execute(
        "SELECT status, archived_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task_row is None:
        raise LookupError(task_id)
    if task_row["status"] in ("done", "closed") or task_row["archived_at"]:
        raise ConflictError(
            "task is terminal; reopen it before creating a new workflow")

    definition = normalize_definition(conn, definition)
    steps = definition.get("steps") or definition.get("children") or []
    if not steps:
        raise ConflictError("workflow has no steps")

    # 最终审批路径：workflow 角色与任务参与者双向一致。
    # 先补齐 workflow 中的现有角色，再校验 no missing（human 指定角色必须参与）。
    if sync_participants:
        sync_workflow_participants(conn, task_id, definition)
        preview = preview_participant_sync(conn, task_id, definition)
        if preview["add"] or preview["missing"]:
            raise ConflictError(
                "workflow participants are inconsistent: "
                f"add={preview['add']} missing={preview['missing']}")

    # ---- 预校验：id/依赖/环/decision 元数据 ----
    raw, plan = _plan_steps(conn, steps, task_id=task_id)
    _validate_loop(conn, definition, raw, task_id=task_id,
                   allow_equal_decide_verify=allow_equal_decide_verify)
    _validate_issue_board(conn, task_id, definition)
    if enforce_role_coverage:
        validate_role_coverage(conn, task_id, definition)

    template_block = definition.get("template") if isinstance(definition.get("template"), dict) else {}
    seed_block = definition.get("template_seed") if isinstance(definition.get("template_seed"), dict) else {}
    template_id = template_block.get("id") or seed_block.get("template_id")
    template_version = template_block.get("version") or seed_block.get("version")
    weight, weight_source, weight_reason = weights.resolve(conn, task_id, definition)
    wf_hash = weights.definition_hash(definition)

    wid = new_id()
    now = now_iso()
    running = conn.execute(
        "SELECT id, execution_no FROM workflows WHERE task_id=? AND status='running' "
        "ORDER BY created_at DESC, rowid DESC", (task_id,)
    ).fetchall()
    if running and not restart_running:
        raise ConflictError(
            "task already has a running workflow; workflow replacement is not allowed. "
            "Terminate the current execution and restart the task instead.")
    previous_workflow_id = None
    if running:
        previous_workflow_id = running[0]["id"]
        reason = (restart_reason or "").strip() or "人工确认终止当前执行并重新开始"
        for old in running:
            _terminate_running_workflow(conn, old["id"], reason, now)
    else:
        latest = conn.execute(
            "SELECT id FROM workflows WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        previous_workflow_id = latest["id"] if latest else None
    execution_no = conn.execute(
        "SELECT COALESCE(MAX(execution_no), 0) + 1 AS n FROM workflows WHERE task_id=?",
        (task_id,),
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO workflows (id, task_id, definition_json, status, created_by, "
        "created_at, approved_at, updated_at, template_id, template_version, "
        "previous_workflow_id, execution_no, weight, weight_source, weight_reason, "
        "definition_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wid, task_id, jdumps(definition), "running", created_by, now, now, now,
         template_id, template_version, previous_workflow_id, execution_no,
         weight, weight_source, weight_reason, wf_hash),
    )
    ledger.emit(
        conn, "execution.started", task_id=task_id, workflow_id=wid,
        execution_no=execution_no,
        actor_type="human" if str(created_by).startswith("human") else "role",
        actor=created_by,
        payload={"previous_workflow_id": previous_workflow_id,
                 "template_id": template_id, "template_version": template_version,
                 "weight": weight, "weight_source": weight_source,
                 "definition_hash": wf_hash},
    )
    for sid, role, kind, reviewer, gate, on_reject, max_rework, meta in plan:
        s = raw[sid]
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, instructions, "
            "depends_on_json, acceptance_json, gate, reviewer, contract, kind, on_reject_json, "
            "max_rework, rework_count, decision_authority, risk_level, reversible, escalate_json, "
            "decision_question, decision_options_json, evidence_required, scope_paths_json, "
            "subagent_hint, slice_key, stage, source_step_id, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, sid, str(s.get("title") or sid), role,
             str(s.get("instructions") or s.get("description") or ""),
             jdumps(s.get("depends_on") or []), jdumps(s.get("acceptance") or []),
             gate, reviewer or None, str(s.get("contract") or "") or None,
             kind, jdumps(on_reject), max_rework, 0,
             meta["decision_authority"], meta["risk_level"], meta["reversible"],
             jdumps(meta["escalate"]), meta["decision_question"],
             jdumps(meta["decision_options"]),
             1 if (meta["evidence_required"] or (weight == "strict" and kind == "task")) else 0,
             jdumps(meta["scope_paths"]), meta["subagent_hint"], s.get("slice_key"),
             s.get("stage"), s.get("source_step_id"), "pending", now),
        )
    from . import tasks as tasks_service
    if not tasks_service.transition_task(
            conn, task_id, "in_progress",
            from_statuses=("todo", "in_progress", "in_review"),
            fields={"assignee": None, "lease_expires_at": None,
                    "claimed_at": None, "review_rejected_at": None},
            now=now):
        conn.rollback()
        raise ConflictError(
            "task is no longer active; refresh before starting a workflow")
    # 在 in_review 上启动新 workflow 时，旧最终评审失效：已提交/通过的角色
    # 回到 working，让新执行的步骤能正常认领。
    conn.execute(
        "UPDATE task_role_claims SET status='working', submitted_at=NULL, updated_at=? "
        "WHERE task_id=? AND status IN ('submitted','approved')",
        (now, task_id))
    if running:
        note = (restart_note or "").strip()
        body = (
            f"已终止第 {running[0]['execution_no']} 次执行并重新开始第 {execution_no} 次执行。"
            f"原因：{(restart_reason or '').strip() or '人工确认重新执行'}。"
            "旧执行与产物保留在任务历史中。"
        )
        if note:
            body += f" 保留/沿用说明：{note}"
        ledger.emit_activity(
            conn, task_id=task_id,
            role_slug="human",
            kind="comment",
            payload=jdumps({
                "body": body,
                "event": "workflow_restarted",
                "previous_workflow_id": previous_workflow_id,
                "workflow_id": wid,
                "execution_no": execution_no,
                "reason": (restart_reason or "").strip() or "人工确认重新执行",
                "previous_execution_no": running[0]["execution_no"],
            }),
            now=now)
    if draft_id:
        cur = conn.execute(
            "UPDATE board_drafts SET status='approved', decided_at=? "
            "WHERE id=? AND status='pending'", (now, draft_id))
        if cur.rowcount != 1:
            conn.rollback()
            raise ConflictError(
                "draft state changed; refresh before approving")
    # 工作流落板执行后才创建知识收割提醒；子任务/未规划任务不创建。
    from . import knowledge_harvest  # 惰性导入，避免 services 循环
    knowledge_harvest.ensure_task_reminder_by_id(conn, task_id)
    conn.commit()
    if previous_workflow_id:
        _clear_restarted_slice_contexts(conn, task_id)
    return get_workflow(conn, wid)  # type: ignore[return-value]
