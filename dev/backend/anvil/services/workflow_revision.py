"""Workflow 修订层：图预览、旁路步骤插入与运行中修订应用。"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, jloads, now_iso
from . import dossier, ledger, weights
from .workflow_common import (
    _transitive_downstream, get_workflow, workflow_lifecycle_lock,
)
from .workflow_definition import (
    SLICE_ID_RE, _dynamic_slice_steps, _effective_hint, _participant_roles,
    _validate_generated_deps, validate_role_coverage,
)


class RevisionConflict(ConflictError):
    """修订被拒绝；附带结构化影响报告，供 API/UI 展示。"""

    def __init__(self, impact: dict):
        super().__init__(str(impact.get("summary") or impact.get("classification")
                             or "workflow revision conflict"))
        self.impact = impact


def _step_is_mutable(step: dict) -> bool:
    return (step.get("status") == "pending"
            and not step.get("run_id") and not step.get("lease_id"))


def _slice_last_step(steps: list[dict], plan_step_id: str) -> str | None:
    if not steps:
        return None
    for step in reversed(steps):
        if step.get("step_id") == f"{plan_step_id}.{step.get('slice_key')}.decide":
            return step["step_id"]
    return steps[-1]["step_id"]


def _step_fields_from_patch(fields: dict) -> dict:
    allowed = {
        "title", "instructions", "acceptance", "contract", "role_slug",
        "depends_on", "scope_paths", "evidence_required", "subagent_hint",
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ConflictError(f"unsupported step fields in revision: {', '.join(unknown)}")
    return fields


def _external_revision_context(conn: sqlite3.Connection, wf: dict,
                               plan_step: dict, plan: dict, slices: list,
                               by_slice: dict[str, list[dict]], loop: dict,
                               external_nodes: list) -> tuple[dict, dict]:
    """分析「旁路验证节点」修订：节点不进入 loop，结果回链到 target slice/step。

    目标已经 submitted/approved 时退回 pending；restart_scope=downstream 会
    一并重跑其下游。目标或其下游正在 running 时拒绝，等 run 结束再提交。
    """
    from . import roles as roles_service

    def invalid(summary: str) -> tuple[dict, dict]:
        return {}, {"classification": "invalid", "safe": False,
                    "summary": summary, "blocked_by": [], "affected_steps": []}

    by_id = {s["step_id"]: s for s in wf["steps"]}
    slice_ids = [str(s.get("id") or "") for s in slices]
    plan_step_id = plan_step["step_id"]
    task_id = wf["task_id"]
    task_row = conn.execute(
        "SELECT assignee, status FROM tasks WHERE id=?", (task_id,)).fetchone()
    bound = {row["role_slug"] for row in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=?", (task_id,))}
    if task_row and task_row["assignee"]:
        bound.add(task_row["assignee"])
    participating = {str(s.get("role_slug") or "") for s in wf["steps"]}
    participating.update(
        str(spec.get("role") or "") for spec in (loop or {}).values()
        if isinstance(spec, dict))

    node_specs: list[dict] = []
    for raw in external_nodes:
        if not isinstance(raw, dict):
            return invalid("external node must be an object")
        slug = str(raw.get("id") or "").strip()
        if not slug or not SLICE_ID_RE.match(slug):
            return invalid(
                f"external node id {slug or '?'} must match {SLICE_ID_RE.pattern}")
        step_id = f"EXT.{slug}"
        if step_id in by_id or any(s["step_id"] == step_id for s in node_specs):
            return invalid(f"external node {slug} already exists")
        node_specs.append({"raw": raw, "slug": slug, "step_id": step_id})
    node_ids = {s["step_id"] for s in node_specs}

    blocked: list[dict] = []
    nodes: list[dict] = []
    restart: set[str] = set()
    target_entries: dict[str, list[str]] = {}

    for spec in node_specs:
        raw = spec["raw"]
        slug = spec["slug"]
        step_id = spec["step_id"]
        title = str(raw.get("title") or slug).strip() or slug
        role = str(raw.get("role") or "").strip()
        if not role:
            return invalid(f"external node {slug}: role is required")
        role_row = roles_service.get(conn, role)
        if role_row is None or not role_row.get("enabled", True) \
                or roles_service.is_onboarding(conn, role):
            return invalid(f"external node {slug}: invalid role {role}")
        if bound and role not in bound and role not in participating:
            return invalid(
                f"external node {slug}: role {role} is not bound to the task "
                "or current workflow")
        target_slice = str(raw.get("target_slice_id") or "").strip()
        target_step = str(raw.get("target_step_id") or "").strip()
        if bool(target_slice) == bool(target_step):
            return invalid(
                f"external node {slug}: set exactly one of target_slice_id "
                "or target_step_id")
        if target_slice:
            if target_slice not in slice_ids:
                return invalid(
                    f"external node {slug}: slice {target_slice} does not exist")
            target_steps = by_slice.get(target_slice, [])
            if not target_steps:
                return invalid(
                    f"external node {slug}: slice {target_slice} has no generated steps")
            entry = target_steps[0]["step_id"]
            target_ref = {"slice_id": target_slice}
        else:
            target_row = by_id.get(target_step)
            if target_row is None:
                return invalid(
                    f"external node {slug}: step {target_step} does not exist")
            if target_row.get("source_step_id") \
                    or target_step == plan_step_id \
                    or target_row.get("kind") == "external":
                return invalid(
                    f"external node {slug}: target_step_id must be a static step; "
                    "use target_slice_id for slice steps")
            target_steps = [target_row]
            entry = target_step
            target_ref = {"step_id": target_step}
        deps_raw = raw.get("depends_on")
        if deps_raw is None:
            deps = [plan_step_id]
        elif isinstance(deps_raw, list):
            deps = [str(x) for x in deps_raw]
        else:
            return invalid(f"external node {slug}: depends_on must be an array")
        if not deps:
            deps = [plan_step_id]
        for dep in deps:
            if dep not in by_id and dep not in node_ids:
                return invalid(
                    f"external node {slug}: depends_on missing step {dep}")
        if step_id in deps:
            return invalid(f"external node {slug}: cannot depend on itself")
        scope = str(raw.get("restart_scope") or "downstream").strip().lower()
        if scope not in ("target", "downstream"):
            return invalid(
                f"external node {slug}: restart_scope must be target|downstream")
        acceptance_raw = raw.get("acceptance") or []
        if not isinstance(acceptance_raw, list):
            return invalid(f"external node {slug}: acceptance must be an array")
        try:
            hint = _effective_hint(
                conn, role, raw.get("subagent_hint"),
                where=f"external node {slug}", task_id=task_id,
                workflow_id=wf.get("id"))
        except ConflictError as exc:
            return invalid(str(exc))

        reset_for_node: set[str] = set()

        def mark(step: dict) -> None:
            status = step.get("status") or "unknown"
            if status == "running":
                blocked.append({
                    "step_id": step["step_id"], "status": status,
                    "reason": "target or downstream step is running; "
                              "wait for it to finish",
                })
            elif status == "pending" and (
                    step.get("run_id") or step.get("lease_id")):
                blocked.append({
                    "step_id": step["step_id"], "status": status,
                    "reason": "target or downstream step is being dispatched; "
                              "wait for it to settle",
                })
            elif status in ("submitted", "approved"):
                reset_for_node.add(step["step_id"])

        for target_row in target_steps:
            mark(target_row)
        if scope == "downstream":
            roots = {s["step_id"] for s in target_steps}
            for sid in _transitive_downstream(wf["steps"], roots):
                step = by_id.get(sid) or {}
                if step.get("kind") == "external":
                    continue
                mark(step)
        restart |= reset_for_node
        target_entries.setdefault(entry, []).append(step_id)
        nodes.append({
            "step_id": step_id, "slug": slug, "title": title, "role": role,
            "instructions": str(raw.get("instructions") or ""),
            "acceptance": [str(x) for x in acceptance_raw],
            "contract": str(raw.get("contract") or "").strip() or None,
            "evidence_required": bool(raw.get("evidence_required")),
            "subagent_hint": hint,
            "depends_on": deps,
            "target_entry": entry,
            "target": target_ref,
            "restart_scope": scope,
        })

    if blocked:
        return {}, {
            "classification": "requires_restart", "safe": False,
            "summary": "目标或下游步骤正在运行，请等它们完成后再提交旁路验证节点",
            "blocked_by": blocked,
            "affected_steps": sorted(
                {x["step_id"] for x in blocked if x.get("step_id")}),
        }

    final_ids = set(by_id) | node_ids
    final_deps: dict[str, list[str]] = {}
    for step in wf["steps"]:
        deps = [str(d) for d in (step.get("depends_on") or [])]
        for ext_id in target_entries.get(step["step_id"], []):
            if ext_id not in deps:
                deps.append(ext_id)
        final_deps[step["step_id"]] = deps
    for node in nodes:
        final_deps[node["step_id"]] = list(node["depends_on"])
    final_on_reject = {
        step["step_id"]: [str(t) for t in (step.get("on_reject") or [])]
        for step in wf["steps"]}
    for node in nodes:
        final_on_reject[node["step_id"]] = []
    try:
        _validate_dep_graph(final_ids, final_deps, final_on_reject)
    except ConflictError as exc:
        return invalid(str(exc))

    restart_ids = sorted(restart)
    summary = f"新增 {len(nodes)} 个旁路验证节点"
    if restart_ids:
        summary += f"；将重跑 {len(restart_ids)} 个步骤"
    else:
        summary += "；目标无需重置"
    impact = {
        "classification": "safe_external", "safe": True,
        "summary": summary, "blocked_by": [],
        "affected_steps": sorted(node_ids | restart | set(target_entries)),
        "diff": {
            "external_nodes": [node["step_id"] for node in nodes],
            "restart_steps": restart_ids,
            "targets": sorted(target_entries),
        },
    }
    state = {
        "mode": "external", "nodes": nodes, "restart_steps": restart_ids,
        "target_entries": target_entries, "plan_step": plan_step,
        "plan": plan, "slices": slices, "task_id": task_id,
        "workflow_was_done": wf.get("status") == "done",
        "task_was_in_review": bool(
            task_row and task_row["status"] == "in_review"),
    }
    return state, impact


def _revision_context(conn: sqlite3.Connection, wf: dict, intent: dict) -> tuple[dict, dict]:
    """计算修订后的切片/步骤结构；返回 (state, impact)。

    state 只在 impact.safe=True 时可用于 apply；impact 始终可直接返回给 API/UI。
    """
    if not isinstance(intent, dict):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "revision intent must be an object",
                    "blocked_by": [], "affected_steps": []}
    external_nodes = intent.get("external_nodes")
    if external_nodes is not None and not isinstance(external_nodes, list):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "external_nodes must be an array",
                    "blocked_by": [], "affected_steps": []}
    external_only = bool(external_nodes)
    if wf.get("status") != "running" and not (
            external_only and wf.get("status") == "done"):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": f"workflow is {wf.get('status')}, not running",
                    "blocked_by": [], "affected_steps": []}
    task_row = conn.execute(
        "SELECT status, archived_at FROM tasks WHERE id=?",
        (wf["task_id"],)).fetchone()
    if (task_row is None or task_row["status"] in ("done", "closed")
            or task_row["archived_at"]):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "task is not active; reopen it before revising",
                    "blocked_by": [], "affected_steps": []}
    plan_step = next((s for s in wf["steps"] if s.get("kind") == "plan_slices"), None)
    if plan_step is None or plan_step.get("status") != "approved":
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "workflow has no approved plan_slices step",
                    "blocked_by": [], "affected_steps": []}
    plan = jloads(plan_step.get("plan_json") or "{}", {})
    slices = plan.get("slices") if isinstance(plan, dict) else None
    if not isinstance(slices, list) or not slices:
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "plan has no slices; use a full workflow draft",
                    "blocked_by": [], "affected_steps": []}
    loop = (wf.get("definition") or {}).get("loop")
    if not isinstance(loop, dict):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "workflow has no loop template",
                    "blocked_by": [], "affected_steps": []}

    by_slice: dict[str, list[dict]] = {}
    for step in wf["steps"]:
        if step.get("source_step_id") == plan_step["step_id"]:
            by_slice.setdefault(str(step.get("slice_key") or ""), []).append(step)
    for raw in slices:
        sid = str(raw.get("id") or "")
        if not sid or sid not in by_slice:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"slice {sid or '?'} has no generated steps",
                        "blocked_by": [], "affected_steps": []}

    mutable_flags = [
        all(_step_is_mutable(s) for s in by_slice[str(raw.get("id"))])
        for raw in slices
    ]
    last_locked = max((i for i, ok in enumerate(mutable_flags) if not ok), default=-1)
    mutable_from = last_locked + 1
    stable_slices = slices[:mutable_from]
    mutable_slices = slices[mutable_from:]
    stable_ids = {st["step_id"] for st in wf["steps"]
                  if st.get("source_step_id") != plan_step["step_id"]}
    stable_ids.update(
        st["step_id"] for raw in stable_slices
        for st in by_slice.get(str(raw.get("id")), []))
    old_last_decide = _slice_last_step(
        by_slice[str(slices[-1].get("id"))], plan_step["step_id"])
    last_stable_decide = (
        _slice_last_step(by_slice[str(stable_slices[-1].get("id"))],
                         plan_step["step_id"]) if stable_slices else None)

    blocked: list[dict] = []

    def block(step_id: str | None, status: str, reason: str) -> None:
        blocked.append({"step_id": step_id, "status": status, "reason": reason})

    add_slices = intent.get("add_slices") or []
    raw_remove = intent.get("remove_slices") or []
    updates = intent.get("update_slices") or []
    patch_steps = intent.get("patch_steps") or []
    if not isinstance(add_slices, list) or not isinstance(raw_remove, list) \
            or not isinstance(updates, list) or not isinstance(patch_steps, list):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "revision lists must be arrays",
                    "blocked_by": [], "affected_steps": []}
    if any(not isinstance(raw, dict) for raw in (*add_slices, *updates)):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "add_slices/update_slices entries must be objects",
                    "blocked_by": [], "affected_steps": []}
    for raw in add_slices:
        if "execute_steps" in raw and not isinstance(
                raw.get("execute_steps"), (list, dict)):
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"slice {raw.get('id') or '?'} execute_steps "
                                   "must be an array or object",
                        "blocked_by": [], "affected_steps": []}
        if "external_deps" in raw:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": "external_deps is controlled by external_nodes; "
                                   "use an external node instead",
                        "blocked_by": [], "affected_steps": []}
        if raw.get("enabling") or "enables" in raw:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": "enabling slices can only be declared at plan "
                                   "approval; runtime revision cannot add or enable them",
                        "blocked_by": [], "affected_steps": []}
    for item in updates:
        patch = item.get("patch") or {}
        if not isinstance(patch, dict):
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"slice {item.get('id') or '?'} patch must be an object",
                        "blocked_by": [], "affected_steps": []}
        if "execute_steps" in patch and not isinstance(
                patch.get("execute_steps"), (list, dict)):
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"slice {item.get('id') or '?'} execute_steps "
                                   "must be an array or object",
                        "blocked_by": [], "affected_steps": []}
    if external_only:
        if add_slices or raw_remove or updates or patch_steps:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": "external_nodes cannot be combined with "
                                   "slice or step changes",
                        "blocked_by": [], "affected_steps": []}
        return _external_revision_context(
            conn, wf, plan_step, plan, slices, by_slice, loop, external_nodes)
    if not add_slices and not raw_remove and not updates and not patch_steps:
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "revision intent has no changes",
                    "blocked_by": [], "affected_steps": []}
    remove_ids = [str(x) for x in raw_remove]
    has_slices = bool(add_slices or remove_ids or updates)

    old_ids = [str(raw.get("id")) for raw in slices]
    external_slice_targets = {
        str((entry.get("target") or {}).get("slice_id") or "")
        for entry in (plan.get("external") or []) if isinstance(entry, dict)
    }
    for sid in remove_ids + [str((u or {}).get("id") or "") for u in updates
                             if isinstance(u, dict)]:
        if sid not in old_ids:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"slice {sid} does not exist",
                        "blocked_by": [], "affected_steps": []}
        if sid in remove_ids and sid in external_slice_targets:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"slice {sid} is linked to an external "
                                   "verification node and cannot be removed",
                        "blocked_by": [], "affected_steps": []}
        idx = old_ids.index(sid)
        if idx < mutable_from:
            raw_steps = by_slice.get(sid, [])
            status = raw_steps[0].get("status") if raw_steps else "unknown"
            block(sid, status, "slice has started or completed")
    new_ids = [str((raw or {}).get("id") or "") for raw in add_slices
               if isinstance(raw, dict)]
    if any(not sid for sid in new_ids):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "added slice needs an id",
                    "blocked_by": [], "affected_steps": []}
    if len(set(new_ids)) != len(new_ids) or any(sid in old_ids for sid in new_ids):
        return {}, {"classification": "invalid", "safe": False,
                    "summary": "duplicate slice id in revision",
                    "blocked_by": [], "affected_steps": []}
    role_keys = {"role", "execute_role", "verify_role", "fix_role",
                 "verify2_role", "decision_role", "lead_role", "reviewer"}
    for raw in add_slices:
        if isinstance(raw, dict) and role_keys.intersection(raw):
            block(str(raw.get("id") or "?"), "new", "slice role fields are owned by the loop template")
    for item in updates:
        if isinstance(item, dict) and role_keys.intersection(item.get("patch") or {}):
            block(str(item.get("id") or "?"), "update", "slice role fields are owned by the loop template")

    # 构建新的尾部切片：update 合并整片 spec，remove 删除，add 追加。
    # 仅 patch 静态步骤时不重建切片，避免无谓重置 pending 步骤的调度状态。
    if has_slices:
        tail = [dict(raw) for raw in mutable_slices]
        remove_set = set(remove_ids)
        tail = [raw for raw in tail if str(raw.get("id")) not in remove_set]
        for item in updates:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "")
            patch = item.get("patch") or {}
            if not isinstance(patch, dict):
                return {}, {"classification": "invalid", "safe": False,
                            "summary": f"slice {sid} patch must be an object",
                            "blocked_by": [], "affected_steps": []}
            if "id" in patch and str(patch["id"]) != sid:
                return {}, {"classification": "invalid", "safe": False,
                            "summary": "update_slices cannot change slice id; "
                                       "remove and add a new slice instead",
                            "blocked_by": [], "affected_steps": []}
            if "external_deps" in patch:
                return {}, {"classification": "invalid", "safe": False,
                            "summary": "external_deps is controlled by external_nodes; "
                                       "use an external node instead",
                            "blocked_by": [], "affected_steps": []}
            if patch.get("enabling") or "enables" in patch:
                return {}, {"classification": "invalid", "safe": False,
                            "summary": "enabling slices can only be declared at plan "
                                       "approval; runtime revision cannot add or enable them",
                            "blocked_by": [], "affected_steps": []}
            for raw in tail:
                if str(raw.get("id")) == sid:
                    raw.update(patch)
        tail.extend([dict(raw) for raw in add_slices if isinstance(raw, dict)])
        new_slices = [dict(raw) for raw in stable_slices] + tail
        if not new_slices:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": "revision cannot remove all slices",
                        "blocked_by": [], "affected_steps": []}

        # 续接稳定前缀；生成尾部步骤用于校验/后续 apply。
        try:
            generated = _dynamic_slice_steps(
                conn, plan_step["step_id"], loop, tail, stable_ids,
                weight=str(wf.get("weight") or "standard"),
                previous_decide=last_stable_decide,
                known_slice_ids={str(raw.get("id") or "") for raw in slices})
            _validate_generated_deps(generated, stable_ids)
        except ConflictError as exc:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": str(exc), "blocked_by": [], "affected_steps": []}
    else:
        tail = []
        generated = []
        new_slices = [dict(raw) for raw in slices]

    # 静态下游：切片变更时只能改 pending；active/settled 说明最终步骤已经开跑。
    static_steps = [s for s in wf["steps"]
                    if not s.get("source_step_id") and s["step_id"] != plan_step["step_id"]
                    and s.get("kind") != "external"]
    old_tail_decide = old_last_decide
    if has_slices and old_tail_decide:
        for st in static_steps:
            if old_tail_decide in (st.get("depends_on") or []) and not _step_is_mutable(st):
                block(st["step_id"], st.get("status") or "unknown",
                      "downstream static step already started")

    # patch 只允许 pending 静态步骤；切片步骤通过 slice intent 修改。
    patched: dict[str, dict] = {}
    by_id = {s["step_id"]: s for s in wf["steps"]}
    for item in patch_steps:
        if not isinstance(item, dict):
            return {}, {"classification": "invalid", "safe": False,
                        "summary": "step patch must be an object",
                        "blocked_by": [], "affected_steps": []}
        sid = str(item.get("step_id") or "")
        step = by_id.get(sid)
        if step is None:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"step {sid} does not exist",
                        "blocked_by": [], "affected_steps": []}
        if step.get("source_step_id"):
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"step {sid} belongs to a slice; use update_slices",
                        "blocked_by": [], "affected_steps": []}
        if step.get("kind") == "external":
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"step {sid} is an external node; use external_nodes",
                        "blocked_by": [], "affected_steps": []}
        if not _step_is_mutable(step):
            block(sid, step.get("status") or "unknown",
                  "only pending steps without run/lease can be patched")
            continue
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"step {sid} fields must be an object",
                        "blocked_by": [], "affected_steps": []}
        if not fields:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": f"step {sid} patch has no fields",
                        "blocked_by": [], "affected_steps": []}
        for key in ("depends_on", "acceptance", "scope_paths"):
            if key in fields and fields[key] is not None \
                    and not isinstance(fields[key], list):
                return {}, {"classification": "invalid", "safe": False,
                            "summary": f"step {sid} {key} must be an array",
                            "blocked_by": [], "affected_steps": []}
        try:
            _step_fields_from_patch(fields)
        except ConflictError as exc:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": str(exc), "blocked_by": [], "affected_steps": []}
        if "role_slug" in fields:
            from . import roles as roles_service
            role_slug = str(fields["role_slug"])
            role = roles_service.get(conn, role_slug)
            if role is None or not role.get("enabled", True) \
                    or roles_service.is_onboarding(conn, role["slug"]):
                return {}, {"classification": "invalid", "safe": False,
                            "summary": f"invalid step role {fields['role_slug']}",
                            "blocked_by": [], "affected_steps": []}
            bound = {row["role_slug"] for row in conn.execute(
                "SELECT role_slug FROM task_assignees WHERE task_id=?",
                (wf["task_id"],))}
            task_row = conn.execute(
                "SELECT assignee FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
            if task_row and task_row["assignee"]:
                bound.add(task_row["assignee"])
            participating = {
                str(s.get("role_slug") or "") for s in wf["steps"]}
            participating.update(
                str(spec.get("role") or "") for spec in loop.values()
                if isinstance(spec, dict))
            if bound and role_slug not in bound and role_slug not in participating:
                return {}, {"classification": "invalid", "safe": False,
                            "summary": f"step {sid} role {role_slug} is not bound "
                                       "to the task or current workflow",
                            "blocked_by": [], "affected_steps": []}
        if "depends_on" in fields:
            deps = [str(x) for x in (fields["depends_on"] or [])]
            final_ids = ((set(stable_ids) | {g["id"] for g in generated})
                         if has_slices
                         else {s["step_id"] for s in wf["steps"]})
            missing = [d for d in deps if d not in final_ids]
            if missing:
                return {}, {"classification": "invalid", "safe": False,
                            "summary": f"step {sid} depends on missing {missing[0]}",
                            "blocked_by": [], "affected_steps": []}
            fields = {**fields, "depends_on": deps}
        patched[sid] = fields

    old_mutable_ids = ([
        st["step_id"] for raw in mutable_slices
        for st in by_slice.get(str(raw.get("id")), [])] if has_slices else [])

    if blocked:
        return {}, {"classification": "requires_restart", "safe": False,
                    "summary": "修订影响已开始/已完成的步骤，需重启或放弃；"
                               "如只是想补验证节点，可改用 external_nodes",
                    "blocked_by": blocked,
                    "affected_steps": sorted({x["step_id"] for x in blocked if x["step_id"]})}

    # 预览阶段校验 patch/切片后的完整依赖图与 on_reject 目标，避免批准后才失败。
    final_ids = ((set(stable_ids) | {g["id"] for g in generated})
                 if has_slices else {s["step_id"] for s in wf["steps"]})
    old_mutable = set(old_mutable_ids)
    # 与 apply 一致：静态下游从旧最后 decide 改挂到新最后 decide。
    new_last_decide = last_stable_decide
    for g in generated:
        if str(g.get("kind") or "task") == "decision":
            new_last_decide = g["id"]
    final_deps: dict[str, list[str]] = {}
    final_on_reject: dict[str, list[str]] = {}
    for s in wf["steps"]:
        if s["step_id"] in old_mutable:
            continue
        if s.get("kind") == "external":
            final_deps[s["step_id"]] = [
                str(d) for d in (s.get("depends_on") or [])]
            final_on_reject[s["step_id"]] = [
                str(t) for t in s.get("on_reject") or []]
            continue
        fields = patched.get(s["step_id"]) or {}
        deps = (fields["depends_on"] if "depends_on" in fields
                else list(s.get("depends_on") or []))
        if (has_slices and "depends_on" not in fields and old_last_decide
                and new_last_decide):
            deps = [new_last_decide if str(d) == str(old_last_decide) else d
                    for d in deps]
        final_deps[s["step_id"]] = [str(d) for d in deps]
        final_on_reject[s["step_id"]] = [
            str(t) for t in s.get("on_reject") or []]
    for g in generated:
        final_deps[g["id"]] = [str(d) for d in g.get("depends_on") or []]
        final_on_reject[g["id"]] = [str(t) for t in g.get("on_reject") or []]
    try:
        _validate_dep_graph(final_ids, final_deps, final_on_reject)
    except ConflictError as exc:
        return {}, {"classification": "invalid", "safe": False,
                    "summary": str(exc), "blocked_by": [], "affected_steps": []}

    # 改静态步骤角色后，任务绑定的每个角色仍必须出现在 workflow 中。
    patched_roles = {sid: str(fields["role_slug"]) for sid, fields in patched.items()
                     if "role_slug" in fields}
    if patched_roles:
        definition = dict(wf.get("definition") or {})
        dsteps = [
            dict(s) if isinstance(s, dict) else s
            for s in (definition.get("steps") or definition.get("children") or [])]
        for dstep in dsteps:
            if isinstance(dstep, dict) and str(dstep.get("id")) in patched_roles:
                dstep["role"] = patched_roles[str(dstep.get("id"))]
        definition["steps"] = dsteps
        try:
            validate_role_coverage(conn, wf.get("task_id"), definition)
        except ConflictError as exc:
            return {}, {"classification": "invalid", "safe": False,
                        "summary": str(exc), "blocked_by": [],
                        "affected_steps": sorted(patched_roles)}

    affected = sorted(set(old_mutable_ids) | {g["id"] for g in generated}
                      | set(patched))
    classification = "safe_loop" if has_slices else "safe_future"
    summary = []
    if add_slices:
        summary.append(f"追加 {len(add_slices)} 个切片")
    if remove_ids:
        summary.append(f"删除 {len(remove_ids)} 个切片")
    if updates:
        summary.append(f"修改 {len(updates)} 个切片")
    if patched:
        summary.append(f"修改 {len(patched)} 个未来步骤")
    if not summary:
        summary.append("无实际变更")
    impact = {
        "classification": classification, "safe": True,
        "summary": "；".join(summary) + "；当前执行不受影响",
        "blocked_by": [], "affected_steps": affected,
        "diff": {"add_slices": add_slices, "remove_slices": remove_ids,
                 "update_slices": updates, "patch_steps": list(patched)},
    }
    state = {
        "impact": impact, "plan_step": plan_step, "plan": plan,
        "old_slices": slices, "new_slices": new_slices,
        "mutable_slices": mutable_slices, "generated": generated,
        "old_mutable_ids": old_mutable_ids, "patched": patched,
        "old_last_decide": old_last_decide, "last_stable_decide": last_stable_decide,
        "static_steps": static_steps, "stable_slices": stable_slices,
        "tail": tail, "has_slices": has_slices,
    }
    return state, impact


def preview_revision(conn: sqlite3.Connection, wid: str, intent: dict) -> dict:
    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    _state, impact = _revision_context(conn, wf, intent)
    return impact


def _validate_dep_graph(ids: set[str], deps: dict[str, list[str]],
                        on_rejects: dict[str, list[str]]) -> None:
    for sid, ds in deps.items():
        for dep in ds:
            if str(dep) not in ids:
                raise ConflictError(f"step {sid} depends on missing step {dep}")
    for sid, targets in on_rejects.items():
        for target in targets:
            if str(target) not in ids:
                raise ConflictError(f"step {sid} on_reject missing {target}")
    state: dict[str, int] = {}

    def visit(sid: str, stack: list[str]) -> None:
        if state.get(sid) == 1:
            raise ConflictError("workflow revision creates a cycle: "
                                + " -> ".join(stack + [sid]))
        if state.get(sid) == 2:
            return
        state[sid] = 1
        for dep in deps.get(sid, []):
            visit(str(dep), stack + [sid])
        state[sid] = 2

    for sid in ids:
        visit(sid, [])


def _validate_runtime_graph(conn: sqlite3.Connection, wid: str) -> None:
    rows = conn.execute(
        "SELECT step_id, depends_on_json, on_reject_json FROM workflow_steps "
        "WHERE workflow_id=?", (wid,)).fetchall()
    _validate_dep_graph(
        {r["step_id"] for r in rows},
        {r["step_id"]: [str(d) for d in jloads(r["depends_on_json"], [])]
         for r in rows},
        {r["step_id"]: [str(t) for t in jloads(r["on_reject_json"], [])]
         for r in rows},
    )


def _insert_revision_steps(conn: sqlite3.Connection, wid: str,
                           generated: list[dict], now: str) -> None:
    task_row = conn.execute(
        "SELECT task_id FROM workflows WHERE id=?", (wid,)).fetchone()
    task_id = task_row["task_id"] if task_row else None
    for raw_step in generated:
        hint = _effective_hint(
            conn, raw_step.get("role"), raw_step.get("subagent_hint"),
            where=f"revision step {raw_step['id']}", task_id=task_id,
            workflow_id=wid)
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, "
            "instructions, depends_on_json, acceptance_json, gate, reviewer, contract, "
            "kind, on_reject_json, max_rework, rework_count, source_step_id, slice_key, "
            "evidence_required, subagent_hint, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
             1 if raw_step.get("evidence_required") else 0,
             hint,
             "pending", now),
        )


def _apply_external_revision(conn: sqlite3.Connection, wf: dict, state: dict,
                             impact: dict, actor: str, draft_id: str | None,
                             now: str) -> dict:
    """应用旁路验证节点：插入节点、回链目标、重置需要重跑的目标/下游。"""
    wid = wf["id"]
    nodes = state["nodes"]
    for node in nodes:
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, "
            "instructions, depends_on_json, acceptance_json, gate, reviewer, contract, "
            "kind, on_reject_json, max_rework, rework_count, source_step_id, slice_key, "
            "evidence_required, subagent_hint, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, node["step_id"], node["title"], node["role"],
             node["instructions"], jdumps(node["depends_on"]),
             jdumps(node["acceptance"]), 0, None, node["contract"],
             "external", jdumps([]), 0, 0, None, None,
             1 if node["evidence_required"] else 0, node["subagent_hint"],
             "pending", now),
        )
    for entry, ext_ids in state["target_entries"].items():
        row = conn.execute(
            "SELECT depends_on_json FROM workflow_steps "
            "WHERE workflow_id=? AND step_id=?", (wid, entry)).fetchone()
        deps = jloads(row["depends_on_json"], []) if row else []
        for ext_id in ext_ids:
            if ext_id not in deps:
                deps.append(ext_id)
        conn.execute(
            "UPDATE workflow_steps SET depends_on_json=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (jdumps(deps), now, wid, entry))
    for sid in state["restart_steps"]:
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "ended_at=NULL, summary=NULL, recovery_count=0, next_retry_at=NULL, "
            "nudge_count=0, last_nudge_at=NULL, lease_id=NULL, fence_token=0, "
            "updated_at=? WHERE workflow_id=? AND step_id=?",
            (now, wid, sid))
        conn.execute(
            "UPDATE gate_packets SET status='superseded' "
            "WHERE workflow_id=? AND step_id=? "
            "AND status IN ('pending_human','pending_ai')",
            (wid, sid))
        conn.execute(
            "UPDATE workflow_steps SET rework_count=0 "
            "WHERE workflow_id=? AND step_id=? AND kind='decision'", (wid, sid))
    if state.get("workflow_was_done"):
        conn.execute(
            "UPDATE workflows SET status='running', finished_at=NULL, updated_at=? "
            "WHERE id=?", (now, wid))
    else:
        conn.execute(
            "UPDATE workflows SET updated_at=? WHERE id=?", (now, wid))
    if state.get("task_was_in_review"):
        from . import tasks as tasks_service
        tasks_service.transition_task(
            conn, state["task_id"], "in_progress",
            from_statuses=("in_review",),
            fields={"lease_expires_at": None}, now=now)
        conn.execute(
            "UPDATE task_role_claims SET status='working', submitted_at=NULL, updated_at=? "
            "WHERE task_id=? AND status IN ('submitted','approved')",
            (now, state["task_id"]))
    plan = dict(state["plan"] or {})
    external = plan.get("external") if isinstance(plan.get("external"), list) else []
    external.extend({
        "id": node["slug"], "step_id": node["step_id"], "title": node["title"],
        "role": node["role"], "target": node["target"],
        "restart_scope": node["restart_scope"],
    } for node in nodes)
    plan["external"] = external
    # 把切片目标的 external_deps 写进 plan，后续普通修订重建该切片时仍然保留链接。
    plan_slices = plan.get("slices") if isinstance(plan.get("slices"), list) else []
    new_slices: list = []
    for raw in plan_slices:
        if isinstance(raw, dict):
            raw = dict(raw)
            sid = str(raw.get("id") or "")
            for node in nodes:
                if str((node.get("target") or {}).get("slice_id") or "") != sid:
                    continue
                deps = [str(x) for x in (raw.get("external_deps") or [])]
                if node["step_id"] not in deps:
                    deps.append(node["step_id"])
                raw["external_deps"] = deps
        new_slices.append(raw)
    plan["slices"] = new_slices
    old_summary = str(state["plan_step"].get("summary") or "").strip()
    summary = (old_summary + "；" if old_summary else "") + \
        "外挂节点：" + "、".join(node["title"] for node in nodes)
    conn.execute(
        "UPDATE workflow_steps SET plan_json=?, summary=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=?",
        (jdumps(plan), summary, now, wid, state["plan_step"]["step_id"]))
    _validate_runtime_graph(conn, wid)
    if draft_id:
        cur = conn.execute(
            "UPDATE board_drafts SET status='approved', decided_at=? "
            "WHERE id=? AND status='pending'", (now, draft_id))
        if cur.rowcount != 1:
            raise ConflictError("draft state changed; refresh before approving")
    ledger.emit_activity(
        conn, task_id=state["task_id"],
        role_slug=actor,
        kind="comment",
        payload=jdumps({"body": f"应用工作流修订：{impact['summary']}。"
                         "旁路节点完成后，目标步骤会等待其结果并继续执行。",
                 "event": "workflow_revised", "workflow_id": wid,
                 "draft_id": draft_id,
                 "external_nodes": [node["step_id"] for node in nodes],
                 "restart_steps": state["restart_steps"]}),
        now=now)
    ledger.emit(
        conn, "workflow.revised", task_id=state["task_id"], workflow_id=wid,
        execution_no=wf.get("execution_no"),
        actor_type="human" if actor == "human" else "role", actor=actor,
        payload={"draft_id": draft_id, "impact": impact,
                 "definition_hash": wf.get("definition_hash"),
                 "external_nodes": [node["step_id"] for node in nodes],
                 "restart_steps": state["restart_steps"]})
    dossier.persist(conn, wid, commit=False)
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def apply_revision(conn: sqlite3.Connection, wid: str, intent: dict,
                   actor: str = "human", draft_id: str | None = None) -> dict:
    """在运行中的 execution 内应用安全修订；不新建 execution、不终止 run。"""
    wf = get_workflow(conn, wid)
    task_id = wf["task_id"] if wf else None
    # task 级生命周期锁：避免 scheduler 读到旧步骤后又在修订删改后写回。
    with workflow_lifecycle_lock(
            conn, task_id, required=True, wait_seconds=2.0):
        return _apply_revision_locked(conn, wid, intent, actor, draft_id)


def _revision_introduced_roles(state: dict) -> set[str]:
    roles: set[str] = set()
    for generated in state.get("generated") or []:
        if isinstance(generated, dict) and generated.get("role"):
            roles.add(str(generated["role"]).strip())
    for fields in (state.get("patched") or {}).values():
        if isinstance(fields, dict) and fields.get("role_slug"):
            roles.add(str(fields["role_slug"]).strip())
    for node in state.get("nodes") or []:
        if isinstance(node, dict) and node.get("role"):
            roles.add(str(node["role"]).strip())
    return {role for role in roles if role}


def _sync_revision_participants(conn: sqlite3.Connection, wf: dict,
                                state: dict) -> list[str]:
    """修订新增角色必须在应用前成为任务参与者，否则后续委派会 403。"""
    from . import roles as roles_service
    from . import tasks as tasks_service

    roles = _revision_introduced_roles(state)
    participants = _participant_roles(conn, wf["task_id"])
    # 无参与者的旧数据/测试工作流不做参与者同步（没有 human 指定角色可对齐）。
    if not participants:
        return []
    add = sorted(roles - participants)
    if not add:
        return []
    for role in add:
        row = roles_service.get(conn, role)
        if row is None or not row.get("enabled", True):
            raise ConflictError(
                f"revision references unknown or disabled role: {role}")
    tasks_service.add_assignees(conn, wf["task_id"], add)
    ledger.emit(
        conn, "workflow.participants_synced", task_id=wf["task_id"],
        workflow_id=wf["id"], actor_type="system", actor="revision",
        payload={"added": add, "source": "revision"})
    return add


def _apply_revision_locked(conn: sqlite3.Connection, wid: str, intent: dict,
                           actor: str, draft_id: str | None) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if draft_id:
            draft_row = conn.execute(
                "SELECT status FROM board_drafts WHERE id=?", (draft_id,)).fetchone()
            if draft_row is None or draft_row["status"] != "pending":
                raise ConflictError(
                    "draft state changed; refresh before approving")
        wf = get_workflow(conn, wid)
        if wf is None:
            raise LookupError(wid)
        state, impact = _revision_context(conn, wf, intent)
        if not impact.get("safe"):
            # 草案创建时已分类为 safe；批准时重新分类失败，说明目标在待审期间推进
            # 或状态改变。按契约返回 stale（不是创建阶段的 requires_restart/invalid），
            # 客户端据此重新读取状态并重新预览。
            impact = {
                **impact,
                "classification": "stale",
                "previous_classification": impact.get("classification"),
                "summary": "修订目标在待审期间已推进或状态改变，请重新预览："
                           + str(impact.get("summary") or ""),
            }
            raise RevisionConflict(impact)

        _sync_revision_participants(conn, wf, state)

        if state.get("mode") == "external":
            return _apply_external_revision(
                conn, wf, state, impact, actor, draft_id, now_iso())

        plan_step = state["plan_step"]
        old_ids = state["old_mutable_ids"]
        if old_ids:
            conn.execute(
                "DELETE FROM workflow_steps WHERE workflow_id=? AND source_step_id=? "
                "AND step_id IN (" + ",".join("?" * len(old_ids)) + ")",
                (wid, plan_step["step_id"], *old_ids))
        now = now_iso()
        _insert_revision_steps(conn, wid, state["generated"], now)

        # 静态下游从旧最后 decide 改挂到新最后 decide（仅切片变更时）。
        new_last_decide = state["last_stable_decide"]
        for g in state["generated"]:
            if str(g.get("kind") or "task") == "decision":
                new_last_decide = g["id"]
        old_last = state["old_last_decide"]
        if state["has_slices"] and old_last:
            for st in state["static_steps"]:
                deps = list(st.get("depends_on") or [])
                if old_last not in deps:
                    continue
                new_deps = [new_last_decide if str(d) == str(old_last) else d
                            for d in deps if new_last_decide or str(d) != str(old_last)]
                conn.execute(
                    "UPDATE workflow_steps SET depends_on_json=?, updated_at=? "
                    "WHERE workflow_id=? AND step_id=?",
                    (jdumps(new_deps), now, wid, st["step_id"]))

        # pending 静态步骤 patch + 同步 definition（保证 planned graph/dossier 一致）。
        definition = dict(wf.get("definition") or {})
        definition_steps = (definition.get("steps")
                            or definition.get("children") or [])
        def_by_id = {str(s.get("id")): s for s in definition_steps
                     if isinstance(s, dict)}
        for sid, fields in state["patched"].items():
            db_fields: dict = {}
            if "title" in fields:
                db_fields["title"] = str(fields["title"])
            if "instructions" in fields:
                db_fields["instructions"] = str(fields["instructions"])
            if "acceptance" in fields:
                db_fields["acceptance_json"] = jdumps(fields["acceptance"] or [])
            if "contract" in fields:
                db_fields["contract"] = str(fields["contract"]) or None
            if "role_slug" in fields:
                db_fields["role_slug"] = str(fields["role_slug"])
            if "depends_on" in fields:
                runtime_deps = [str(x) for x in (fields["depends_on"] or [])]
                for entry in (state.get("plan") or {}).get("external") or []:
                    if not isinstance(entry, dict):
                        continue
                    target = entry.get("target") or {}
                    if str(target.get("step_id") or "") != sid:
                        continue
                    ext_id = str(entry.get("step_id") or "")
                    if ext_id and ext_id not in runtime_deps:
                        runtime_deps.append(ext_id)
                db_fields["depends_on_json"] = jdumps(runtime_deps)
            if "scope_paths" in fields:
                db_fields["scope_paths_json"] = jdumps(fields["scope_paths"] or [])
            if "evidence_required" in fields:
                db_fields["evidence_required"] = 1 if fields["evidence_required"] else 0
            if "subagent_hint" in fields:
                hint_role = str(
                    fields.get("role_slug")
                    or (def_by_id.get(sid) or {}).get("role") or "")
                db_fields["subagent_hint"] = _effective_hint(
                    conn, hint_role, fields["subagent_hint"],
                    where=f"revision patch {sid}", task_id=wf.get("task_id"),
                    workflow_id=wid)
            if db_fields:
                sets = ", ".join(f"{k}=?" for k in db_fields)
                conn.execute(
                    f"UPDATE workflow_steps SET {sets}, updated_at=? "
                    "WHERE workflow_id=? AND step_id=?",
                    (*db_fields.values(), now, wid, sid))
                dstep = def_by_id.get(sid)
                if dstep is not None:
                    if "title" in fields:
                        dstep["title"] = str(fields["title"])
                    if "instructions" in fields:
                        dstep["instructions"] = str(fields["instructions"])
                    if "acceptance" in fields:
                        dstep["acceptance"] = fields["acceptance"] or []
                    if "contract" in fields:
                        dstep["contract"] = str(fields["contract"]) or ""
                    if "role_slug" in fields:
                        dstep["role"] = str(fields["role_slug"])
                    if "depends_on" in fields:
                        dstep["depends_on"] = fields["depends_on"] or []
                    if "scope_paths" in fields:
                        dstep["scope_paths"] = fields["scope_paths"] or []
                    if "evidence_required" in fields:
                        dstep["evidence_required"] = bool(fields["evidence_required"])
                    if "subagent_hint" in fields:
                        dstep["subagent_hint"] = bool(
                            db_fields.get("subagent_hint"))

        _validate_runtime_graph(conn, wid)
        plan = dict(state["plan"] or {})
        plan["slices"] = state["new_slices"]
        plan.setdefault("rationale", "workflow revision")
        summary = f"切片计划：{len(state['new_slices'])} 片（修订）"
        conn.execute(
            "UPDATE workflow_steps SET plan_json=?, summary=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?",
            (jdumps(plan), summary, now, wid, plan_step["step_id"]))
        new_hash = weights.definition_hash(definition)
        conn.execute(
            "UPDATE workflows SET definition_json=?, definition_hash=?, updated_at=? "
            "WHERE id=?",
            (jdumps(definition), new_hash, now, wid))
        if draft_id:
            cur = conn.execute(
                "UPDATE board_drafts SET status='approved', decided_at=? "
                "WHERE id=? AND status='pending'", (now, draft_id))
            if cur.rowcount != 1:
                raise ConflictError("draft state changed; refresh before approving")
        task_id = wf["task_id"]
        ledger.emit_activity(
            conn, task_id=task_id,
            role_slug=actor,
            kind="comment",
            payload=jdumps({"body": f"应用工作流修订：{impact['summary']}。"
                             "当前执行继续，不重启。",
                     "event": "workflow_revised", "workflow_id": wid,
                     "draft_id": draft_id}),
            now=now)
        ledger.emit(
            conn, "workflow.revised", task_id=task_id, workflow_id=wid,
            execution_no=wf.get("execution_no"),
            actor_type="human" if actor == "human" else "role", actor=actor,
            payload={"draft_id": draft_id, "impact": impact,
                     "definition_hash": new_hash})
        # 修订后 planned graph/dossier 与新步骤一起刷新（同一事务）。
        dossier.persist(conn, wid, commit=False)
        conn.commit()
        return get_workflow(conn, wid)  # type: ignore[return-value]
    except BaseException:
        conn.rollback()
        raise
