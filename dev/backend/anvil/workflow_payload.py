"""Workflow 响应载荷的容错规范化。

Cindy 的草案或历史数据可能把本应为字符串数组的字段写成对象数组
（例如 ``feedback_rules: [{"when": ..., "action": ...}]``），而 ``WorkflowOut``
要求 ``list[str]``；FastAPI 在响应序列化阶段会因此抛 ResponseValidationError（500）。

本模块只做契约/展示层兼容：把字典、字符串等常见偏差收敛为 schema 期望的形状，
不改业务状态。写入路径（``normalize_definition``）与读取路径（``get_workflow``）
共用，保证：
- 新任务落库前就是安全形状；
- 已存在的异常数据在读取时也能正常展示；
- 真正发生转换时按字段去重记录 WARNING，便于定位脏数据。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("anvil.services.workflow")

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()
_WARN_LIMIT = 1024

_WORKFLOW_STATUSES = ("running", "done", "failed", "terminated")
_WEIGHTS = (None, "light", "standard", "strict")
_DECISION_AUTHORITIES = (None, "", "ai", "human", "joint")


def _warn(context: str | None, field: str, value: Any) -> None:
    key = f"{context or '-'}:{field}:{type(value).__name__}"
    with _WARN_LOCK:
        if key in _WARNED:
            return
        if len(_WARNED) >= _WARN_LIMIT:
            _WARNED.clear()
        _WARNED.add(key)
    try:
        logger.warning(
            "workflow payload normalized: %s (%s -> compatible shape)",
            field, type(value).__name__)
    except Exception:  # noqa: BLE001 —— 日志失败不影响响应
        pass


def _scalar_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _dict_text(item: dict) -> str | None:
    """把一条结构化规则压成可读字符串，保留 when/action 语义。"""
    when = _scalar_text(item.get("when"))
    action = _scalar_text(item.get("action"))
    if when and action:
        return f"{when}：{action}"
    if when:
        return when
    if action:
        return action
    for key in ("text", "rule", "description", "label", "summary", "message",
                "criterion"):
        text = _scalar_text(item.get(key))
        if text:
            status = _scalar_text(item.get("status"))
            if key == "criterion" and status and status != "unknown":
                return f"{text}（{status}）"
            return text
    pairs = [
        f"{key}: {text}"
        for key in item
        for text in (_scalar_text(item.get(key)),)
        if text
    ]
    return "；".join(pairs) or None


def _string_list(value: Any, *, field: str, context: str | None,
                 keyed_dict: bool = False) -> list[str]:
    """把任意常见形状收敛为 list[str]。"""
    if value is None:
        return []
    if isinstance(value, str):
        _warn(context, field, value)
        return [value] if value.strip() else []
    if isinstance(value, dict):
        _warn(context, field, value)
        if keyed_dict:
            return [str(key) for key in value if str(key).strip()]
        out: list[str] = []
        for key, raw in value.items():
            text = _scalar_text(raw) or (_dict_text(raw) if isinstance(raw, dict) else None)
            if text:
                out.append(f"{key}: {text}")
        return out
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        changed = False
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    out.append(item)
                continue
            changed = True
            if isinstance(item, dict):
                text = _dict_text(item)
            else:
                text = _scalar_text(item)
            if text:
                out.append(text)
        if changed:
            _warn(context, field, value)
        return out
    _warn(context, field, value)
    text = _scalar_text(value)
    return [text] if text else []


def _coerce_object_strings(item: dict, keys: tuple[str, ...], *,
                           field: str, context: str | None) -> dict:
    out = dict(item)
    for key in keys:
        raw = out.get(key)
        if raw is None or isinstance(raw, str):
            continue
        _warn(context, f"{field}.{key}", raw)
        out[key] = str(raw)
    return out


def _verification_list(value: Any, *, field: str, context: str | None) -> list[dict]:
    def one(raw: Any) -> dict | None:
        if isinstance(raw, dict):
            return _coerce_object_strings(
                raw, ("cell", "method", "verifier_role"), field=field, context=context)
        if isinstance(raw, str):
            return {"method": raw} if raw.strip() else None
        text = _scalar_text(raw)
        return {"method": text} if text else None

    if value is None:
        return []
    if isinstance(value, dict):
        _warn(context, field, value)
        if not value:
            return []
        if any(key in value for key in ("cell", "method", "verifier_role")):
            item = one(value)
            return [item] if item else []
        out: list[dict] = []
        for key, raw in value.items():
            if isinstance(raw, dict):
                out.append({**raw, "cell": str(key)})
            else:
                text = _scalar_text(raw)
                if text:
                    out.append({"cell": str(key), "method": text})
        return out
    if isinstance(value, (list, tuple)):
        changed = any(not isinstance(item, dict) for item in value)
        out = [item for raw in value if (item := one(raw)) is not None]
        if changed:
            _warn(context, field, value)
        return out
    _warn(context, field, value)
    item = one(value)
    return [item] if item else []


def _evidence_list(value: Any, *, field: str, context: str | None) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        _warn(context, field, value)
        return [_coerce_object_strings(
            value, ("id", "kind", "ref", "label", "digest", "captured_at"),
            field=field, context=context)] if value else []
    if isinstance(value, (list, tuple)):
        out: list[dict] = []
        changed = False
        for item in value:
            if isinstance(item, dict):
                out.append(_coerce_object_strings(
                    item, ("id", "kind", "ref", "label", "digest", "captured_at"),
                    field=field, context=context))
                continue
            changed = True
            text = _scalar_text(item)
            if text:
                out.append({"label": text})
        if changed:
            _warn(context, field, value)
        return out
    _warn(context, field, value)
    text = _scalar_text(value)
    return [{"label": text}] if text else []


def _decision_option_list(value: Any, *, field: str, context: str | None) -> list[dict]:
    def one(raw: Any) -> dict | None:
        if isinstance(raw, dict):
            item = _coerce_object_strings(
                raw, ("id", "label", "decision"), field=field, context=context)
            item.setdefault("decision", "")
            return item
        text = _scalar_text(raw)
        if not text:
            return None
        return {"label": text, "decision": text}

    if value is None:
        return []
    if isinstance(value, dict):
        _warn(context, field, value)
        if not value:
            return []
        if any(key in value for key in ("decision", "label", "id")):
            item = one(value)
            return [item] if item else []
        out: list[dict] = []
        for key, raw in value.items():
            if isinstance(raw, dict):
                out.append({**raw, "decision": str(key)})
            else:
                text = _scalar_text(raw)
                if text:
                    out.append({"decision": str(key), "label": text})
        return out
    if isinstance(value, (list, tuple)):
        changed = any(not isinstance(item, dict) for item in value)
        out = [item for raw in value if (item := one(raw)) is not None]
        if changed:
            _warn(context, field, value)
        return out
    _warn(context, field, value)
    item = one(value)
    return [item] if item else []


def _acceptance_for_definition(value: Any, *, field: str,
                               context: str | None) -> list:
    """definition 里的 acceptance 保留条目结构（review packet 依赖 status/unmet 等字段），
    只把非列表形状收敛成列表；面向 API 的扁平化在 normalize_step_payload 中做。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    _warn(context, field, value)
    return [value]


