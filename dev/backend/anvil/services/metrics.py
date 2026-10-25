"""Workflow metrics and planned/actual graphs (computed, not materialized)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..util import jloads


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _seconds(a, b) -> float:
    start, end = _parse(a), _parse(b)
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _workflow(conn: sqlite3.Connection, wid: str) -> dict | None:
    row = conn.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    return dict(row) if row else None


def _steps(conn: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for row in conn.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY rowid", (wid,)):
        d = dict(row)
        d["depends_on"] = jloads(d.pop("depends_on_json", "[]"), [])
        d["gate"] = bool(d["gate"])
        d["kind"] = d.get("kind") or "task"
        out.append(d)
    return out


def _depth(steps: list[dict]) -> int:
    by_id = {s["step_id"]: s for s in steps}
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(sid: str) -> int:
        if sid in memo:
            return memo[sid]
        if sid in visiting:
            return 0
        visiting.add(sid)
        deps = [d for d in (by_id.get(sid, {}).get("depends_on") or []) if d in by_id]
        value = 1 + max((visit(d) for d in deps), default=0)
        visiting.discard(sid)
        memo[sid] = value
        return value

    return max((visit(s) for s in by_id), default=0)


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda x: x[0])
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(max(0.0, (end - start).total_seconds()) for start, end in merged)


def _human_wait_seconds(conn: sqlite3.Connection, wid: str, wf: dict,
                        window_start: str | None, window_end: str | None) -> float:
    """Union of intervals where the workflow was waiting for a human.

    Only packets that actually required a human (human/joint authority, AI
    escalation, or pending_human) are counted, so a human override of a normal
    AI decision does not inflate the wait.
    """
    start_bound, end_bound = _parse(window_start), _parse(window_end)
    if not start_bound or not end_bound:
        return 0.0
    packets = conn.execute(
        "SELECT id, status, authority, created_at FROM gate_packets "
        "WHERE workflow_id=?", (wid,)).fetchall()
    decisions = conn.execute(
        "SELECT packet_id, decider_type, decision, created_at FROM decisions "
        "WHERE workflow_id=? ORDER BY created_at, rowid", (wid,)).fetchall()
    by_packet: dict[str, list[dict]] = {}
    for d in decisions:
        by_packet.setdefault(d["packet_id"], []).append(dict(d))
    intervals: list[tuple[datetime, datetime]] = []
    for packet in packets:
        rows = by_packet.get(packet["id"], [])
        humans = [d for d in rows if d["decider_type"] == "human"]
        escalated = any(d["decision"] == "escalate" for d in rows)
        waiting = (packet["authority"] in ("human", "joint") or escalated
                   or packet["status"] == "pending_human")
        if not waiting:
            continue
        if humans:
            end = min(_parse(d["created_at"]) for d in humans)
        elif packet["status"] == "pending_human":
            end = (datetime.now(timezone.utc) if wf["status"] == "running"
                   else end_bound)
        else:
            continue
        if end is None:
            continue
        handovers = [d for d in rows if d["decider_type"] != "human"
                     and d["decision"] in ("escalate", "recommend_approve")
                     and (_parse(d["created_at"]) or end) <= end]
        start = (min(_parse(d["created_at"]) for d in handovers)
                 if handovers else _parse(packet["created_at"]))
        if start is None:
            continue
        start = max(start, start_bound)
        end = min(end, end_bound)
        if end > start:
            intervals.append((start, end))
    return _union_seconds(intervals)


def _usage(conn: sqlite3.Connection, wid: str, task_id: str) -> dict:
    def agg(where: str, params: tuple) -> dict:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                       COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                       COUNT(*) AS records
                  FROM token_usage WHERE {where}""", params).fetchone()
        data = dict(row) if row else {}
        data["total_tokens"] = (data.get("input_tokens", 0)
                                + data.get("output_tokens", 0)
                                + data.get("cache_read_tokens", 0)
                                + data.get("cache_creation_tokens", 0))
        return data

    workflow = agg(
        "run_id IN (SELECT run_id FROM workflow_steps "
        "WHERE workflow_id=? AND run_id IS NOT NULL)", (wid,))
    task = agg("task_id=?", (task_id,))
    return {
        **workflow,
        # Task-level totals include planner/final-review and any role run that
        # was not directly attached to a workflow step.
        "task_input_tokens": task["input_tokens"],
        "task_output_tokens": task["output_tokens"],
        "task_cache_read_tokens": task["cache_read_tokens"],
        "task_cache_creation_tokens": task["cache_creation_tokens"],
        "task_records": task["records"],
        "task_total_tokens": task["total_tokens"],
    }


