"""Workflow（工作流）模型与执行引擎。

- **Task** 是人类的高层工作空间；**Workflow** 是 Cindy 生成的步骤状态机，属于一个 Task。
- 步骤不是独立 task 行；跨角色可并行，**同一角色的多个步骤由引擎串行调度**。
- 状态：pending → running → submitted → approved / rejected / failed；gate=1 的步骤
  提交后进入收件箱等待自然人评审（HITL）。
- 依赖满足且角色空闲时，advance() 通过 starter 创建 run；scheduler 每轮推进。
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import ledger

RUN_ACTIVE = ("created", "projecting", "running")
STEP_DONE = ("approved", "skipped")


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


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

    def add(step: dict) -> str:
        sid = str(step.get("id") or "").strip()
        if not sid:
            raise ConflictError("workflow slice step missing id")
        if sid in seen:
            raise ConflictError(f"duplicate workflow step id: {sid}")
        seen.add(sid)
        steps.append(step)
        return sid

    for index, raw_slice in enumerate(slices):
        if not isinstance(raw_slice, dict):
            raise ConflictError(f"workflow slice #{index + 1} must be an object")
        base = str(raw_slice.get("id") or f"S{index + 1}").strip()
        title = str(raw_slice.get("title") or base).strip()
        execute_role = str(raw_slice.get("execute_role") or raw_slice.get("role") or "").strip()
        verify_role = str(
            raw_slice.get("verify_role") or raw_slice.get("reviewer") or "").strip()
        fix_role = str(raw_slice.get("fix_role") or execute_role).strip()
        decision_role = str(
            raw_slice.get("decision_role") or raw_slice.get("lead_role") or "").strip()
        lead_role = str(raw_slice.get("lead_role") or decision_role).strip()

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
        return definition
    if definition.get("slices"):
        return _expand_slices(definition)
    return definition


def _plan_steps(conn: sqlite3.Connection, steps: list[dict]):
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

    plan: list[tuple[str, str, str, str, int, list[str], int]] = []
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
        on_reject: list[str] = []
        max_rework = 0
        if kind == "plan_slices":
            if gate:
                raise ConflictError(f"plan_slices step {sid} cannot be a human gate")
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
        plan.append((sid, role, kind, reviewer or "", gate, on_reject, max_rework))

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


def _validate_loop(conn: sqlite3.Connection, definition: dict,
                   raw: dict[str, dict]) -> None:
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
    role_of("verify2", verify_role)
    decision_role = role_of("decide")
    if execute_role == verify_role:
        raise ConflictError("workflow loop: verify.role must differ from execute.role")
    if fix_role == verify_role:
        raise ConflictError("workflow loop: fix.role must differ from verify.role")
    if decision_role in (execute_role, fix_role):
        raise ConflictError(
            "workflow loop: decide.role must differ from execute/fix roles")
    scope = loop.get("scope")
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
    return "\n\n".join(parts)


def _dynamic_slice_steps(conn: sqlite3.Connection, plan_step_id: str, loop: dict,
                         slices: list[dict], existing_ids: set[str]) -> list[dict]:
    """按 loop 模板把运行期切片展开成每片小闭环。

    生成 ID：<plan_step>.<slice_id>.<phase>，便于决策步骤按切片前缀汇总。
    """
    from . import roles as roles_service

    steps: list[dict] = []
    seen: set[str] = set(existing_ids)
    previous_decide: str | None = None

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

    for raw_slice in slices:
        if not isinstance(raw_slice, dict):
            raise ConflictError("each slice must be an object")
        slice_id = str(raw_slice.get("id") or "").strip()
        if not SLICE_ID_RE.match(slice_id):
            raise ConflictError(
                f"invalid slice id {slice_id!r}: use letters/digits/_/-, max 64 chars")
        title = str(raw_slice.get("title") or slice_id).strip()
        slice_acceptance = _merge_unique(raw_slice.get("acceptance"))
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
        cursor = first_dep
        if scope_spec:
            scope_id = add({
                "id": f"{plan_step_id}.{slice_id}.scope",
                "title": f"{title} · {scope_spec.get('title') or '切片范围确认'}",
                "role": lead_role,
                "depends_on": [first_dep],
                "instructions": _join_instructions(scope_spec, context),
                "acceptance": _merge_unique(scope_spec.get("acceptance")),
                "source_step_id": plan_step_id,
                "slice_key": slice_id,
            })
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
        })

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
            "depends_on": [verify2_id],
            "on_reject": [fix_id, verify2_id],
            "max_rework": max_rework,
            "instructions": _join_instructions(decide_spec, context),
            "acceptance": decide_acceptance,
            "source_step_id": plan_step_id,
            "slice_key": slice_id,
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


def _assert_current_run(step: dict, run_id: str | None) -> None:
    """旧 run 在步骤重派后不能继续提交/决策；run_id 为空时保留测试/旧客户端兼容。"""
    current = str(step.get("run_id") or "")
    if run_id and current and str(run_id) != current:
        raise ConflictError(
            f"stale_run: step {step.get('step_id')} is bound to run {current}, "
            f"not {run_id}")
    if run_id and not current:
        raise ConflictError(f"stale_run: step {step.get('step_id')} has no active run")


def apply_slice_plan(conn: sqlite3.Connection, wid: str, step_id: str,
                     role: str | None, plan: dict, run_id: str | None = None) -> dict:
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
        # 新 workflow 草案待审时，禁止继续按旧 loop 展开切片；等待 Cindy 的新版本获批。
        pending = conn.execute(
            "SELECT 1 FROM board_drafts WHERE source_task=? AND kind='workflow' "
            "AND status='pending' LIMIT 1", (wf["task_id"],)).fetchone()
        if pending:
            raise ConflictError(
                "a workflow revision is pending approval; wait for it before submitting "
                "the slice plan (loop roles are owned by the planner)")
        if step["status"] != "running":
            raise ConflictError(f"step {step_id} is {step['status']}, expected running")

        loop = (wf.get("definition") or {}).get("loop")
        if not isinstance(loop, dict):
            raise ConflictError("workflow has no loop template")

        generated = _dynamic_slice_steps(
            conn, step_id, loop, slices, {s["step_id"] for s in wf["steps"]})
        _validate_generated_deps(generated, {s["step_id"] for s in wf["steps"]})
        last_decide = None
        for raw_step in generated:
            conn.execute(
                "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, "
                "instructions, depends_on_json, acceptance_json, gate, reviewer, contract, "
                "kind, on_reject_json, max_rework, rework_count, source_step_id, slice_key, "
                "status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                 "pending", now),
            )
            if str(raw_step.get("kind") or "task") == "decision":
                last_decide = raw_step["id"]

        # 依赖 plan 步骤的静态下游步骤：改挂到最后一片 decide；没有切片则解除依赖。
        for row in conn.execute(
            "SELECT step_id, depends_on_json FROM workflow_steps WHERE workflow_id=? "
            "AND COALESCE(source_step_id,'')='' AND step_id<>?",
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

        titles = "、".join(str(s.get("title") or s.get("id")) for s in slices[:8])
        summary = f"切片计划：{len(slices)} 片"
        if titles:
            summary += f"（{titles}{'…' if len(slices) > 8 else ''}）"
        conn.execute(
            "UPDATE workflow_steps SET status='approved', summary=?, plan_json=?, "
            "ended_at=?, updated_at=? WHERE workflow_id=? AND step_id=?",
            (summary, jdumps(plan), now, now, wid, step_id))
        if step["run_id"]:
            run_row = conn.execute(
                "SELECT status FROM runs WHERE id=?", (step["run_id"],)).fetchone()
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
        _step_activity(
            conn, wf["task_id"], step["role_slug"], step, "planned", summary,
            extra={"slice_ids": [str(s.get("id")) for s in slices]})
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


def validate_role_coverage(conn: sqlite3.Connection, task_id: str | None,
                           definition: dict) -> None:
    """切片类 workflow 必须让任务绑定的每个角色都参与。

    角色分配是 planner（Cindy）的专属职责；运行期切片计划不能新增/切换角色，
    这条校验也防止 planner 在 loop 模板里静默漏掉某个已绑定角色。
    """
    if not task_id:
        return
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


def validate_definition(conn: sqlite3.Connection, definition: dict) -> None:
    """落板/草案提交前的结构校验。

    关键约束：大型多角色工作流不能是一条没有决策点的串行长链。Cindy 必须按
    「切片 → 多角色闭环 → 决策 → 下一片」组织；否则打回并给出可执行的修正要求。
    """
    definition = normalize_definition(conn, definition)
    steps = definition.get("steps") or definition.get("children") or []
    if not steps:
        raise ConflictError("workflow has no steps")
    raw, _plan = _plan_steps(conn, steps)
    _validate_loop(conn, definition, raw)


def _terminate_running_workflow(conn: sqlite3.Connection, wid: str, reason: str,
                                now: str) -> None:
    """终止一次执行但保留其 workflow/step/run 历史。"""
    from . import runs as runs_service
    for st in conn.execute(
        "SELECT run_id FROM workflow_steps WHERE workflow_id=? "
        "AND status IN ('running','submitted')", (wid,)
    ).fetchall():
        if st["run_id"]:
            try:
                runs_service.set_status(conn, st["run_id"], "abandoned", ended_at=now)
            except Exception:  # noqa: BLE001
                pass
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
    wf = conn.execute(
        "SELECT task_id, execution_no FROM workflows WHERE id=?", (wid,)).fetchone()
    conn.execute(
        "UPDATE workflows SET status='terminated', termination_reason=?, terminated_at=?, "
        "updated_at=? WHERE id=?",
        (reason, now, now, wid),
    )
    if wf:
        ledger.emit(
            conn, "execution.terminated", task_id=wf["task_id"], workflow_id=wid,
            execution_no=wf["execution_no"], actor_type="human",
            payload={"reason": reason},
        )


def create_workflow(conn: sqlite3.Connection, task_id: str, definition: dict,
                    created_by: str = "agent:cindy",
                    *, enforce_role_coverage: bool = True,
                    restart_running: bool = False,
                    restart_reason: str | None = None,
                    restart_note: str | None = None) -> dict:
    """落板 workflow（Cindy 草案批准后调用）：创建步骤并把任务置为进行中。

    decision 步骤（kind=decision）由决策角色评审后 approve/reject；reject 时把
    on_reject 目标及其到 decision 之间的下游闭包一起重置为 pending。
    `enforce_role_coverage=False` 仅供 human 明确确认“模板未覆盖任务角色”后使用。

    运行中的 workflow 不允许被中途替换。需要换流程时，human 显式确认
    `restart_running=True`：旧执行被标记为 terminated 并保留历史，新执行从第一步重新开始。
    """
    definition = normalize_definition(conn, definition)
    steps = definition.get("steps") or definition.get("children") or []
    if not steps:
        raise ConflictError("workflow has no steps")

    # ---- 预校验：id/依赖/环/decision 元数据 ----
    raw, plan = _plan_steps(conn, steps)
    _validate_loop(conn, definition, raw)
    if enforce_role_coverage:
        validate_role_coverage(conn, task_id, definition)

    template_block = definition.get("template") if isinstance(definition.get("template"), dict) else {}
    seed_block = definition.get("template_seed") if isinstance(definition.get("template_seed"), dict) else {}
    template_id = template_block.get("id") or seed_block.get("template_id")
    template_version = template_block.get("version") or seed_block.get("version")

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
        "previous_workflow_id, execution_no) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (wid, task_id, jdumps(definition), "running", created_by, now, now, now,
         template_id, template_version, previous_workflow_id, execution_no),
    )
    ledger.emit(
        conn, "execution.started", task_id=task_id, workflow_id=wid,
        execution_no=execution_no,
        actor_type="human" if str(created_by).startswith("human") else "role",
        actor=created_by,
        payload={"previous_workflow_id": previous_workflow_id,
                 "template_id": template_id, "template_version": template_version},
    )
    for sid, role, kind, reviewer, gate, on_reject, max_rework in plan:
        s = raw[sid]
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, instructions, "
            "depends_on_json, acceptance_json, gate, reviewer, contract, kind, on_reject_json, "
            "max_rework, rework_count, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, sid, str(s.get("title") or sid), role,
             str(s.get("instructions") or s.get("description") or ""),
             jdumps(s.get("depends_on") or []), jdumps(s.get("acceptance") or []),
             gate, reviewer or None, str(s.get("contract") or "") or None,
             kind, jdumps(on_reject), max_rework, 0, "pending", now),
        )
    conn.execute(
        "UPDATE tasks SET status='in_progress', assignee=NULL, lease_expires_at=NULL, "
        "claimed_at=NULL, updated_at=? WHERE id=? AND status IN ('todo','in_progress')",
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
        conn.execute(
            "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, "human", "comment", jdumps({
                "body": body,
                "event": "workflow_restarted",
                "previous_workflow_id": previous_workflow_id,
                "workflow_id": wid,
                "execution_no": execution_no,
                "reason": (restart_reason or "").strip() or "人工确认重新执行",
                "previous_execution_no": running[0]["execution_no"],
            }), now),
        )
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def get_workflow(conn: sqlite3.Connection, wid: str) -> dict | None:
    r = conn.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["definition"] = jloads(d.get("definition_json") or "{}", {})
    d["steps"] = _steps(conn, wid)
    return d


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
                  COUNT(s.step_id) AS step_count,
                  SUM(CASE WHEN s.status='approved' THEN 1 ELSE 0 END) AS approved_count
             FROM workflows w
             LEFT JOIN workflow_steps s ON s.workflow_id = w.id
            WHERE w.task_id=?
            GROUP BY w.id
            ORDER BY w.execution_no DESC, w.created_at DESC""",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _steps(conn: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY rowid", (wid,)):
        d = dict(r)
        d["depends_on"] = jloads(d.get("depends_on_json") or "[]", [])
        d["acceptance"] = jloads(d.get("acceptance_json") or "[]", [])
        d["gate"] = bool(d["gate"])
        d["kind"] = d.get("kind") or "task"
        d["on_reject"] = jloads(d.get("on_reject_json") or "[]", [])
        d["max_rework"] = int(d.get("max_rework") or 0)
        d["rework_count"] = int(d.get("rework_count") or 0)
        out.append(d)
    return out


def step_prompt(workflow: dict, step: dict, task: dict | None) -> str:
    """注入给角色的步骤说明（替代任务级协议）。"""
    deps = "、".join(step["depends_on"]) or "无"
    acceptance = "\n".join(f"- {a}" for a in step["acceptance"]) or "-（无）"
    contract = (step.get("contract") or "").strip()
    dep_contracts = []
    by_id = {x["step_id"]: x for x in workflow.get("steps", [])}
    for d in step["depends_on"]:
        up = by_id.get(d) or {}
        if (up.get("contract") or "").strip():
            dep_contracts.append(f"[{d} {up.get('title','')}] {up['contract'].strip()}")
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
    elif "." in step["step_id"]:
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
            + "\n## 本切片依赖步骤产出（决策依据）\n"
            + dep_output_block + "\n"
            + "\n## 决策职责\n"
            "你是本切片的决策角色。只审阅依赖步骤的产出、证据与验收标准，"
            "不要亲自实现具体工作。决策前逐条核对验收标准；通过时写明放行依据与剩余风险，"
            "打回时逐条列出必须修改的问题、验收口径与复验要求。\n"
            f"重做次数：{step.get('rework_count', 0)} / {step.get('max_rework', 0)}\n"
            f"可打回的目标步骤（on_reject）：\n{target_block}\n"
            + feedback_block
            + recovery_block
            + "\n完成后执行：\n"
            f"- 通过：`anvil step decide {workflow['id']} {step['step_id']} "
            "--approve --feedback \"结论\"`\n"
            f"- 打回：`anvil step decide {workflow['id']} {step['step_id']} "
            "--reject --feedback \"具体问题与修改要求\"`\n"
            "不要调用 `anvil step submit` / `anvil task submit` / `anvil task claim`。\n"
        )

    return (
        base
        + loop_block
        + feedback_block
        + recovery_block
        + f"\n完成后必须执行：`anvil step submit {workflow['id']} {step['step_id']} "
          "--summary \"完成摘要\"`。\n"
        "不要调用 `anvil task submit` / `anvil task claim`。\n"
    )


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