def _normalize_definition_step(step: dict, *, field_prefix: str,
                               context: str | None) -> dict:
    out = dict(step)
    if "acceptance" in out:
        out["acceptance"] = _acceptance_for_definition(
            out["acceptance"], field=f"{field_prefix}.acceptance", context=context)
    return out


def _normalize_step_fields(step: dict, *, field_prefix: str,
                           context: str | None) -> dict:
    out = dict(step)
    for key in ("depends_on", "acceptance", "on_reject", "escalate", "scope_paths"):
        if key in out:
            out[key] = _string_list(
                out[key], field=f"{field_prefix}.{key}", context=context,
                keyed_dict=(key == "depends_on"))
    if "evidence" in out:
        out["evidence"] = _evidence_list(
            out["evidence"], field=f"{field_prefix}.evidence", context=context)
    if "decision_options" in out:
        out["decision_options"] = _decision_option_list(
            out["decision_options"], field=f"{field_prefix}.decision_options",
            context=context)
    return out


_STEP_STRING_FIELDS = (
    "workflow_id", "step_id", "title", "instructions", "role_slug", "contract",
    "failure_reason", "last_nudge_at", "feedback", "source_step_id", "slice_key",
    "stage",
    "plan_json", "risk_level", "decision_question", "packet_id", "submitted_at",
    "status", "run_id", "summary", "started_at", "ended_at", "updated_at",
    "lease_holder", "lease_expires_at",
)
_STEP_REQUIRED_STRINGS = ("workflow_id", "step_id", "title", "status", "updated_at")
_STEP_INT_FIELDS = (
    "max_rework", "rework_count", "nudge_count", "recovery_count",
    "fence_token", "lease_fence",
)
_STEP_BOOL_FIELDS = ("gate", "reversible", "evidence_required", "subagent_hint")