def compute(conn: sqlite3.Connection, wid: str) -> dict:
    wf = _workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    steps = _steps(conn, wid)
    counts: dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    done = counts.get("approved", 0) + counts.get("skipped", 0)
    now = datetime.now(timezone.utc).isoformat()
    finished_at = (wf.get("finished_at") or wf.get("terminated_at")
                   or (wf.get("updated_at") if wf["status"] != "running" else None))
    end = finished_at or (now if wf["status"] == "running" else wf.get("updated_at"))

    decisions = conn.execute(
        "SELECT step_id, decider_type, decision, latency_seconds FROM decisions "
        "WHERE workflow_id=? ORDER BY created_at, rowid", (wid,)).fetchall()
    human_decisions = [d for d in decisions if d["decider_type"] == "human"]
    rejects = [d for d in decisions if d["decision"] == "reject"]
    human_wait = sum(float(d["latency_seconds"] or 0) for d in human_decisions)
    human_reviews = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE task_id=? AND event_type='audit.review'",
        (wf["task_id"],)).fetchone()["n"]
    if human_reviews == 0:
        human_reviews = conn.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE task_id=? AND kind='review'",
            (wf["task_id"],)).fetchone()["n"]

    # Legacy executions predate immutable decision rows. Count the terminal
    # outcome of decision steps so replay metrics don't show a misleading zero.
    recorded_steps = {d["step_id"] for d in decisions}
    legacy_decisions: list[str] = []
    for step in steps:
        if step["kind"] != "decision" or step["step_id"] in recorded_steps:
            continue
        reworks = int(step.get("rework_count") or 0)
        if step["status"] == "approved":
            legacy_decisions.extend(["reject"] * reworks)
            legacy_decisions.append("approve")
        elif step["status"] in ("failed", "rejected"):
            legacy_decisions.extend(["reject"] * max(1, reworks))
    legacy_rejects = len([d for d in legacy_decisions if d == "reject"])
    total_decisions = len(decisions) + len(legacy_decisions)
    total_rejects = len(rejects) + legacy_rejects

    packets = conn.execute(
        "SELECT status, created_at FROM gate_packets WHERE workflow_id=?",
        (wid,)).fetchall()
    pending_wait = sum(
        _seconds(p["created_at"], now) for p in packets
        if p["status"] == "pending_human" and wf["status"] == "running")
    evidence = conn.execute(
        "SELECT COUNT(*) AS n FROM gate_packets WHERE workflow_id=?", (wid,)).fetchone()["n"]

    step_roles = {s["role_slug"] for s in steps if s["role_slug"]}
    excluded_wait = _human_wait_seconds(
        conn, wid, wf, wf.get("created_at"), end)
    duration_seconds = _seconds(wf.get("created_at"), end)
    active_seconds = max(0.0, duration_seconds - excluded_wait)
    return {
        "workflow_id": wid,
        "task_id": wf["task_id"],
        "execution_no": wf.get("execution_no") or 1,
        "status": wf["status"],
        "weight": wf.get("weight") or "standard",
        "weight_source": wf.get("weight_source") or "default",
        "created_at": wf.get("created_at"),
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "active_duration_seconds": active_seconds,
        "steps": {
            "total": len(steps),
            "done": done,
            "running": counts.get("running", 0),
            "submitted": counts.get("submitted", 0),
            "failed": counts.get("failed", 0),
            "max_depth": _depth(steps),
            "parallel_roles": len(step_roles),
            "generated_slices": len([s for s in steps if s.get("source_step_id")]),
            "by_status": counts,
        },
        "gates": {
            "total": len([s for s in steps if s["gate"]]),
            "pending": len([s for s in steps
                            if s["gate"] and s["status"] == "submitted"]),
        },
        "decisions": {
            "total": total_decisions,
            "recorded": len(decisions),
            "legacy": len(legacy_decisions),
            "human": len(human_decisions),
            "human_reviews": human_reviews,
            "human_interventions": len(human_decisions) + human_reviews,
            "rejected": total_rejects,
            "pending_human": len([p for p in packets if p["status"] == "pending_human"]),
            "rework_rate": round(total_rejects / total_decisions, 3)
            if total_decisions else 0.0,
        },
        "waiting": {
            # Decision latency is per decision; excluded_seconds is the union
            # of pure human-wait windows inside the workflow wall clock.
            "human_seconds": round(human_wait, 1),
            "pending_human_seconds": round(pending_wait, 1),
            "excluded_seconds": round(excluded_wait, 1),
            "active_seconds": round(active_seconds, 1),
        },
        "evidence": {"packets": evidence},
        "tokens": _usage(conn, wid, wf["task_id"]),
    }