_ADVANCE_LOCK = threading.Lock()


def advance(conn: sqlite3.Connection, wid: str, starter=None) -> dict:
    """串行化工作流推进，避免 scheduler 与 API 同时通过“无活跃 run”检查。"""
    with _ADVANCE_LOCK:
        return _advance_locked(conn, wid, starter)


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
        role = s["role_slug"]
        if not role:
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
        prompt = step_prompt(wf, s, dict(task) if task else None)
        if starter is not None:
            run = starter(role, wf["task_id"], prompt, s)
            run_id = (run or {}).get("id")
        else:
            run_id = None
        now = now_iso()
        conn.execute(
            "UPDATE workflow_steps SET status='running', run_id=?, started_at=?, ended_at=NULL, "
            "summary=NULL, failure_reason=NULL, next_retry_at=NULL, updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (run_id, now, now, wid, s["step_id"]),
        )
        _step_activity(
            conn, wf["task_id"], role, s, "running",
            extra={"rework_count": int(s.get("rework_count") or 0)}
            if int(s.get("rework_count") or 0) else None,
        )
        started.append(s["step_id"])
    conn.commit()
    _finalize_if_done(conn, wid)
    wf = get_workflow(conn, wid)
    all_done = wf is not None and wf["status"] == "done"
    return {"started": started, "done": all_done}


