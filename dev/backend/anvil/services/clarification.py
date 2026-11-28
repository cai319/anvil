"""Cindy 规划前澄清（clarify-before-plan）状态服务。

职责边界：
- 只处理 **首次规划阶段**：任务 status=todo、从未有过 workflow、没有执行 run、
  不是派生子任务。已有 workflow / 已执行 / 非 todo 的任务不进入澄清。
- DB 是真相源：``tasks.intent_brief_json`` + ``task_brief_versions``；
  任务工作区 ``.anvil/intent-brief.json|md`` 只是投影。
- 确认后把 flow weight 写入 ``tasks.workflow_weight``（source=human），
  沿用现有 ``weights.resolve()`` 的优先级。
- 确认/跳过/编辑简报必须由 human 直接触发；agent 只能写草稿。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import ledger, roles as roles_service, task_workspace, tasks as tasks_service, weights

STATUSES = ("none", "in_progress", "confirmed", "skipped", "stale")
ACTIVE_STATUSES = ("in_progress", "stale")
MAX_ROUNDS = 10
CLARIFY_MARKER = "[anvil:clarify]"
QUESTION_BLOCK_RE = re.compile(
    r"```anvil-questions\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
PLAN_SHAPES = ("flat", "slices", "auto")
LIST_FIELDS = (
    "non_goals", "constraints", "acceptance", "assumptions", "open_questions", "risks")
TEXT_FIELDS = ("goal", "true_intent", "weight_rationale", "plan_shape_rationale")
BRIEF_KEYS = set(TEXT_FIELDS) | set(LIST_FIELDS) | {
    "weight", "plan_shape", "basis_hash", "confirmed_at", "confirmed_by"}
BASIS_FIELDS = (
    "title", "description", "type", "repo", "scope_paths", "acceptance", "base_branch")
ACTIVE_RUN_STATUSES = (
    "created", "projecting", "running", "submitted", "reviewing", "suspended")
THINKING_RUN_STATUSES = ("created", "projecting", "running")
CONFIRM_REQUIRED_TEXT = ("goal", "true_intent")
CONFIRM_REQUIRED_LIST = ("acceptance",)


# ---- 触发条件 -------------------------------------------------------------


def _require_task(conn: sqlite3.Connection, task_id: str) -> dict:
    task = tasks_service.get(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    return task


def _flag_on() -> bool:
    return bool(getattr(config, "CINDY_CLARIFICATION_ENABLED", True))


def _has_workflow(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM workflows WHERE task_id=? LIMIT 1", (task_id,)
    ).fetchone() is not None


def _has_pending_draft(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM board_drafts WHERE source_task=? AND status='pending' "
        "AND kind IN ('workflow','workflow_revision') LIMIT 1", (task_id,)
    ).fetchone() is not None


def _has_active_non_clarify_run(conn: sqlite3.Connection, task_id: str) -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    return conn.execute(
        f"SELECT 1 FROM runs WHERE task_id=? AND status IN ({placeholders}) "
        "AND COALESCE(origin,'') != 'clarify' LIMIT 1",
        (task_id, *ACTIVE_RUN_STATUSES),
    ).fetchone() is not None


def _has_active_run(conn: sqlite3.Connection, task_id: str) -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    return conn.execute(
        f"SELECT 1 FROM runs WHERE task_id=? AND status IN ({placeholders}) LIMIT 1",
        (task_id, *ACTIVE_RUN_STATUSES),
    ).fetchone() is not None


def _is_thinking(conn: sqlite3.Connection, task: dict, status: str) -> bool:
    """Cindy 是否正在处理：clarify run 在跑，或确认后的 planner run 在跑。

    suspended 表示已发完消息、等待 human，不算思考；failed/abandoned 也不算。
    """
    planner = str(task.get("planner") or "cindy")
    placeholders = ",".join("?" for _ in THINKING_RUN_STATUSES)
    if status == "in_progress":
        return conn.execute(
            f"SELECT 1 FROM runs WHERE task_id=? AND role_slug=? "
            f"AND origin='clarify' AND status IN ({placeholders}) LIMIT 1",
            (task["id"], planner, *THINKING_RUN_STATUSES),
        ).fetchone() is not None
    if status == "confirmed":
        return conn.execute(
            f"SELECT 1 FROM runs WHERE task_id=? AND role_slug=? "
            f"AND COALESCE(origin,'') != 'clarify' "
            f"AND status IN ({placeholders}) LIMIT 1",
            (task["id"], planner, *THINKING_RUN_STATUSES),
        ).fetchone() is not None
    return False


def _has_execution_run(conn: sqlite3.Connection, task_id: str) -> bool:
    """是否有过执行 run；规划/澄清 run 不算。

    - 步骤/工作流 run 直接由 step_id/workflow_id 判定；
    - mention/dispatch run 也是执行；
    - 直接任务 run（origin=NULL、prompt=NULL）在结束后同样算执行；
    - planner run 的 prompt 含 ``[anvil:plan]``，clarify run 含 ``[anvil:clarify]``，
      因此不会被误判。
    """
    return conn.execute(
        "SELECT 1 FROM runs WHERE task_id=? AND ("
        "step_id IS NOT NULL OR workflow_id IS NOT NULL "
        "OR origin IN ('mention','dispatch') "
        "OR (origin IS NULL AND prompt IS NULL "
        "    AND status IN ('completed','failed','abandoned'))"
        ") LIMIT 1", (task_id,)
    ).fetchone() is not None


def _is_subtask(task: dict) -> bool:
    return bool(task.get("parent_id")) or str(task.get("type") or "") in (
        "dispatch", "dispatched")


def _resolve_planner(task: dict, planner: str | None) -> str:
    return str(planner or task.get("planner") or "cindy")


def can_clarify(conn: sqlite3.Connection, task_id: str,
                planner: str | None = None) -> bool:
    """澄清子系统是否适用于该任务（与当前澄清状态无关）。"""
    try:
        task = _require_task(conn, task_id)
    except LookupError:
        return False
    if not _flag_on():
        return False
    if str(task.get("status") or "") != "todo":
        return False
    if _is_subtask(task):
        return False
    if not roles_service.is_onboarding(conn, _resolve_planner(task, planner)):
        return False
    if _has_workflow(conn, task_id):
        return False
    if _has_pending_draft(conn, task_id):
        return False
    if _has_active_non_clarify_run(conn, task_id):
        return False
    if _has_execution_run(conn, task_id):
        return False
    return True


# ---- 简报校验与哈希 --------------------------------------------------------


def basis_hash(task: dict) -> str:
    payload: dict[str, Any] = {}
    for field in BASIS_FIELDS:
        value = task.get(field)
        if isinstance(value, list):
            value = sorted(str(x) for x in value)
        payload[field] = value if value is not None else ""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_brief(brief: dict | None, *, strict: bool) -> dict:
    if brief is None:
        brief = {}
    if not isinstance(brief, dict):
        raise ValueError("brief must be a JSON object")
    unknown = sorted(set(brief) - BRIEF_KEYS)
    if unknown:
        raise ValueError("unknown brief fields: " + ", ".join(unknown))

    out: dict[str, Any] = {}
    for key in TEXT_FIELDS:
        if key in brief and brief[key] is not None:
            if not isinstance(brief[key], str):
                raise ValueError(f"brief.{key} must be a string")
            out[key] = brief[key].strip()
    for key in LIST_FIELDS:
        if key in brief and brief[key] is not None:
            value = brief[key]
            if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value):
                raise ValueError(f"brief.{key} must be a list of strings")
            out[key] = [item.strip() for item in value if item.strip()]
    if brief.get("weight") is not None:
        weight = weights.normalize(brief.get("weight"))
        if weight is None:
            raise ValueError(f"invalid brief.weight: {brief.get('weight')!r}")
        out["weight"] = weight
    if brief.get("plan_shape") is not None:
        shape = str(brief["plan_shape"]).strip().lower()
        if shape not in PLAN_SHAPES:
            raise ValueError(f"invalid brief.plan_shape: {brief['plan_shape']!r}")
        out["plan_shape"] = shape

    if strict:
        missing = [key for key in CONFIRM_REQUIRED_TEXT if not out.get(key)]
        missing += [key for key in CONFIRM_REQUIRED_LIST if not out.get(key)]
        if missing:
            raise ValueError("brief missing required fields: " + ", ".join(missing))
        if "weight" not in out:
            raise ValueError("brief.weight is required")
        out.setdefault("plan_shape", "auto")
    return out


def brief_ready(brief: dict | None) -> bool:
    try:
        _normalize_brief(brief, strict=True)
        return True
    except ValueError:
        return False


def _require_human(actor: str) -> None:
    if str(actor) != "human":
        raise ConflictError("only human can confirm, skip, or edit the intent brief")


def _next_version(task: dict) -> int:
    return int(task.get("intent_brief_version") or 0) + 1


def _record_version(conn: sqlite3.Connection, task_id: str, version: int,
                    kind: str, brief: dict, actor: str,
                    reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO task_brief_versions "
        "(id,task_id,version,kind,brief_json,actor,reason,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (new_id(), task_id, version, kind, jdumps(brief), actor,
         reason, now_iso()))


# ---- 投影 -----------------------------------------------------------------


def _render_markdown(brief: dict) -> str:
    lines = ["# 任务简报（Intent Brief）", ""]
    if brief.get("goal"):
        lines.append(f"**目标**：{brief['goal']}")
    if brief.get("true_intent"):
        lines.append(f"**真实意图**：{brief['true_intent']}")
    for key, label in (
            ("non_goals", "非目标"), ("constraints", "约束"),
            ("acceptance", "验收标准"), ("assumptions", "假设"),
            ("open_questions", "开放问题"), ("risks", "风险")):
        values = brief.get(key) or []
        if values:
            lines.append("")
            lines.append(f"**{label}**：")
            lines.extend(f"- {value}" for value in values)
    if brief.get("weight"):
        lines.append("")
        lines.append(f"**流程强度**：{brief['weight']}")
    if brief.get("weight_rationale"):
        lines.append(f"**强度理由**：{brief['weight_rationale']}")
    if brief.get("plan_shape"):
        lines.append(f"**计划形态**：{brief['plan_shape']}")
    if brief.get("plan_shape_rationale"):
        lines.append(f"**形态理由**：{brief['plan_shape_rationale']}")
    if brief.get("confirmed_at"):
        lines.append("")
        lines.append(
            f"_已由 {brief.get('confirmed_by') or 'human'} 于 "
            f"{brief['confirmed_at']} 确认；DB 为真相源。_")
    lines.append("")
    return "\n".join(lines)


def project_brief(conn: sqlite3.Connection, task_id: str, brief: dict) -> Path:
    """把简报投影到任务工作区；DB 仍是真相源。"""
    target = task_workspace.path_for(task_id) / ".anvil"
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "intent-brief.json"
    json_path.write_text(
        json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    (target / "intent-brief.md").write_text(
        _render_markdown(brief), encoding="utf-8")
    return json_path


# ---- 状态读取 -------------------------------------------------------------


def get_state(conn: sqlite3.Connection, task_id: str) -> dict:
    task = _require_task(conn, task_id)
    status = str(task.get("clarification_status") or "none")
    brief = task.get("intent_brief")
    if not isinstance(brief, dict):
        brief = None
    ready = brief_ready(brief)
    weight = (brief or {}).get("weight") or task.get("workflow_weight")
    weight_source = task.get("workflow_weight_source")
    if weight and not weight_source:
        weight_source = "cindy" if brief else None
    plan_shape = (brief or {}).get("plan_shape")
    can_edit = status in ("in_progress", "stale")
    if status == "confirmed":
        can_edit = (not _has_active_run(conn, task_id)
                    and not _has_pending_draft(conn, task_id))
    waiting_for_human = (
        status == "in_progress"
        and tasks_service.waiting_for_human(
            conn, task_id, str(task.get("planner") or "cindy")))
    # 轮次是派生值：按实际 grill 问题消息校准，兼容旧版按对话次数累计的数据。
    round_no = (sync_question_rounds(conn, task) if status == "in_progress"
                else int(task.get("clarification_round") or 0))
    return {
        "task_id": task_id,
        "status": status,
        "round": round_no,
        "max_rounds": MAX_ROUNDS,
        "brief": brief,
        "brief_version": int(task.get("intent_brief_version") or 0),
        "ready": ready,
        "weight": weight,
        "weight_source": weight_source,
        "plan_shape": plan_shape,
        "stale_reason": "任务定义已变更" if status == "stale" else None,
        "waiting_for_human": waiting_for_human,
        # 已发出问题并等待 human 时，即使 run 仍残留 running 也不再显示思考动画。
        "thinking": _is_thinking(conn, task, status) and not waiting_for_human,
        "can_start": status in ("none", "stale", "skipped")
        and can_clarify(conn, task_id),
        "can_confirm": status in ACTIVE_STATUSES and ready,
        "can_skip": status in ACTIVE_STATUSES,
        "can_edit": can_edit,
    }


# ---- 迁移 -----------------------------------------------------------------


def start(conn: sqlite3.Connection, task_id: str, *,
          planner: str | None = None, actor: str = "human",
          reason: str | None = None) -> dict:
    task = _require_task(conn, task_id)
    resolved = _resolve_planner(task, planner)
    if not can_clarify(conn, task_id, planner=resolved):
        raise ConflictError(
            "clarification is not available for this task: only Cindy's first "
            "planning phase on a fresh, non-executed task can be clarified")
    status = str(task.get("clarification_status") or "none")
    if status not in ("none", "stale", "skipped"):
        raise ConflictError(f"task {task_id} clarification is {status}")
    now = now_iso()
    cur = conn.execute(
        "UPDATE tasks SET clarification_status='in_progress', "
        "clarification_round=0, clarification_confirmed_at=NULL, updated_at=? "
        "WHERE id=? AND COALESCE(clarification_status,'none')=?",
        (now, task_id, status))
    if cur.rowcount != 1:
        raise ConflictError("clarification state changed; retry")
    ledger.emit(
        conn, "clarification.started", task_id=task_id,
        actor_type="human" if actor == "human" else "system", actor=actor,
        payload={"reason": reason, "from_status": status, "planner": resolved})
    conn.commit()
    return get_state(conn, task_id)


def save_draft(conn: sqlite3.Connection, task_id: str, brief: dict, *,
               actor: str = "cindy", reason: str | None = None) -> dict:
    task = _require_task(conn, task_id)
    if str(task.get("clarification_status") or "none") != "in_progress":
        raise ConflictError(
            f"task {task_id} clarification is {task.get('clarification_status')}; "
            "brief drafts are only accepted while in_progress")
    normalized = _normalize_brief(brief, strict=False)
    normalized["basis_hash"] = basis_hash(task)
    version = _next_version(task)
    now = now_iso()
    cur = conn.execute(
        "UPDATE tasks SET intent_brief_json=?, intent_brief_version=?, updated_at=? "
        "WHERE id=? AND clarification_status='in_progress'",
        (jdumps(normalized), version, now, task_id))
    if cur.rowcount != 1:
        raise ConflictError("clarification state changed; retry")
    _record_version(conn, task_id, version, "draft", normalized, actor, reason)
    ledger.emit(
        conn, "clarification.brief_drafted", task_id=task_id,
        actor_type="role" if actor != "human" else "human", actor=actor,
        payload={"version": version, "ready": brief_ready(normalized)})
    conn.commit()
    project_brief(conn, task_id, normalized)
    return get_state(conn, task_id)


def confirm(conn: sqlite3.Connection, task_id: str, *,
            brief: dict | None = None, weight: str | None = None,
            plan_shape: str | None = None,
            acknowledge_open_questions: bool = False,
            actor: str = "human", reason: str | None = None) -> dict:
    _require_human(actor)
    task = _require_task(conn, task_id)
    status = str(task.get("clarification_status") or "none")
    if status not in ACTIVE_STATUSES:
        raise ConflictError(f"task {task_id} clarification is {status}; cannot confirm")
    merged: dict[str, Any] = dict(brief if brief is not None else (
        task.get("intent_brief") if isinstance(task.get("intent_brief"), dict) else {}))
    if weight is not None:
        merged["weight"] = weight
    if plan_shape is not None:
        merged["plan_shape"] = plan_shape
    for server_field in ("basis_hash", "confirmed_at", "confirmed_by"):
        merged.pop(server_field, None)
    normalized = _normalize_brief(merged, strict=True)
    if normalized.get("open_questions") and not acknowledge_open_questions:
        raise ValueError(
            "open questions remain; pass acknowledge_open_questions=true to confirm")
    now = now_iso()
    normalized["basis_hash"] = basis_hash(task)
    normalized["confirmed_at"] = now
    normalized["confirmed_by"] = "human"
    version = _next_version(task)
    weight_value = normalized["weight"]
    weight_reason = "澄清阶段确认：" + (
        normalized.get("weight_rationale") or "human 在澄清阶段确认")
    cur = conn.execute(
        "UPDATE tasks SET clarification_status='confirmed', intent_brief_json=?, "
        "intent_brief_version=?, clarification_confirmed_at=?, "
        "clarification_basis_hash=?, workflow_weight=?, "
        "workflow_weight_source='human', workflow_weight_reason=?, updated_at=? "
        "WHERE id=? AND clarification_status=?",
        (jdumps(normalized), version, now, normalized["basis_hash"],
         weight_value, weight_reason, now, task_id, status))
    if cur.rowcount != 1:
        raise ConflictError("clarification state changed; retry")
    _record_version(conn, task_id, version, "confirmed", normalized, actor, reason)
    ledger.emit(
        conn, "clarification.confirmed", task_id=task_id,
        actor_type="human", actor=actor,
        payload={"version": version, "weight": weight_value,
                 "plan_shape": normalized.get("plan_shape")})
    conn.commit()
    project_brief(conn, task_id, normalized)
    return get_state(conn, task_id)


def skip(conn: sqlite3.Connection, task_id: str, *,
         actor: str = "human", reason: str | None = None) -> dict:
    _require_human(actor)
    task = _require_task(conn, task_id)
    status = str(task.get("clarification_status") or "none")
    if status not in ACTIVE_STATUSES:
        raise ConflictError(f"task {task_id} clarification is {status}; cannot skip")
    now = now_iso()
    brief = task.get("intent_brief")
    if not isinstance(brief, dict):
        brief = {}
    version = _next_version(task)
    cur = conn.execute(
        "UPDATE tasks SET clarification_status='skipped', intent_brief_version=?, "
        "clarification_confirmed_at=?, clarification_basis_hash=?, updated_at=? "
        "WHERE id=? AND clarification_status=?",
        (version, now, basis_hash(task), now, task_id, status))
    if cur.rowcount != 1:
        raise ConflictError("clarification state changed; retry")
    _record_version(conn, task_id, version, "skipped", brief, actor, reason)
    ledger.emit(
        conn, "clarification.skipped", task_id=task_id,
        actor_type="human", actor=actor,
        payload={"version": version, "reason": reason})
    conn.commit()
    return get_state(conn, task_id)


def update_brief(conn: sqlite3.Connection, task_id: str, brief: dict, *,
                 actor: str = "human", reason: str | None = None) -> dict:
    """human 在确认后编辑简报；有活跃 planner run 或 pending 草案时拒绝。"""
    _require_human(actor)
    task = _require_task(conn, task_id)
    if str(task.get("clarification_status") or "none") != "confirmed":
        raise ConflictError(
            f"task {task_id} clarification is {task.get('clarification_status')}; "
            "only confirmed briefs can be edited")
    if _has_active_run(conn, task_id):
        raise ConflictError("a run is active; wait for it to finish before editing the brief")
    if _has_pending_draft(conn, task_id):
        raise ConflictError("a workflow draft is pending; decide it before editing the brief")
    normalized = _normalize_brief(brief, strict=True)
    now = now_iso()
    normalized["basis_hash"] = basis_hash(task)
    normalized["confirmed_at"] = now
    normalized["confirmed_by"] = "human"
    version = _next_version(task)
    weight_value = normalized["weight"]
    weight_reason = "澄清阶段确认：" + (
        normalized.get("weight_rationale") or "human 编辑简报后确认")
    cur = conn.execute(
        "UPDATE tasks SET intent_brief_json=?, intent_brief_version=?, "
        "clarification_confirmed_at=?, clarification_basis_hash=?, "
        "workflow_weight=?, workflow_weight_source='human', "
        "workflow_weight_reason=?, updated_at=? WHERE id=? AND clarification_status='confirmed'",
        (jdumps(normalized), version, now, normalized["basis_hash"],
         weight_value, weight_reason, now, task_id))
    if cur.rowcount != 1:
        raise ConflictError("clarification state changed; retry")
    _record_version(conn, task_id, version, "human_edit", normalized, actor, reason)
    ledger.emit(
        conn, "clarification.brief_edited", task_id=task_id,
        actor_type="human", actor=actor,
        payload={"version": version, "weight": weight_value})
    conn.commit()
    project_brief(conn, task_id, normalized)
    return get_state(conn, task_id)


# ---- 工作区简报：只读预览 + 人工确认后导入 --------------------------------

WORKSPACE_BRIEF_CANDIDATES = ("brief.json", ".anvil/intent-brief.json")
WORKSPACE_BRIEF_MAX_BYTES = 256_000


def preview_workspace_brief(conn: sqlite3.Connection, task_id: str) -> dict:
    """只读预览工作区里的 ``brief.json`` / ``.anvil/intent-brief.json``。

    绝不写入 DB。Cindy 在运行时环境失败时可能把草稿写在工作区（v1.62.1 事故现场），
    但它只能作为 human 查看后手动导入的候选，不能自动成为真相源。
    """
    _require_task(conn, task_id)
    workspace = task_workspace.path_for(task_id)
    for rel in WORKSPACE_BRIEF_CANDIDATES:
        path = workspace / rel
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > WORKSPACE_BRIEF_MAX_BYTES:
            return {
                "exists": True, "path": str(path), "relative_path": rel,
                "brief": None, "error": (
                    f"工作区简报过大（{size} 字节）；请人工检查后手工粘贴导入。"),
            }
        try:
            brief = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "exists": True, "path": str(path), "relative_path": rel,
                "brief": None, "error": f"无法解析工作区简报：{exc}",
            }
        if not isinstance(brief, dict):
            return {
                "exists": True, "path": str(path), "relative_path": rel,
                "brief": None, "error": "工作区简报必须是 JSON 对象",
            }
        return {
            "exists": True, "path": str(path), "relative_path": rel,
            "brief": brief, "error": None,
        }
    return {"exists": False, "path": None, "relative_path": None,
            "brief": None, "error": None}


def import_workspace_brief(conn: sqlite3.Connection, task_id: str, *,
                           brief: dict | None = None,
                           reviewed: bool = False,
                           actor: str = "human",
                           reason: str | None = None) -> dict:
    """human 查看后导入为**草稿**；导入不等于确认。

    - 只有 human 能调用；
    - 必须 ``reviewed=True``（两步：先 preview，再带 reviewed 导入）；
    - 只用显式传入或 preview 得到的结构，不自动读取/落库；
    - 导入后仍走既有结构化确认（POST /clarification/confirm）。
    """
    _require_human(actor)
    if not reviewed:
        raise ValueError(
            "workspace brief import requires reviewed=true after preview")
    task = _require_task(conn, task_id)
    status = str(task.get("clarification_status") or "none")
    if status != "in_progress":
        raise ConflictError(
            f"task {task_id} clarification is {status}; start clarification before "
            "importing a workspace brief")
    preview = preview_workspace_brief(conn, task_id)
    chosen = brief if isinstance(brief, dict) else preview.get("brief")
    if not isinstance(chosen, dict):
        raise ValueError(
            preview.get("error") or "no workspace brief found to import")
    # 先校验结构，避免把坏草稿写进版本历史。
    _normalize_brief(chosen, strict=False)
    state = save_draft(
        conn, task_id, chosen, actor="human",
        reason=reason or f"imported workspace {preview.get('relative_path') or 'brief'}")
    ledger.emit(
        conn, "clarification.workspace_brief_imported", task_id=task_id,
        actor_type="human", actor="human",
        payload={"path": preview.get("relative_path"),
                 "requires_confirmation": True})
    conn.commit()
    return {
        "imported": True,
        "requires_confirmation": True,
        "source": preview.get("relative_path"),
        "clarification": state,
    }


def mark_stale(conn: sqlite3.Connection, task_id: str, *,
               reason: str | None = None, actor: str = "system") -> dict | None:
    """任务定义材料性变更后标记 stale；只在无 workflow 的任务上生效。"""
    task = _require_task(conn, task_id)
    status = str(task.get("clarification_status") or "none")
    if status not in ("confirmed", "skipped"):
        return None
    if _has_workflow(conn, task_id):
        return None
    current = basis_hash(task)
    if current == task.get("clarification_basis_hash"):
        return None
    now = now_iso()
    cur = conn.execute(
        "UPDATE tasks SET clarification_status='stale', updated_at=? "
        "WHERE id=? AND clarification_status=?",
        (now, task_id, status))
    if cur.rowcount != 1:
        return None
    ledger.emit(
        conn, "clarification.stale", task_id=task_id,
        actor_type="system", actor=actor,
        payload={"reason": reason or "任务定义已变更", "basis_hash": current})
    try:
        tasks_service.mention(
            conn, task_id, "system", "human",
            f"任务定义已变更，请在任务简报卡重新确认或跳过：{reason or '任务定义已变更'}")
    except Exception:  # noqa: BLE001 —— 提醒失败不阻断 stale 状态
        pass
    conn.commit()
    return get_state(conn, task_id)


def versions(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM task_brief_versions WHERE task_id=? ORDER BY version",
        (task_id,)).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["brief"] = jloads(item.pop("brief_json", "{}"), {})
        out.append(item)
    return out


# ---- 门禁与 run -----------------------------------------------------------


def assert_workflow_allowed(conn: sqlite3.Connection, task_id: str) -> None:
    """澄清未确认时，服务端拒绝创建 workflow / workflow_revision 草案。"""
    row = conn.execute(
        "SELECT clarification_status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is not None and str(row["clarification_status"] or "none") in ACTIVE_STATUSES:
        raise ConflictError(
            "clarification not confirmed; confirm the intent brief or skip before "
            "creating a workflow draft")


def confirmed_brief(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """读取已确认的任务简报；未确认/不存在时返回 None。"""
    row = conn.execute(
        "SELECT clarification_status, intent_brief_json FROM tasks WHERE id=?",
        (task_id,)).fetchone()
    if row is None or str(row["clarification_status"] or "none") != "confirmed":
        return None
    brief = jloads(row["intent_brief_json"], None)
    return brief if isinstance(brief, dict) else None


def apply_confirmed_weight(conn: sqlite3.Connection, task_id: str,
                           tree: dict) -> dict:
    """把 human 在澄清阶段确认的流程强度写进 workflow 草案。

    weight 的最终解析本来就有 human override 兜底；这里同步草案本身，
    避免审批页/草案树仍显示 Cindy 的旧建议（例如 strict）。
    """
    brief = confirmed_brief(conn, task_id)
    if not brief:
        return tree
    weight = weights.normalize(brief.get("weight"))
    if weight is None:
        return tree
    out = dict(tree)
    out["weight"] = weight
    out["weight_rationale"] = (
        "澄清阶段确认：" + (brief.get("weight_rationale") or "human 确认"))
    return out


def enforce_plan_shape(conn: sqlite3.Connection, task_id: str, tree: dict) -> None:
    """按已确认简报的 plan_shape 校验 workflow 结构。

    - flat：不允许 plan_slices 步骤或 loop 模板；
    - slices：必须同时有 plan_slices 步骤与 loop 模板；
    - auto：不约束。
    在 normalize_definition 之后调用，检查服务端展开后的实际结构。
    """
    brief = confirmed_brief(conn, task_id)
    if not brief:
        return
    shape = str(brief.get("plan_shape") or "auto").strip().lower()
    if shape == "auto":
        return
    steps = tree.get("steps") or tree.get("children") or []
    has_plan_slices = any(
        str(step.get("kind") or "") == "plan_slices"
        for step in steps if isinstance(step, dict))
    has_loop = isinstance(tree.get("loop"), dict)
    if shape == "flat" and (has_plan_slices or has_loop):
        raise ConflictError(
            "confirmed plan_shape=flat; submit flat steps without "
            "plan_slices/loop")
    if shape == "slices" and not (has_plan_slices and has_loop):
        raise ConflictError(
            "confirmed plan_shape=slices; submit a plan_slices step plus "
            "a loop template")


def _question_block_questions(message: str) -> list[Any] | None:
    """解析消息里的结构化问题块；无效或为空返回 None。"""
    match = QUESTION_BLOCK_RE.search(message or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1).strip())
    except (TypeError, ValueError):
        return None
    questions = payload.get("questions") if isinstance(payload, dict) else None
    return questions if isinstance(questions, list) and questions else None


def count_question_rounds(conn: sqlite3.Connection, task: dict) -> int:
    """按实际消息统计 Cindy 主动发出的 grill 问题批次。

    只数本轮澄清（最近一次 clarification.started 之后）中 planner/onboarding
    发给 human、且带合法 anvil-questions 块的消息；human 的追问与修正不计数。
    """
    started = conn.execute(
        "SELECT ts FROM events WHERE task_id=? AND event_type='clarification.started' "
        "ORDER BY ts DESC LIMIT 1", (task["id"],)).fetchone()
    since = str(started["ts"]) if started else ""
    planner = str(task.get("planner") or "cindy")
    rows = conn.execute(
        "SELECT role_slug, payload_json, created_at FROM activities "
        "WHERE task_id=? AND kind='mention' AND created_at>=? "
        "ORDER BY created_at, id", (task["id"], since)).fetchall()
    count = 0
    for row in rows:
        role = str(row["role_slug"] or "")
        if role != planner and not roles_service.is_onboarding(conn, role):
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("target") != "human":
            continue
        message = str(payload.get("message") or "")
        if _question_block_questions(message) is not None:
            count += 1
    return min(count, MAX_ROUNDS)


def sync_question_rounds(conn: sqlite3.Connection, task: dict) -> int:
    """把 tasks.clarification_round 校准为实际问题批次数（兼容旧数据/升级）。"""
    current = int(task.get("clarification_round") or 0)
    if str(task.get("clarification_status") or "none") != "in_progress":
        return current
    actual = count_question_rounds(conn, task)
    if actual != current:
        conn.execute(
            "UPDATE tasks SET clarification_round=? "
            "WHERE id=? AND clarification_status='in_progress'",
            (actual, task["id"]))
        conn.commit()
    return actual


def record_question_round(conn: sqlite3.Connection, task_id: str, *,
                          actor: str) -> int:
    """mention 落库后校准轮次；只有实际新增问题批次时才写事件。"""
    task = _require_task(conn, task_id)
    current = int(task.get("clarification_round") or 0)
    round_no = sync_question_rounds(conn, task)
    if round_no > current:
        ledger.emit(
            conn, "clarification.question_round", task_id=task_id,
            actor_type="role", actor=actor,
            payload={"round": round_no, "max_rounds": MAX_ROUNDS})
        conn.commit()
    return round_no


def start_clarify_run(conn: sqlite3.Connection, db_path: str,
                      orchestrator, task_id: str, planner: str, *,
                      assignees: list[str] | None = None,
                      human_message: str | None = None,
                      replace_active: bool = True,
                      reason: str | None = None) -> dict:
    """启动一轮 clarify run。

    每次 human 回复都新建 run（replace_active=True abandon 旧的 suspended run），
    但 run 本身不消耗轮次；轮次按 Cindy 实际发出的 grill 问题批次派生并校准。
    第 MAX_ROUNDS 轮起 prompt 带 must_conclude。
    run 以 `[anvil:clarify]` 开头，graph/nodes 据此注入 CLARIFY_PROTOCOL。
    """
    from ..graph.render import CLARIFY_PROTOCOL
    from . import planning as planning_service
    from . import runs as runs_service

    if roles_service.is_onboarding(conn, planner):
        roles_service.ensure_onboarding(conn)
    task = _require_task(conn, task_id)
    if str(task.get("clarification_status") or "none") != "in_progress":
        raise ConflictError(
            f"task {task_id} clarification is {task.get('clarification_status')}; "
            "cannot start a clarify run")

    if replace_active:
        for row in conn.execute(
            "SELECT id FROM runs WHERE task_id=? AND role_slug=? "
            "AND status NOT IN ('completed','failed','abandoned')",
            (task_id, planner),
        ).fetchall():
            runs_service.set_status(conn, row["id"], "abandoned", ended_at=now_iso())
    else:
        existing = conn.execute(
            "SELECT * FROM runs WHERE task_id=? AND role_slug=? AND origin='clarify' "
            "AND status NOT IN ('completed','failed','abandoned') "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (task_id, planner)).fetchone()
        if existing is not None:
            return dict(existing)

    now = now_iso()
    cur = conn.execute(
        "UPDATE tasks SET updated_at=? "
        "WHERE id=? AND clarification_status='in_progress'",
        (now, task_id))
    if cur.rowcount != 1:
        raise ConflictError("clarification state changed; retry")
    task = _require_task(conn, task_id)
    round_no = sync_question_rounds(conn, task)
    must_conclude = round_no >= MAX_ROUNDS
    if assignees is None:
        assignees = planning_service._assignees(conn, task_id)
    roster = planning_service.build_roster(conn, task_id, assignees)
    brief = task.get("intent_brief")
    if not isinstance(brief, dict):
        brief = None

    prompt = CLARIFY_MARKER + "\n" + CLARIFY_PROTOCOL
    prompt += (
        "\n\n## 当前澄清状态\n"
        f"- 已提出的 grill 问题轮次：{round_no}/{MAX_ROUNDS}\n"
        f"- 本轮如提出新一批问题，将记为第 {min(round_no + 1, MAX_ROUNDS)} 轮\n"
        f"- must_conclude：{'true' if must_conclude else 'false'}\n")
    if brief:
        prompt += ("\n## 当前简报草稿（在此基础上修正，不要丢失已确认内容）\n"
                   + json.dumps(brief, ensure_ascii=False, indent=2))
    else:
        prompt += "\n## 当前简报草稿\n尚无；本轮开始建立。\n"
    prompt += ("\n## 参与角色（Roster）\n"
               + planning_service.render_roster(conn, task_id, roster))
    size_block = planning_service._task_size_block(task)
    if size_block:
        prompt += "\n\n## 任务规模信息（参考）\n" + size_block
    knowledge_block = planning_service._task_knowledge_block(conn, task)
    if knowledge_block:
        prompt += "\n\n## 任务知识与仓库上下文\n" + knowledge_block
    if human_message and str(human_message).strip():
        prompt += "\n\n## human 本轮回复\n" + str(human_message).strip()
    if must_conclude:
        prompt += (
            "\n\n## 本轮强制收束\n已达 grill 问题上限：不再开新问题。"
            "写最终简报并用 anvil task mention 请求 human 确认。\n")
    else:
        prompt += (
            "\n\n## 本轮要求\n先更新简报草稿，再用 anvil task mention 通知 human；"
            "然后结束回合。human 的追问/修正不消耗轮次；只有你在消息中附带新的 "
            "anvil-questions 问题块时，才记为新一轮。\n")
    if reason:
        prompt += f"\n## 触发原因\n{reason}\n"

    run = runs_service.create_run(conn, planner, task_id, prompt=prompt, origin="clarify")
    ledger.emit(
        conn, "clarification.round_started", task_id=task_id, run_id=run["id"],
        actor_type="system", actor="system",
        payload={"round": round_no, "must_conclude": must_conclude,
                 "has_human_message": bool(human_message)})
    conn.commit()
    if orchestrator:
        try:
            orchestrator.pump(db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001 —— 唤醒失败不阻断 run 创建
            pass
    return run