def planned_graph(conn: sqlite3.Connection, wid: str) -> dict:
    wf = _workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    definition = jloads(wf.get("definition_json") or "{}", {})
    nodes = []
    for idx, step in enumerate(definition.get("steps") or definition.get("children") or []):
        sid = str(step.get("id") or step.get("step_id") or f"step_{idx + 1}")
        nodes.append({
            "step_id": sid,
            "title": str(step.get("title") or sid),
            "role_slug": str(step.get("role") or step.get("role_slug") or ""),
            "depends_on": [str(x) for x in (step.get("depends_on") or [])],
            "kind": str(step.get("kind") or "task"),
            "gate": bool(step.get("gate")),
            "decision_authority": str(step.get("decision_authority") or ""),
            "origin": "planned",
        })
    return {"mode": "planned", "workflow_id": wid, "nodes": nodes}


def actual_graph(conn: sqlite3.Connection, wid: str) -> dict:
    wf = _workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    nodes = []
    for s in _steps(conn, wid):
        nodes.append({
            "step_id": s["step_id"],
            "title": s["title"],
            "role_slug": s["role_slug"],
            "depends_on": s["depends_on"],
            "kind": s["kind"],
            "gate": s["gate"],
            "decision_authority": s.get("decision_authority") or "",
            "status": s["status"],
            "run_id": s.get("run_id"),
            "slice_key": s.get("slice_key"),
            "source_step_id": s.get("source_step_id"),
            "rework_count": s.get("rework_count") or 0,
            "origin": "generated" if s.get("source_step_id") else "actual",
        })
    return {"mode": "actual", "workflow_id": wid, "nodes": nodes}


def graph_diff(conn: sqlite3.Connection, wid: str) -> dict:
    planned = planned_graph(conn, wid)
    actual = actual_graph(conn, wid)
    p = {n["step_id"]: n for n in planned["nodes"]}
    a = {n["step_id"]: n for n in actual["nodes"]}
    changed = []
    for sid, node in p.items():
        if sid not in a:
            continue
        current = a[sid]
        for field in ("title", "role_slug", "gate", "decision_authority"):
            if node.get(field) != current.get(field):
                changed.append({"step_id": sid, "field": field,
                                "planned": node.get(field), "actual": current.get(field)})
    return {
        "mode": "diff",
        "workflow_id": wid,
        "added": [n for sid, n in a.items() if sid not in p],
        "missing": [n for sid, n in p.items() if sid not in a],
        "changed": changed,
    }