def submit_step(conn: sqlite3.Connection, wid: str, step_id: str, role: str,
                summary: str | None, run_id: str | None = None) -> dict:
    """角色提交当前步骤（anvil step submit）。gate 步骤进入人工审核。"""
    from . import runs as runs_service

    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None:
        raise LookupError(step_id)
    _assert_current_run(step, run_id)
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
    now = now_iso()
    conn.execute(
        "UPDATE workflow_steps SET status='submitted', summary=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=?",
        (summary, now, wid, step_id),
    )
    if step["run_id"]:
        try:
            runs_service.set_status(conn, step["run_id"], "completed", ended_at=now)
        except Exception:  # noqa: BLE001
            pass
    if step["gate"]:
        # 审核门条目由收件箱聚合（kind=gate），这里只记时间线，不重复发 mention
        _step_activity(conn, wf["task_id"], role, step, "submitted", summary)
        conn.execute("UPDATE workflows SET updated_at=? WHERE id=?", (now, wid))
    else:
        conn.execute(
            "UPDATE workflow_steps SET status='approved', ended_at=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?", (now, now, wid, step_id))
        _step_activity(conn, wf["task_id"], role, step, "approved", summary)
    conn.commit()
    _finalize_if_done(conn, wid)
    return get_workflow(conn, wid)  # type: ignore[return-value]


