"""任务问题板（Issue Board）：安全 findings、审核者、人工确认与任务闸门。

数据模型：
- `task_finding_boards`：每任务一行，in_progress / completed；
- `task_findings`：reviewer 通过 CLI 写入的结构化安全 finding；
- `task_reviewers`：允许填写 finding 并完成问题板的角色。

状态：
- finding: draft -> submitted -> confirmed / false_positive / changes_requested；
  changes_requested -> submitted（reviewer 补证/修改后重交）；
- board: in_progress -> completed；human request-changes/reopen -> in_progress；
- task: board completed + workflow done -> in_review；review approve 要求所有 finding
  已 confirmed/false_positive。
"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jloads, new_id, now_iso

RISK_TYPES: dict[str, str] = {
    "injection": "注入",
    "authn_authz": "认证/越权",
    "info_disclosure": "信息泄露",
    "ssrf": "SSRF",
    "path_traversal": "路径穿越",
    "deserialization": "反序列化",
    "race_condition": "竞态/并发",
    "memory_safety": "内存安全",
    "logic_flaw": "逻辑缺陷",
    "crypto": "密码学",
    "supply_chain": "供应链",
    "dos": "拒绝服务",
    "config": "配置安全",
    "other": "其他",
}
SEVERITIES: dict[str, str] = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "提示",
}
FIX_PRIORITIES: dict[str, str] = {
    "P0": "P0 · 立即修复",
    "P1": "P1 · 高优先",
    "P2": "P2 · 计划修复",
    "P3": "P3 · 低优先",
}
POC_STATUSES: dict[str, str] = {
    "provided": "已提供 PoC",
    "not_verified": "未验证（需说明原因）",
    "not_applicable": "不适用（需说明原因）",
}

FIELD_SPECS: dict[str, dict] = {
    "title": {"label": "问题标题", "max": 200, "required": True},
    "risk_type": {"label": "风险类型", "enum": RISK_TYPES, "required": True},
    "severity": {"label": "严重等级", "enum": SEVERITIES, "required": True},
    "module": {"label": "问题所在模块", "max": 500, "required": True},
    "function_name": {"label": "问题函数", "max": 500, "required": True},
    "file_path": {"label": "文件", "max": 1000, "required": True},
    "line_numbers": {"label": "行号", "max": 200, "required": True},
    "preconditions": {"label": "触发前置条件", "max": 20000, "required": True},
    "reasoning_chain": {"label": "推理链（CoT）", "max": 30000, "required": True},
    "risk_description": {"label": "问题风险描述", "max": 20000, "required": True},
    "attack_path": {"label": "攻击路径（数据流分析）", "max": 30000, "required": True},
    "trigger_point": {"label": "触发点", "max": 10000, "required": True},
    "poc_status": {"label": "PoC 状态", "enum": POC_STATUSES, "required": True},
    "poc": {"label": "技术证明（PoC）/ 豁免原因", "max": 40000, "required": True},
    "fix_priority": {"label": "修复优先级", "enum": FIX_PRIORITIES, "required": True},
    "fix_suggestion": {"label": "修复建议", "max": 20000, "required": True},
    "confidence": {"label": "问题最终置信度", "int": True, "required": True},
}
FIELD_ORDER = tuple(FIELD_SPECS)
REQUIRED_FIELDS = tuple(k for k, v in FIELD_SPECS.items() if v.get("required"))
STATUSES = ("draft", "submitted", "confirmed", "false_positive", "changes_requested")
BOARD_STATUSES = ("in_progress", "completed")


class FindingValidationError(ConflictError):
    """字段缺失/非法；API 映射为 422 并返回 missing_fields。"""

    def __init__(self, missing_fields: list[str], errors: list[str] | None = None):
        self.missing_fields = list(missing_fields)
        self.errors = list(errors or [])
        msg = "; ".join(self.errors) or (
            "missing fields: " + ", ".join(self.missing_fields))
        super().__init__(msg)


def schema() -> dict:
    """字段/枚举/中文标签，供前端表单与 CLI template 使用。"""
    return {
        "fields": [
            {"key": key, **spec,
             "label": spec.get("label", key),
             **({"enum_labels": spec["enum"]} if "enum" in spec else {})}
            for key, spec in FIELD_SPECS.items()
        ],
        "risk_types": RISK_TYPES,
        "severities": SEVERITIES,
        "fix_priorities": FIX_PRIORITIES,
        "poc_statuses": POC_STATUSES,
        "statuses": STATUSES,
        "required": list(REQUIRED_FIELDS),
    }


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def _task(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT id, title, status, issue_board_enabled, assignee, planner "
        "FROM tasks WHERE id=?", (task_id,)
    ).fetchone()


def _enabled_task(conn: sqlite3.Connection, task_id: str):
    task = _task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    if not task["issue_board_enabled"]:
        raise ConflictError("issue board is not enabled for this task")
    return task


def _text(conn, key: str, value) -> str:
    spec = FIELD_SPECS[key]
    text = str(value or "").strip()
    limit = int(spec.get("max") or 0)
    if limit and len(text) > limit:
        raise FindingValidationError(
            [], [f"{spec['label']} 超过 {limit} 字符"])
    return text


def _validate_partial(payload: dict) -> dict:
    out: dict = {}
    errors: list[str] = []
    for key in FIELD_ORDER:
        if key not in payload or payload[key] is None:
            continue
        spec = FIELD_SPECS[key]
        if spec.get("int"):
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                errors.append(f"{spec['label']} 必须是 0–100 的整数")
                continue
            if value < 0 or value > 100:
                errors.append(f"{spec['label']} 必须在 0–100 之间")
                continue
            out[key] = value
            continue
        text = _text(None, key, payload[key])
        enum = spec.get("enum")
        if enum is not None and text and text not in enum:
            errors.append(f"{spec['label']} 非法：{text}")
            continue
        out[key] = text
    if "title" in out and not out["title"]:
        errors.append("问题标题不能为空")
    if errors:
        raise FindingValidationError([], errors)
    return out


def _validate_submit(row: dict) -> None:
    def _missing(key: str) -> bool:
        value = row.get(key)
        if key == "confidence":
            return value is None  # 0 是合法置信度，不能当作空值
        return not str(value or "").strip()

    missing = [key for key in REQUIRED_FIELDS if _missing(key)]
    errors: list[str] = []
    for key in ("risk_type", "severity", "fix_priority", "poc_status"):
        spec = FIELD_SPECS[key]
        value = str(row.get(key) or "").strip()
        if value and value not in spec["enum"]:
            errors.append(f"{spec['label']} 非法：{value}")
    try:
        confidence = int(row.get("confidence"))
    except (TypeError, ValueError):
        confidence = -1
    if confidence < 0 or confidence > 100:
        errors.append("问题最终置信度必须在 0–100 之间")
    if str(row.get("poc_status") or "") in ("not_verified", "not_applicable") \
            and not str(row.get("poc") or "").strip():
        errors.append("PoC 豁免时必须说明未验证/不适用的原因")
    if missing or errors:
        raise FindingValidationError(missing, errors)


def is_reviewer(conn: sqlite3.Connection, task_id: str, role: str | None) -> bool:
    if not role:
        return False
    return conn.execute(
        "SELECT 1 FROM task_reviewers WHERE task_id=? AND role_slug=? LIMIT 1",
        (task_id, role),
    ).fetchone() is not None


def _require_reviewer(conn: sqlite3.Connection, task_id: str, role: str | None) -> None:
    if not is_reviewer(conn, task_id, role):
        raise PermissionError("only the task's reviewers can write findings")


def reviewer_roles(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [r["role_slug"] for r in conn.execute(
        "SELECT role_slug FROM task_reviewers WHERE task_id=? ORDER BY added_at, role_slug",
        (task_id,),
    ).fetchall()]


def _board_row(conn: sqlite3.Connection, task_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM task_finding_boards WHERE task_id=?", (task_id,)
    ).fetchone())


def board_status(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = _board_row(conn, task_id)
    return str(row["status"]) if row is not None else None


def ensure_board(conn: sqlite3.Connection, task_id: str) -> dict:
    now = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO task_finding_boards "
        "(task_id, status, summary, created_at, updated_at) VALUES (?,?,?,?,?)",
        (task_id, "in_progress", None, now, now),
    )
    return _board_row(conn, task_id) or {
        "task_id": task_id, "status": "in_progress", "summary": None,
        "created_at": now, "updated_at": now,
    }


def get_finding(conn: sqlite3.Connection, finding_id: str,
                task_id: str | None = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM task_findings WHERE id=?", (finding_id,)
    ).fetchone()
    if row is None:
        return None
    if task_id is not None and row["task_id"] != task_id:
        return None
    return _row(row)


def list_findings(conn: sqlite3.Connection, task_id: str, *,
                  status: str | None = None, severity: str | None = None,
                  risk_type: str | None = None, q: str | None = None,
                  limit: int = 500) -> list[dict]:
    _enabled_task(conn, task_id)
    clauses = ["task_id=?"]
    params: list = [task_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if severity:
        clauses.append("severity=?")
        params.append(severity)
    if risk_type:
        clauses.append("risk_type=?")
        params.append(risk_type)
    if q and str(q).strip():
        like = f"%{str(q).strip()}%"
        clauses.append("(title LIKE ? OR module LIKE ? OR function_name LIKE ? "
                       "OR file_path LIKE ? OR risk_description LIKE ?)")
        params.extend([like] * 5)
    params.append(max(1, min(1000, int(limit or 500))))
    rows = conn.execute(
        f"SELECT * FROM task_findings WHERE {' AND '.join(clauses)} "
        "ORDER BY updated_at DESC, id DESC LIMIT ?", params
    ).fetchall()
    return [_row(r) for r in rows]


def _infer_run_context(conn: sqlite3.Connection, task_id: str,
                       role: str) -> tuple[str | None, str | None, str | None]:
    row = conn.execute(
        "SELECT id, workflow_id, step_id FROM runs WHERE task_id=? AND role_slug=? "
        "ORDER BY started_at DESC LIMIT 1", (task_id, role)
    ).fetchone()
    if row is None:
        return None, None, None
    return row["workflow_id"], row["step_id"], row["id"]


def create_finding(conn: sqlite3.Connection, task_id: str, role: str,
                   payload: dict, *, workflow_id: str | None = None,
                   step_id: str | None = None, run_id: str | None = None) -> dict:
    _enabled_task(conn, task_id)
    _require_reviewer(conn, task_id, role)
    board = ensure_board(conn, task_id)
    if board["status"] != "in_progress":
        raise ConflictError("issue board is completed; reopen it before adding findings")
    fields = _validate_partial(payload)
    if not fields.get("title"):
        raise FindingValidationError(["title"], ["问题标题不能为空"])
    ctx_wf, ctx_step, ctx_run = _infer_run_context(conn, task_id, role)
    fid = new_id()
    now = now_iso()
    values = {
        "id": fid, "task_id": task_id, "author_role": role,
        "workflow_id": workflow_id or ctx_wf,
        "step_id": step_id or ctx_step,
        "run_id": run_id or ctx_run,
        "status": "draft", "version": 1,
        "created_at": now, "updated_at": now,
    }
    values.update({key: fields.get(key, 0 if key == "confidence" else "")
                   for key in FIELD_ORDER})
    cols = ", ".join(values)
    conn.execute(
        f"INSERT INTO task_findings ({cols}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    _audit(conn, role, task_id, "finding.create", {"finding_id": fid, "title": fields["title"]})
    conn.commit()
    return get_finding(conn, fid)  # type: ignore[return-value]


def update_finding(conn: sqlite3.Connection, task_id: str, finding_id: str,
                   role: str, payload: dict) -> dict:
    _enabled_task(conn, task_id)
    _require_reviewer(conn, task_id, role)
    finding = get_finding(conn, finding_id, task_id)
    if finding is None:
        raise LookupError(finding_id)
    if finding["status"] not in ("draft", "changes_requested"):
        raise ConflictError(
            f"finding is {finding['status']}; only draft/changes_requested can be edited")
    board = ensure_board(conn, task_id)
    if board["status"] != "in_progress":
        raise ConflictError("issue board is completed; reopen it before editing findings")
    fields = _validate_partial(payload)
    if not fields:
        return finding
    sets = ", ".join(f"{key}=?" for key in fields)
    conn.execute(
        f"UPDATE task_findings SET {sets}, version=version+1, updated_at=? WHERE id=?",
        (*fields.values(), now_iso(), finding_id),
    )
    _audit(conn, role, task_id, "finding.update",
           {"finding_id": finding_id, "fields": sorted(fields)})
    conn.commit()
    return get_finding(conn, finding_id, task_id)  # type: ignore[return-value]


def submit_finding(conn: sqlite3.Connection, task_id: str, finding_id: str,
                   role: str) -> dict:
    _enabled_task(conn, task_id)
    _require_reviewer(conn, task_id, role)
    finding = get_finding(conn, finding_id, task_id)
    if finding is None:
        raise LookupError(finding_id)
    if finding["status"] not in ("draft", "changes_requested"):
        raise ConflictError(f"finding is {finding['status']}; cannot submit")
    board = ensure_board(conn, task_id)
    if board["status"] != "in_progress":
        raise ConflictError("issue board is completed; reopen it before submitting findings")
    _validate_submit(finding)
    now = now_iso()
    conn.execute(
        "UPDATE task_findings SET status='submitted', submitted_at=?, version=version+1, "
        "decided_by=NULL, decided_at=NULL, decision_note=NULL, updated_at=? WHERE id=?",
        (now, now, finding_id),
    )
    _audit(conn, role, task_id, "finding.submit",
           {"finding_id": finding_id, "severity": finding["severity"]})
    conn.commit()
    _comment(conn, task_id, role,
             f"问题板：提交 finding「{finding['title']}」（{SEVERITIES.get(finding['severity'], finding['severity'])}）。")
    return get_finding(conn, finding_id, task_id)  # type: ignore[return-value]


def delete_finding(conn: sqlite3.Connection, task_id: str, finding_id: str,
                   by: str = "human") -> bool:
    _enabled_task(conn, task_id)
    finding = get_finding(conn, finding_id, task_id)
    if finding is None:
        raise LookupError(finding_id)
    if by not in (None, "human"):
        _require_reviewer(conn, task_id, by)
        if finding["status"] not in ("draft", "changes_requested"):
            raise ConflictError(
                "only draft/changes_requested findings can be deleted")
    conn.execute("DELETE FROM task_findings WHERE id=?", (finding_id,))
    _audit(conn, by, task_id, "finding.delete",
           {"finding_id": finding_id, "title": finding["title"]})
    conn.commit()
    return True


def _decide(conn: sqlite3.Connection, task_id: str, finding_id: str,
            decided_by: str, status: str, note: str | None) -> dict:
    _enabled_task(conn, task_id)
    finding = get_finding(conn, finding_id, task_id)
    if finding is None:
        raise LookupError(finding_id)
    if finding["status"] != "submitted":
        raise ConflictError(f"finding is {finding['status']}; expected submitted")
    now = now_iso()
    label = "确认真实问题" if status == "confirmed" else "标记误报"
    conn.execute(
        "UPDATE task_findings SET status=?, decided_by=?, decided_at=?, decision_note=?, "
        "version=version+1, updated_at=? WHERE id=?",
        (status, decided_by, now, (note or "").strip() or None, now, finding_id),
    )
    _audit(conn, decided_by, task_id, f"finding.{status}",
           {"finding_id": finding_id, "note": note})
    conn.commit()
    _comment(conn, task_id, decided_by,
             f"问题板：finding「{finding['title']}」{label}。"
             + (f" 说明：{note.strip()}" if note and note.strip() else ""))
    return get_finding(conn, finding_id, task_id)  # type: ignore[return-value]


def confirm_finding(conn: sqlite3.Connection, task_id: str, finding_id: str,
                    by: str, note: str | None = None) -> dict:
    return _decide(conn, task_id, finding_id, by, "confirmed", note)


def mark_false_positive(conn: sqlite3.Connection, task_id: str, finding_id: str,
                        by: str, note: str | None = None) -> dict:
    return _decide(conn, task_id, finding_id, by, "false_positive", note)


def request_changes(conn: sqlite3.Connection, task_id: str, finding_id: str,
                    by: str, note: str | None) -> dict:
    if not str(note or "").strip():
        raise FindingValidationError([], ["要求重新评估必须填写说明"])
    _enabled_task(conn, task_id)
    finding = get_finding(conn, finding_id, task_id)
    if finding is None:
        raise LookupError(finding_id)
    if finding["status"] not in ("submitted", "confirmed", "false_positive"):
        raise ConflictError(f"finding is {finding['status']}; cannot request changes")
    task = _task(conn, task_id)
    if task["status"] in ("done", "closed"):
        raise ConflictError("task is terminal; reopen it before requesting finding changes")
    now = now_iso()
    conn.execute(
        "UPDATE task_findings SET status='changes_requested', decided_by=?, decided_at=?, "
        "decision_note=?, version=version+1, updated_at=? WHERE id=?",
        (by, now, note.strip(), now, finding_id),
    )
    conn.execute(
        "UPDATE task_finding_boards SET status='in_progress', reopened_by=?, "
        "reopened_at=?, updated_at=? WHERE task_id=?",
        (by, now, now, task_id),
    )
    conn.execute(
        "UPDATE tasks SET status='in_progress', updated_at=? "
        "WHERE id=? AND status='in_review'", (now, task_id))
    _audit(conn, by, task_id, "finding.request_changes",
           {"finding_id": finding_id, "note": note})
    conn.commit()
    from . import tasks as tasks_service
    tasks_service.mention(
        conn, task_id, "human", finding["author_role"],
        f"问题板 finding「{finding['title']}」需要重新评估：{note.strip()}。"
        "请与其他相关角色复核后更新 finding 并重新提交。")
    return get_finding(conn, finding_id, task_id)  # type: ignore[return-value]


def reopen_board(conn: sqlite3.Connection, task_id: str, by: str,
                 note: str | None) -> dict:
    _enabled_task(conn, task_id)
    task = _task(conn, task_id)
    if task["status"] in ("done", "closed"):
        raise ConflictError("task is terminal; reopen it before reopening the issue board")
    board = ensure_board(conn, task_id)
    if board["status"] == "in_progress":
        return board
    now = now_iso()
    conn.execute(
        "UPDATE task_finding_boards SET status='in_progress', reopened_by=?, "
        "reopened_at=?, summary=COALESCE(summary, ?), updated_at=? WHERE task_id=?",
        (by, now, (note or "").strip() or None, now, task_id),
    )
    conn.execute(
        "UPDATE tasks SET status='in_progress', updated_at=? "
        "WHERE id=? AND status='in_review'", (now, task_id))
    _audit(conn, by, task_id, "finding_board.reopen", {"note": note})
    conn.commit()
    from . import tasks as tasks_service
    for role in reviewer_roles(conn, task_id):
        tasks_service.mention(
            conn, task_id, "human", role,
            f"问题板已退回重评：{(note or '').strip() or '请复核 findings'}。")
    return ensure_board(conn, task_id)


def complete_board(conn: sqlite3.Connection, task_id: str, role: str,
                   summary: str) -> dict:
    _enabled_task(conn, task_id)
    _require_reviewer(conn, task_id, role)
    summary = str(summary or "").strip()
    if not summary:
        raise FindingValidationError(["summary"], ["问题板完成说明不能为空"])
    if len(summary) > 4000:
        raise FindingValidationError([], ["问题板完成说明超过 4000 字符"])
    board = ensure_board(conn, task_id)
    if board["status"] == "completed":
        return board
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM task_findings WHERE task_id=? "
        "AND status IN ('draft','changes_requested')", (task_id,)
    ).fetchone()["n"]
    if pending:
        raise ConflictError(
            f"{pending} finding(s) still draft/changes_requested; finish editing before completing the board")
    now = now_iso()
    conn.execute(
        "UPDATE task_finding_boards SET status='completed', summary=?, completed_by=?, "
        "completed_at=?, reopened_by=NULL, reopened_at=NULL, updated_at=? WHERE task_id=?",
        (summary, role, now, now, task_id),
    )
    _audit(conn, role, task_id, "finding_board.complete",
           {"summary": summary})
    conn.commit()
    _comment(conn, task_id, role, f"问题板已完成：{summary}")
    from . import workflow as workflow_service
    workflow_service.finalize_task_review_if_ready(conn, task_id)
    return ensure_board(conn, task_id)


def board_summary(conn: sqlite3.Connection, task_id: str) -> dict:
    task = _enabled_task(conn, task_id)
    board = _board_row(conn, task_id)
    if board is None:
        board = ensure_board(conn, task_id)
        conn.commit()
    counts = {status: 0 for status in STATUSES}
    for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM task_findings WHERE task_id=? GROUP BY status",
        (task_id,),
    ).fetchall():
        counts[r["status"]] = int(r["n"])
    severity = {s: 0 for s in SEVERITIES}
    for r in conn.execute(
        "SELECT severity, COUNT(*) AS n FROM task_findings WHERE task_id=? "
        "AND status NOT IN ('false_positive') GROUP BY severity", (task_id,),
    ).fetchall():
        if r["severity"] in severity:
            severity[r["severity"]] = int(r["n"])
    total = sum(counts.values())
    pending = counts["submitted"] + counts["changes_requested"] + counts["draft"]
    can_complete = (board["status"] == "in_progress"
                    and counts["draft"] == 0 and counts["changes_requested"] == 0)
    all_decided = total == 0 or (
        counts["confirmed"] + counts["false_positive"] == total)
    return {
        "task_id": task_id,
        "task_status": task["status"],
        "board": board,
        "reviewers": reviewer_roles(conn, task_id),
        "counts": counts,
        "severity_counts": severity,
        "total": total,
        "pending": pending,
        "can_complete": can_complete,
        "all_decided": all_decided,
    }


def assert_ready_for_task_review(conn: sqlite3.Connection, task_id: str) -> None:
    task = _enabled_task(conn, task_id)
    board = _board_row(conn, task_id)
    if board is None or board["status"] != "completed":
        raise ConflictError(
            "issue board is not completed; finish all findings and mark the board complete "
            "before approving the task")
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM task_findings WHERE task_id=? "
        "AND status NOT IN ('confirmed','false_positive') GROUP BY status",
        (task_id,),
    ).fetchall()
    pending = {r["status"]: int(r["n"]) for r in rows}
    if pending:
        detail = "、".join(f"{k}={v}" for k, v in sorted(pending.items()))
        raise ConflictError(
            f"issue board still has unconfirmed findings ({detail}); "
            "confirm or mark them false-positive before approving the task")
    # task status is used by callers; keep it referenced for lint clarity
    _ = task


def add_reviewers(conn: sqlite3.Connection, task_id: str, roles: list[str],
                  *, by: str = "human", source: str = "human") -> dict:
    _enabled_task(conn, task_id)
    from . import roles as roles_service
    from . import tasks as tasks_service
    from . import workflow as workflow_service
    clean = list(dict.fromkeys(str(r).strip() for r in roles if str(r).strip()))
    if not clean:
        raise FindingValidationError(["roles"], ["至少指定一个 reviewer 角色"])
    for slug in clean:
        role = roles_service.get(conn, slug)
        if role is None:
            raise ConflictError(f"role {slug} does not exist")
        if not role.get("enabled", True):
            raise ConflictError(f"role {slug} is disabled")
        if roles_service.is_onboarding(conn, slug):
            raise ConflictError(f"role {slug} is the onboarding assistant and cannot review tasks")
    running = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? AND status='running' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)
    ).fetchone()
    if running is not None:
        wf = workflow_service.get_workflow(conn, running["id"])
        definition = (wf or {}).get("definition") or {}
        allowed = workflow_service._workflow_roles(definition)
        allowed.update(str(s["role_slug"]) for s in (wf or {}).get("steps") or []
                       if s.get("role_slug"))
        missing = [r for r in clean if r not in allowed]
        if missing:
            raise ConflictError(
                "running workflow must cover reviewer(s): " + ", ".join(missing)
                + "; revise/replan the workflow first")
    tasks_service.add_assignees(conn, task_id, clean)
    now = now_iso()
    for slug in clean:
        conn.execute(
            "INSERT OR IGNORE INTO task_reviewers (task_id, role_slug, source, added_at) "
            "VALUES (?,?,?,?)", (task_id, slug, source, now))
    _audit(conn, by, task_id, "finding_board.reviewers_add",
           {"roles": clean, "source": source})
    conn.commit()
    return board_summary(conn, task_id)


def remove_reviewer(conn: sqlite3.Connection, task_id: str, role: str,
                    *, by: str = "human") -> dict:
    _enabled_task(conn, task_id)
    roles = reviewer_roles(conn, task_id)
    if role not in roles:
        raise LookupError(role)
    if len(roles) <= 1:
        raise ConflictError("cannot remove the last reviewer while the issue board is enabled")
    conn.execute("DELETE FROM task_reviewers WHERE task_id=? AND role_slug=?",
                 (task_id, role))
    _audit(conn, by, task_id, "finding_board.reviewers_remove", {"role": role})
    conn.commit()
    return board_summary(conn, task_id)


def _audit(conn: sqlite3.Connection, role: str | None, task_id: str,
           action: str, payload: dict | None = None) -> None:
    from . import tasks as tasks_service
    tasks_service._audit(conn, role, task_id, action, payload or {})


def _comment(conn: sqlite3.Connection, task_id: str, role: str, body: str) -> None:
    from . import tasks as tasks_service
    tasks_service.comment(conn, task_id, role, body)