def normalize_step_payload(step: dict, *, field_prefix: str = "steps[]",
                           context: str | None = None) -> dict:
    """规范化单个 workflow step 的响应字段。幂等。"""
    if not isinstance(step, dict):
        return step
    out = _normalize_step_fields(step, field_prefix=field_prefix, context=context)

    authority = out.get("decision_authority")
    if authority not in _DECISION_AUTHORITIES:
        _warn(context, f"{field_prefix}.decision_authority", authority)
        out["decision_authority"] = ""

    for key in _STEP_STRING_FIELDS:
        if key not in out:
            continue
        raw = out[key]
        if raw is None:
            if key in _STEP_REQUIRED_STRINGS:
                out[key] = ""
            continue
        if not isinstance(raw, str):
            _warn(context, f"{field_prefix}.{key}", raw)
            out[key] = str(raw)

    for key in _STEP_INT_FIELDS:
        if key not in out or out[key] is None:
            continue
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            _warn(context, f"{field_prefix}.{key}", out[key])
            out[key] = 0

    for key in _STEP_BOOL_FIELDS:
        if key not in out or out[key] is None:
            continue
        raw = out[key]
        if isinstance(raw, str):
            out[key] = raw.strip().lower() not in ("", "0", "false", "no")
        elif isinstance(raw, (int, float, bool)):
            out[key] = bool(raw)
    return out


def normalize_definition_payload(definition: Any, *,
                                 context: str | None = None) -> dict:
    """规范化 workflows.definition_json 的稳定子集（feedback_rules/verification/steps）。"""
    if not isinstance(definition, dict):
        _warn(context, "definition", definition)
        return {}
    out = dict(definition)
    if "feedback_rules" in out and out["feedback_rules"] is not None:
        out["feedback_rules"] = _string_list(
            out["feedback_rules"], field="definition.feedback_rules", context=context)
    if "verification" in out and out["verification"] is not None:
        out["verification"] = _verification_list(
            out["verification"], field="definition.verification", context=context)
    steps = out.get("steps")
    if isinstance(steps, list):
        out["steps"] = [
            _normalize_definition_step(step, field_prefix=f"definition.steps[{i}]",
                                       context=context)
            if isinstance(step, dict) else step
            for i, step in enumerate(steps)
        ]
    return out


def normalize_workflow_payload(workflow: Any, *,
                               context: str | None = None) -> dict:
    """规范化整个 WorkflowOut 载荷。幂等。"""
    if not isinstance(workflow, dict):
        return workflow
    out = dict(workflow)
    ctx = context or str(out.get("id") or "-")

    out["definition"] = normalize_definition_payload(
        out.get("definition") or {}, context=ctx)

    steps = out.get("steps")
    if isinstance(steps, list):
        out["steps"] = [
            normalize_step_payload(step, field_prefix=f"steps[{i}]", context=ctx)
            if isinstance(step, dict) else step
            for i, step in enumerate(steps)
        ]

    status = out.get("status")
    if status not in _WORKFLOW_STATUSES:
        _warn(ctx, "status", status)
        out["status"] = "failed"

    weight = out.get("weight")
    if weight not in _WEIGHTS:
        _warn(ctx, "weight", weight)
        out["weight"] = None

    execution_no = out.get("execution_no")
    if execution_no is not None:
        try:
            out["execution_no"] = int(execution_no)
        except (TypeError, ValueError):
            _warn(ctx, "execution_no", execution_no)
            out["execution_no"] = 1
    template_version = out.get("template_version")
    if template_version is not None:
        try:
            out["template_version"] = int(template_version)
        except (TypeError, ValueError):
            _warn(ctx, "template_version", template_version)
            out["template_version"] = None

    for key in ("created_at", "updated_at"):
        raw = out.get(key)
        if raw is None:
            _warn(ctx, key, raw)
            out[key] = ""
        elif not isinstance(raw, str):
            _warn(ctx, key, raw)
            out[key] = str(raw)

    pending = out.get("pending_revision")
    if isinstance(pending, dict):
        impact = pending.get("impact")
        if isinstance(impact, dict):
            impact = dict(impact)
            if "affected_steps" in impact:
                impact["affected_steps"] = _string_list(
                    impact["affected_steps"],
                    field="pending_revision.impact.affected_steps", context=ctx)
            pending = {**pending, "impact": impact}
        out["pending_revision"] = pending
    return out


def normalize_weight(value: Any) -> Any:
    """WorkflowHistoryOut.weight 的 Literal 兜底。"""
    return value if value in _WEIGHTS else None


def normalize_affected_steps(value: Any, *,
                             context: str | None = None) -> list[str]:
    """PendingRevisionOut.impact.affected_steps 的列表兜底。"""
    return _string_list(
        value, field="pending_revision.impact.affected_steps", context=context)