def decide_step(conn: sqlite3.Connection, wid: str, step_id: str, role: str | None,
                approve: bool, feedback: str | None = None,
                run_id: str | None = None) -> dict:
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
        if step.get("kind") != "decision":
            raise ConflictError(f"step {step_id} is not a decision step")
        if step["status"] != "running":
            raise ConflictError(f"step {step_id} is {step['status']}, expected running")
        if role and role != "human" and role != step["role_slug"]:
            raise PermissionError(f"role {role} cannot decide step {step_id}")

        if step["run_id"]:
            # 直接 UPDATE，避免 runs.set_status 内部 commit 打断 BEGIN IMMEDIATE。
            run_row = conn.execute(
                "SELECT status FROM runs WHERE id=?", (step["run_id"],)).fetchone()
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
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (wf["task_id"], sender, "mention", jdumps({
                    "target": "human",
                    "message": f"决策步骤「{step['title']}」已超过最大重做次数"
                               f"（{max_rework} 次）；请人工介入。反馈：{note}",
                    "event": "rework_exhausted", "workflow_id": wid, "step_id": step_id,
                }), now))
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
                "updated_at=? WHERE workflow_id=? AND step_id=?",
                (note, now, wid, tid))
            target_role = target.get("role_slug") or ""
            if target_role:
                conn.execute(
                    "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (wf["task_id"], step["role_slug"], "mention", jdumps({
                        "target": target_role,
                        "message": f"决策打回步骤「{target.get('title', tid)}」：{note}",
                    }), now))
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "ended_at=NULL, summary=NULL, feedback=?, rework_count=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (note, count, now, wid, step_id))
        _step_activity(conn, wf["task_id"], step["role_slug"], step, "rework", note,
                       extra={"rework_count": count})
        conn.commit()
        return get_workflow(conn, wid)  # type: ignore[return-value]
    except BaseException:
        conn.rollback()
        raise


