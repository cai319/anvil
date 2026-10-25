"""Task replay log export (plain text).

The log is intentionally human-readable and self-contained: task metadata,
workflow executions/steps, then the full append-only event stream in order.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..util import jdumps, jloads, now_iso


def _short(value, length: int = 8) -> str:
    text = str(value or "")
    return text[:length] if text else ""


def _kv(payload: dict, *keys: str) -> str:
    parts = []
    for key in keys:
        if key in payload and payload[key] not in (None, "", [], {}):
            parts.append(f"{key}={payload[key]}")
    return " ".join(parts)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in str(text).splitlines())


def _payload_lines(event_type: str, payload: dict) -> list[str]:
    if event_type.endswith("comment") or event_type == "message.comment":
        return [str(payload.get("body") or "")]
    if event_type.endswith("mention") or event_type == "message.mention":
        return [f"@{payload.get('target', 'human')}: {payload.get('message', '')}"]
    if event_type.startswith("step."):
        head = (f"STEP {payload.get('step', '')} "
                f"{payload.get('title', '')} -> {payload.get('status', '')}")
        extras = []
        if payload.get("summary"):
            extras.append(str(payload["summary"]))
        if payload.get("failure_reason"):
            extras.append(f"failure_reason={payload['failure_reason']}")
        if payload.get("rework_count") is not None:
            extras.append(f"rework_count={payload['rework_count']}")
        if payload.get("slice_ids"):
            extras.append("slices=" + ",".join(str(x) for x in payload["slice_ids"]))
        return [head, *extras]
    if event_type == "run.status_changed":
        return [f"RUN {_short(payload.get('run_id'), 8)} "
                f"{payload.get('prev', '?')} -> {payload.get('status', '?')}"]
    if event_type == "gate.packet_created":
        return [f"PACKET {_short(payload.get('packet_id'))} rev={payload.get('revision')} "
                f"authority={payload.get('authority')} "
                f"evidence={payload.get('evidence_count', 0)} "
                f"stale={payload.get('stale', False)}"]
    if event_type == "decision.recorded":
        return [f"DECISION {payload.get('decision')} "
                f"authority={payload.get('authority')} "
                f"closed={payload.get('closed', True)}",
                f"feedback={payload.get('feedback') or ''}",
                f"reasons={','.join(payload.get('reasons') or [])}",
                f"rework_targets={','.join(payload.get('rework_targets') or [])}",
                f"override_reason={payload.get('override_reason') or ''}"]
    if event_type.startswith("execution."):
        return [f"{event_type.upper()} {_kv(payload, 'previous_workflow_id', 'template_id', 'weight', 'reason', 'steps')}".strip()]
    if event_type.startswith("audit."):
        return [_kv(payload, "reason", "detail", "approve") or jdumps(payload)]
    if event_type.startswith("task.") or event_type.startswith("board."):
        return [jdumps(payload)]
    return [jdumps(payload)]


def _task_header(conn: sqlite3.Connection, task_id: str) -> tuple[dict, list[str]]:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise LookupError(task_id)
    task = dict(row)
    roles = [r["role_slug"] for r in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at",
        (task_id,)).fetchall()]
    return task, roles


def _workflow_lines(conn: sqlite3.Connection, task_id: str) -> list[str]:
    lines: list[str] = ["", "# Workflow executions"]
    workflows = conn.execute(
        "SELECT * FROM workflows WHERE task_id=? ORDER BY execution_no, created_at",
        (task_id,)).fetchall()
    if not workflows:
        lines.append("(no workflow)")
        return lines
    for wf in workflows:
        lines.append("")
        lines.append(
            f"## Execution {wf['execution_no']} · {wf['status']} · weight={wf['weight']} "
            f"· workflow={wf['id']}")
        finished = (wf["finished_at"] or wf["terminated_at"]
                    or (wf["updated_at"] if wf["status"] != "running" else None))
        lines.append(
            f"created={wf['created_at']} finished={finished or '—'} "
            f"source={wf['weight_source']}")
        if wf["termination_reason"]:
            lines.append(f"termination_reason={wf['termination_reason']}")
        for st in conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY rowid",
                (wf["id"],)).fetchall():
            extras = []
            if st["started_at"] or st["ended_at"]:
                extras.append(f"{st['started_at'] or '—'} → {st['ended_at'] or '…'}")
            if st["run_id"]:
                extras.append(f"run={st['run_id']}")
            if st["gate"]:
                extras.append("gate")
            if st["kind"] and st["kind"] != "task":
                extras.append(f"kind={st['kind']}")
            if st["decision_authority"]:
                extras.append(f"authority={st['decision_authority']}")
            if st["rework_count"]:
                extras.append(f"rework={st['rework_count']}/{st['max_rework']}")
            lines.append(
                f"- {st['step_id']} [{st['status']}] {st['role_slug']} · {st['title']}"
                + (f" · {' · '.join(extras)}" if extras else ""))
            if st["summary"]:
                lines.append(f"    summary: {st['summary']}")
            if st["feedback"]:
                lines.append(f"    feedback: {st['feedback']}")
            if st["evidence_json"] and st["evidence_json"] != "[]":
                lines.append(f"    evidence: {st['evidence_json']}")
    return lines


def render_log(conn: sqlite3.Connection, task_id: str) -> str:
    """Render the complete task replay log as plain text."""
    task, roles = _task_header(conn, task_id)
    tags = jloads(task.get("tags_json") or "[]", [])
    events = conn.execute(
        "SELECT * FROM events WHERE task_id=? ORDER BY ts, id", (task_id,)).fetchall()
    if events:
        entries = [{
            "ts": e["ts"], "event_type": e["event_type"], "actor_type": e["actor_type"],
            "actor": e["actor"], "workflow_id": e["workflow_id"],
            "execution_no": e["execution_no"], "step_id": e["step_id"],
            "slice_key": e["slice_key"], "run_id": e["run_id"],
            "payload": jloads(e["payload_json"] or "{}", {}),
        } for e in events]
    else:
        activities = conn.execute(
            "SELECT * FROM activities WHERE task_id=? ORDER BY created_at, id",
            (task_id,)).fetchall()
        entries = [{
            "ts": a["created_at"], "event_type": f"activity.{a['kind']}",
            "actor_type": "human" if a["role_slug"] == "human" else "role",
            "actor": a["role_slug"], "workflow_id": a["workflow_id"],
            "execution_no": a["execution_no"], "step_id": a["step_id"],
            "slice_key": a["slice_key"], "run_id": a["run_id"],
            "payload": jloads(a["payload_json"] or "{}", {}),
        } for a in activities]

    now = now_iso()
    lines = [
        "# ============================================================",
        "# Anvil Task Replay Log",
        "# ============================================================",
        f"Task ID    : {task['id']}",
        f"Title      : {task['title']}",
        f"Status     : {task['status']}",
        f"Created    : {task['created_at']}",
        f"Updated    : {task['updated_at']}",
        f"Exported   : {now}",
        f"Tags       : {', '.join(tags) or '—'}",
        f"Assignees  : {', '.join(roles) or '—'}",
        f"Events     : {len(entries)}",
        "",
        "# Workflow executions",
    ]
    lines.extend(_workflow_lines(conn, task_id)[2:])
    lines += ["", "# Event log", ""]
    for index, ev in enumerate(entries, 1):
        ctx = []
        if ev["workflow_id"]:
            ctx.append(f"wf={_short(ev['workflow_id'])}")
        if ev["execution_no"]:
            ctx.append(f"exec={ev['execution_no']}")
        if ev["step_id"]:
            ctx.append(f"step={ev['step_id']}")
        if ev["slice_key"]:
            ctx.append(f"slice={ev['slice_key']}")
        if ev["run_id"]:
            ctx.append(f"run={_short(ev['run_id'])}")
        actor = ev["actor"] or ev["actor_type"]
        lines.append(
            f"[{ev['ts']}] #{index:04d} [{ev['event_type']}] "
            f"{ev['actor_type']}:{actor}" + (f" ({' '.join(ctx)})" if ctx else ""))
        for body in _payload_lines(ev["event_type"], ev["payload"]):
            if body:
                lines.append(_indent(body))
        lines.append("")
    lines.append("# End of replay log · exported by Anvil")
    return "\n".join(lines) + "\n"


def log_filename(task_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"replay-{task_id[:8]}-{stamp}.log"
