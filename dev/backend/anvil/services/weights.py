"""Workflow weight (light | standard | strict).

Cindy proposes a weight with the workflow definition; a human override always
wins for the next execution. A running execution is never changed in place —
the override takes effect when the task is restarted as a new execution.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from ..util import ConflictError, now_iso

WEIGHTS = ("light", "standard", "strict")

STRICT_KEYWORDS = (
    "security", "auth", "payment", "billing", "migration", "release", "deploy",
    "production", "permission", "crypto", "compliance", "pii", "audit",
    "安全", "支付", "权限", "发布", "上线", "迁移", "合规", "生产", "审计",
)


def normalize(value) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in WEIGHTS else None


def definition_hash(definition: dict) -> str:
    raw = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _roles(definition: dict) -> set[str]:
    roles: set[str] = set()
    for step in definition.get("steps") or definition.get("children") or []:
        role = str(step.get("role") or step.get("role_slug") or "").strip()
        if role:
            roles.add(role)
    loop = definition.get("loop")
    if isinstance(loop, dict):
        for phase in loop.values():
            if isinstance(phase, dict):
                role = str(phase.get("role") or "").strip()
                if role:
                    roles.add(role)
    return roles


def _human_gates(definition: dict) -> int:
    count = 0
    for step in definition.get("steps") or definition.get("children") or []:
        if step.get("gate") or step.get("decision_authority") in ("human", "joint"):
            count += 1
    loop = definition.get("loop")
    if isinstance(loop, dict):
        for phase in loop.values():
            if isinstance(phase, dict) and phase.get("gate"):
                count += 1
    return count


def infer(task: dict | None, definition: dict) -> tuple[str, str]:
    text = " ".join(str((task or {}).get(k) or "") for k in
                    ("title", "description", "type")).lower()
    tags = " ".join(str(x) for x in (task or {}).get("tags") or []).lower()
    if any(k in text or k in tags for k in STRICT_KEYWORDS):
        return "strict", "任务涉及安全/发布/迁移/权限/支付等高风险面"
    steps = definition.get("steps") or definition.get("children") or []
    roles = _roles(definition)
    if (len(steps) <= 4 and len(roles) <= 2 and not _human_gates(definition)
            and not definition.get("loop")):
        return "light", "小型、可逆、少量角色的扁平等价流程"
    return "standard", "默认标准流程"


def resolve(conn: sqlite3.Connection, task_id: str,
            definition: dict) -> tuple[str, str, str]:
    """Return (weight, source, reason). Human override > Cindy > heuristic."""
    row = conn.execute(
        "SELECT workflow_weight, workflow_weight_reason, title, description, type, "
        "tags_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        task = None
    else:
        task = dict(row)
        task["tags"] = json.loads(task.pop("tags_json") or "[]")
    override = normalize((task or {}).get("workflow_weight"))
    if override:
        reason = str((task or {}).get("workflow_weight_reason") or "人工指定")
        return override, "human", reason
    proposed = normalize(definition.get("weight"))
    if proposed:
        reason = str(definition.get("weight_rationale") or "Cindy 根据任务风险与规模选择")
        return proposed, "cindy", reason
    weight, reason = infer(task, definition)
    return weight, "heuristic", reason


def set_task_weight(conn: sqlite3.Connection, task_id: str, weight: str | None,
                    reason: str | None = None) -> dict:
    """Set/clear the task-level override. Running executions are not mutated."""
    normalized = normalize(weight) if weight else None
    if weight and normalized is None:
        raise ConflictError(f"invalid workflow weight: {weight!r}")
    task = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise LookupError(task_id)
    if normalized is None:
        conn.execute(
            "UPDATE tasks SET workflow_weight=NULL, workflow_weight_source=NULL, "
            "workflow_weight_reason=NULL, updated_at=? WHERE id=?",
            (now_iso(), task_id))
    else:
        conn.execute(
            "UPDATE tasks SET workflow_weight=?, workflow_weight_source='human', "
            "workflow_weight_reason=?, updated_at=? WHERE id=?",
            (normalized, (reason or "").strip() or "人工指定", now_iso(), task_id))
    running = conn.execute(
        "SELECT id, execution_no, weight FROM workflows WHERE task_id=? "
        "AND status='running' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,)).fetchone()
    conn.commit()
    return {
        "task_id": task_id,
        "weight": normalized,
        "requires_restart": bool(running),
        "running_workflow_id": running["id"] if running else None,
        "running_execution_no": running["execution_no"] if running else None,
        "running_weight": running["weight"] if running else None,
    }