def review_step(conn: sqlite3.Connection, wid: str, step_id: str, approve: bool,
                feedback: str | None = None) -> dict:
    """人工审核 gate 步骤：通过则解锁下游；驳回则回到 pending 等待角色重做。"""
    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None:
        raise LookupError(step_id)
    if step.get("kind") == "decision":
        raise ConflictError("decision steps are decided by their role, not a human gate")
    if step["status"] != "submitted":
        raise ConflictError(f"step {step_id} is {step['status']}, expected submitted")
    now = now_iso()
    if approve:
        conn.execute(
            "UPDATE workflow_steps SET status='approved', ended_at=?, "
            "feedback=COALESCE(?, feedback), updated_at=? "
            "WHERE workflow_id=? AND step_id=?", (now, feedback, now, wid, step_id))
        _step_activity(conn, wf["task_id"], "human", step, "approved",
                       feedback or step.get("summary"))
    else:
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "summary=NULL, feedback=COALESCE(?, feedback), updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (feedback, now, wid, step_id))
        _step_activity(conn, wf["task_id"], "human", step, "rejected",
                       feedback or "请按评审意见修改后重新提交")
        from . import roles as roles_service
        _mention(
            conn, wf["task_id"], step["role_slug"],
            f"步骤「{step['title']}」被驳回：{feedback or '请按评审意见修改后重新提交'}。",
            sender=roles_service.notification_sender(
                conn, wf["task_id"], wf.get("created_by")))
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def retry_step(conn: sqlite3.Connection, wid: str, step_id: str) -> dict:
    """人工重试失败/驳回的步骤：重置为 pending，等待调度。"""
    now = now_iso()
    wf = get_workflow(conn, wid)
    step = next((s for s in (wf or {}).get("steps", []) if s["step_id"] == step_id), None)
    if step and step.get("kind") == "plan_slices":
        expanded = conn.execute(
            "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND source_step_id=? LIMIT 1",
            (wid, step_id)).fetchone()
        if expanded:
            raise ConflictError(
                "slice plan already expanded; create or re-dispatch the workflow to re-slice")
    conn.execute(
        "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, ended_at=NULL, "
        "summary=NULL, recovery_count=0, next_retry_at=NULL, nudge_count=0, last_nudge_at=NULL, "
        "updated_at=? WHERE workflow_id=? AND step_id=?",
        (now, wid, step_id))
    if step and step.get("kind") == "decision":
        conn.execute(
            "UPDATE workflow_steps SET rework_count=0 WHERE workflow_id=? AND step_id=?",
            (wid, step_id))
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def _retry_backoff_seconds(attempt: int) -> float:
    """指数退避：base, 2×base, 4×base… 上限 max。"""
    base = max(1.0, float(config.AUTO_RECOVER_BACKOFF_SECONDS))
    cap = max(base, float(config.AUTO_RECOVER_MAX_BACKOFF_SECONDS))
    return min(cap, base * (2 ** max(0, int(attempt) - 1)))


