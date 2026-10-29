"""Execution dossier: an immutable, exportable snapshot of one workflow execution."""
from __future__ import annotations

import hashlib
import sqlite3

from ..util import jdumps, jloads, now_iso
from . import metrics


def _packets(conn: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for row in conn.execute(
            "SELECT * FROM gate_packets WHERE workflow_id=? ORDER BY revision, created_at",
            (wid,)).fetchall():
        d = dict(row)
        for key in ("options_json", "acceptance_json", "evidence_json",
                    "upstream_json", "risks_json", "recommendation_json"):
            d[key[:-5]] = jloads(d.pop(key), {} if key == "recommendation_json" else [])
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE packet_id=? "
            "ORDER BY created_at, rowid", (d["id"],)).fetchall()
        d["decisions"] = []
        for dec in decisions:
            item = dict(dec)
            item["reasons"] = jloads(item.pop("reasons_json", "[]"), [])
            item["rework_targets"] = jloads(item.pop("rework_targets_json", "[]"), [])
            d["decisions"].append(item)
        out.append(d)
    return out


def _steps(conn: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for row in conn.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY rowid", (wid,)):
        d = dict(row)
        d["depends_on"] = jloads(d.pop("depends_on_json", "[]"), [])
        d["acceptance"] = jloads(d.pop("acceptance_json", "[]"), [])
        d["evidence"] = jloads(d.pop("evidence_json", "[]"), [])
        d["on_reject"] = jloads(d.pop("on_reject_json", "[]"), [])
        d["gate"] = bool(d["gate"])
        d["reversible"] = bool(d.get("reversible", 1))
        out.append(d)
    return out


def build(conn: sqlite3.Connection, wid: str) -> dict:
    wf = conn.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    if wf is None:
        raise LookupError(wid)
    workflow = dict(wf)
    workflow.pop("definition_json", None)
    event_counts = {
        row["event_type"]: row["n"] for row in conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM events WHERE workflow_id=? "
            "GROUP BY event_type", (wid,)).fetchall()
    }
    payload = {
        "workflow": workflow,
        "metrics": metrics.compute(conn, wid),
        "planned": metrics.planned_graph(conn, wid),
        "actual": metrics.actual_graph(conn, wid),
        "diff": metrics.graph_diff(conn, wid),
        "steps": _steps(conn, wid),
        "packets": _packets(conn, wid),
        "event_counts": event_counts,
        "generated_at": now_iso(),
    }
    digest = hashlib.sha256(
        jdumps({k: v for k, v in payload.items() if k != "generated_at"}
               ).encode("utf-8")).hexdigest()
    payload["digest"] = digest
    return payload


def persist(conn: sqlite3.Connection, wid: str, *, commit: bool = True) -> dict | None:
    try:
        payload = build(conn, wid)
    except LookupError:
        return None
    wf = payload["workflow"]
    conn.execute(
        "INSERT INTO workflow_dossiers (workflow_id, task_id, execution_no, weight, "
        "status, definition_hash, dossier_json, digest, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(workflow_id) DO UPDATE SET dossier_json=excluded.dossier_json, "
        "digest=excluded.digest, status=excluded.status, "
        "definition_hash=excluded.definition_hash, created_at=excluded.created_at",
        (wid, wf["task_id"], wf.get("execution_no") or 1, wf.get("weight"),
         wf["status"], wf.get("definition_hash"), jdumps(payload),
         payload["digest"], now_iso()))
    if commit:
        conn.commit()
    return payload


def get(conn: sqlite3.Connection, wid: str, *, rebuild: bool = False) -> dict | None:
    if rebuild:
        return persist(conn, wid)
    row = conn.execute(
        "SELECT dossier_json FROM workflow_dossiers WHERE workflow_id=?", (wid,)).fetchone()
    if row is not None:
        return jloads(row["dossier_json"], {})
    # Historical executions predate dossiers: build lazily but don't persist
    # unless the caller explicitly rebuilds.
    wf = conn.execute("SELECT status FROM workflows WHERE id=?", (wid,)).fetchone()
    if wf is None:
        return None
    return build(conn, wid)


def render_md(dossier: dict) -> str:
    wf = dossier.get("workflow") or {}
    m = dossier.get("metrics") or {}
    steps = dossier.get("steps") or []
    packets = dossier.get("packets") or []
    lines = [
        f"# 执行档案 · 第 {wf.get('execution_no', 1)} 次执行",
        "",
        f"- 任务：{wf.get('task_id')}",
        f"- 工作流：{wf.get('id')}",
        f"- 状态：{wf.get('status')}",
        f"- 流程强度：{wf.get('weight') or 'standard'}（{wf.get('weight_source') or 'default'}）",
        f"- 创建：{wf.get('created_at')}",
        f"- 结束：{wf.get('finished_at') or wf.get('terminated_at') or '—'}",
        f"- 摘要：`{dossier.get('digest')}`",
        "",
        "## 指标",
        "",
        f"- 步骤：{m.get('steps', {}).get('done', 0)}/{m.get('steps', {}).get('total', 0)} 完成",
        f"- 最大深度：{m.get('steps', {}).get('max_depth', 0)}",
        f"- 决策：{m.get('decisions', {}).get('total', 0)}（人工 {m.get('decisions', {}).get('human', 0)}，"
        f"打回 {m.get('decisions', {}).get('rejected', 0)}）",
        f"- 人工等待：{m.get('waiting', {}).get('human_seconds', 0)} 秒",
        f"- Token：入 {m.get('tokens', {}).get('input_tokens', 0)} / "
        f"出 {m.get('tokens', {}).get('output_tokens', 0)}",
        "",
        "## 步骤",
        "",
    ]
    for step in steps:
        lines.append(
            f"- `{step['step_id']}` {step.get('title')} · {step.get('role_slug')} · "
            f"{step.get('status')}" + (f" · {step['summary']}" if step.get("summary") else ""))
    lines += ["", "## 决策与证据", ""]
    for packet in packets:
        lines.append(f"### {packet.get('step_id')} · {packet.get('question')}")
        lines.append(f"- 权限：{packet.get('authority')} · 状态：{packet.get('status')}")
        for ev in packet.get("evidence") or []:
            lines.append(
                f"- 证据 [{ev.get('kind')}] {ev.get('ref')}"
                + ("" if ev.get("fresh", True) else "（过期）"))
        for dec in packet.get("decisions") or []:
            lines.append(
                f"- 决策：{dec.get('decider')} {dec.get('decision')} "
                f"{dec.get('feedback') or ''}".rstrip())
        lines.append("")
    lines += ["## 计划 vs 实际", ""]
    diff = dossier.get("diff") or {}
    lines.append(f"- 新增节点：{len(diff.get('added') or [])}")
    lines.append(f"- 缺失节点：{len(diff.get('missing') or [])}")
    lines.append(f"- 变更字段：{len(diff.get('changed') or [])}")
    return "\n".join(lines) + "\n"
