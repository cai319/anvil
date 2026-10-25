"""知识库效果评估：命中覆盖、命中次数与治理健康度。

注意：hit_count 只代表知识被检索命中并加入 CIC/one-shot 上下文的次数，
是暴露/使用机会，不是任务收益。真正收益需结合试点对照指标（返工、驳回、
周期、token 成本）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from ..util import jloads, now_iso


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def library_stats(conn: sqlite3.Connection) -> dict:
    now = now_iso()
    today = now[:10]

    approved_rows = conn.execute(
        "SELECT workspace_id, hit_count, expires_at, review_at, conflicts_json "
        "FROM workspace_knowledge WHERE status='approved'"
    ).fetchall()
    proposed_rows = conn.execute(
        "SELECT workspace_id, COUNT(*) AS n FROM workspace_knowledge "
        "WHERE status='proposed' GROUP BY workspace_id"
    ).fetchall()
    proposed_by_ws = {r["workspace_id"]: int(r["n"]) for r in proposed_rows}

    tasks_rows = conn.execute(
        "SELECT workspace_id, COUNT(*) AS n FROM tasks "
        "WHERE archived_at IS NULL AND workspace_id IS NOT NULL GROUP BY workspace_id"
    ).fetchall()
    tasks_by_ws = {r["workspace_id"]: int(r["n"]) for r in tasks_rows}

    workspaces = conn.execute(
        "SELECT id, name FROM workspaces ORDER BY name"
    ).fetchall()
    ws_name = {r["id"]: r["name"] for r in workspaces}

    per_ws: dict[str, dict] = defaultdict(lambda: {
        "approved": 0, "used": 0, "hits": 0, "zero_hit": 0,
    })
    totals = {
        "approved": 0, "used": 0, "hits": 0, "zero_hit": 0,
        "proposed": sum(proposed_by_ws.values()),
        "expired": 0, "review_due": 0, "conflicts": 0,
    }
    for row in approved_rows:
        ws_id = row["workspace_id"]
        bucket = per_ws[ws_id]
        hits = int(row["hit_count"] or 0)
        bucket["approved"] += 1
        bucket["hits"] += hits
        totals["approved"] += 1
        totals["hits"] += hits
        if hits > 0:
            bucket["used"] += 1
            totals["used"] += 1
        else:
            bucket["zero_hit"] += 1
            totals["zero_hit"] += 1
        expires_at = str(row["expires_at"] or "")
        if expires_at and expires_at <= now:
            totals["expired"] += 1
        review_at = str(row["review_at"] or "")
        if review_at and review_at[:10] <= today:
            totals["review_due"] += 1
        if jloads(row["conflicts_json"], []):
            totals["conflicts"] += 1

    channels: list[dict] = []
    for ws in workspaces:
        ws_id = ws["id"]
        bucket = per_ws.get(ws_id, {"approved": 0, "used": 0, "hits": 0, "zero_hit": 0})
        approved = bucket["approved"]
        tasks = tasks_by_ws.get(ws_id, 0)
        channels.append({
            "workspace_id": ws_id,
            "workspace_name": ws_name.get(ws_id) or ws_id,
            "approved": approved,
            "used": bucket["used"],
            "hits": bucket["hits"],
            "zero_hit": bucket["zero_hit"],
            "proposed": proposed_by_ws.get(ws_id, 0),
            "tasks": tasks,
            "hit_rate": _rate(bucket["used"], approved),
            "avg_hits_per_card": _rate(bucket["hits"], approved),
            "hits_per_task": _rate(bucket["hits"], tasks),
        })
    channels.sort(key=lambda item: (-item["approved"], item["workspace_name"]))

    task_active = conn.execute(
        "SELECT COUNT(*) FROM task_knowledge WHERE status='active'"
    ).fetchone()[0]
    tasks_with_knowledge = conn.execute(
        "SELECT COUNT(DISTINCT task_id) FROM task_knowledge WHERE status='active'"
    ).fetchone()[0]
    tasks_total = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE archived_at IS NULL"
    ).fetchone()[0]

    lesson_counts = {
        r["status"]: int(r["n"])
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM lessons GROUP BY status")
    }
    role_notes_total = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE scope='role' AND status='active'"
    ).fetchone()[0]
    roles_with_notes = conn.execute(
        "SELECT COUNT(DISTINCT role_slug) FROM notes WHERE scope='role' AND status='active'"
    ).fetchone()[0]

    totals["hit_rate"] = _rate(totals["used"], totals["approved"])
    totals["avg_hits_per_card"] = _rate(totals["hits"], totals["approved"])

    return {
        "channels": channels,
        "totals": totals,
        "task_knowledge": {
            "active": int(task_active),
            "tasks_with_knowledge": int(tasks_with_knowledge),
            "tasks_total": int(tasks_total),
            "coverage": _rate(int(tasks_with_knowledge), int(tasks_total)),
        },
        "lessons": {
            "pending": lesson_counts.get("pending", 0),
            "approved": lesson_counts.get("approved", 0),
        },
        "role_knowledge": {
            "notes_total": int(role_notes_total),
            "roles_with_notes": int(roles_with_notes),
            "usage_tracked": False,
        },
        "generated_at": now,
        "note": (
            "hit_count 统计知识被检索命中并加入 CIC/one-shot 上下文的次数，"
            "代表暴露/使用机会，不等同于任务收益；目前仅频道知识接入该计数，"
            "任务/角色/共享经验未追踪。收益需结合返工、评审驳回、周期和 token 成本的试点对照评估。"
        ),
    }