def fail_step(conn: sqlite3.Connection, wid: str, step_id: str, reason: str,
              category: str = "agent_error") -> None:
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
    recovery = int(step.get("recovery_count") or 0) + 1
    max_attempts = max(1, int(config.AUTO_RECOVER_MAX_ATTEMPTS))
    if config.AUTO_RECOVER_ENABLED and recovery <= max_attempts:
        delay = _retry_backoff_seconds(recovery)
        next_at = (datetime.now(timezone.utc)
                   + timedelta(seconds=delay)).isoformat()
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "ended_at=NULL, summary=?, failure_reason=?, recovery_count=?, next_retry_at=?, "
            "nudge_count=0, last_nudge_at=NULL, updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (reason, category, recovery, next_at, now, wid, step_id))
        _step_activity(conn, wf["task_id"], step["role_slug"], step, "retrying", reason,
                       extra={"failure_reason": category, "attempt": recovery,
                              "retry_at": next_at, "backoff_seconds": int(delay)})
        conn.commit()
        return
    conn.execute(
        "UPDATE workflow_steps SET status='failed', summary=?, failure_reason=?, "
        "recovery_count=?, next_retry_at=NULL, ended_at=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=?",
        (reason, category, recovery, now, now, wid, step_id))
    _step_activity(conn, wf["task_id"], step["role_slug"], step, "failed", reason,
                   extra={"failure_reason": category, "attempt": recovery})
    # 非阻塞通知：写 comment 而不是 @human。human 回来后在任务时间线能看到；
    # 但系统不等待人工确认才能继续（依赖此步骤的其它步骤仍按依赖规则等待）。
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (wf["task_id"], "workflow", "comment",
         jdumps({"body": f"工作流步骤「{step['title']}」（{step['role_slug']}）"
                          f"自动恢复 {recovery - 1} 次后仍失败：{reason}。"
                          "已保留现场，可在任务详情重试该步骤。",
                 "event": "step_failed", "workflow_id": wid, "step_id": step_id,
                 "auto_recovered": recovery - 1}),
         now),
    )
    conn.commit()


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


def _finalize_if_done(conn: sqlite3.Connection, wid: str) -> None:
    """所有步骤完成 → workflow done，任务进入最终人工评审。"""
    wf = get_workflow(conn, wid)
    if wf is None or wf["status"] != "running":
        return
    if not all(s["status"] in STEP_DONE for s in wf["steps"]):
        return
    now = now_iso()
    conn.execute("UPDATE workflows SET status='done', updated_at=? WHERE id=?", (now, wid))
    conn.execute(
        "UPDATE tasks SET status='in_review', lease_expires_at=NULL, updated_at=? "
        "WHERE id=? AND status IN ('in_progress','todo')", (now, wf["task_id"]))
    ledger.emit(
        conn, "execution.finished", task_id=wf["task_id"], workflow_id=wid,
        execution_no=wf.get("execution_no"), actor_type="system", actor="workflow",
        payload={"steps": len(wf.get("steps") or [])},
    )
    from . import roles as roles_service
    sender = roles_service.notification_sender(
        conn, wf["task_id"], wf.get("created_by"))
    _mention(conn, wf["task_id"], "human",
             "工作流全部步骤完成，请做最终评审（通过/驳回）", sender=sender)
    conn.commit()


def _mention(conn, task_id: str, target: str, message: str,
             sender: str | None = None) -> None:
    """系统通知：发送者必须是人类能 @ 回去的真实角色（planner/Cindy/assignee）。"""
    from . import roles as roles_service
    from . import tasks as tasks_service
    who = sender or roles_service.notification_sender(conn, task_id)
    tasks_service.mention(conn, task_id, who, target, message)
