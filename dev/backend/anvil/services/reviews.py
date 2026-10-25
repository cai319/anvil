"""Evidence packets and immutable decision records for gates/decisions."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import ledger

EVIDENCE_KINDS = {"file", "test", "command", "artifact", "url", "note"}
EVIDENCE_TTL_HOURS = 24
MAX_EVIDENCE_ITEMS = 50
MAX_EVIDENCE_REF = 1000
MAX_HASH_BYTES = 5 * 1024 * 1024


class EvidenceError(ValueError):
    """Invalid or unsafe evidence reference."""


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _safe_file(ref: str) -> Path | None:
    """File evidence must stay inside configured repos/agents roots."""
    p = Path(ref)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        raise EvidenceError(f"unsafe evidence path: {ref}")
    for root in (config.REPOS_DIR, config.AGENTS_DIR):
        try:
            candidate = (root / p).resolve()
        except OSError:
            continue
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _file_digest(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def normalize_evidence(items) -> list[dict]:
    """Validate/normalize evidence submitted by a role.

    Relative file paths only (no absolute paths, no ``..``); files that exist
    under the configured repo/agent roots get a sha256 digest. Evidence older
    than the TTL is marked stale.
    """
    if items is None:
        return []
    if isinstance(items, (str, dict)):
        items = [items]
    if not isinstance(items, list):
        raise EvidenceError("evidence must be a list")
    if len(items) > MAX_EVIDENCE_ITEMS:
        raise EvidenceError(f"too many evidence items (max {MAX_EVIDENCE_ITEMS})")
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for index, raw in enumerate(items):
        if isinstance(raw, str):
            raw = {"kind": "note", "ref": raw}
        if not isinstance(raw, dict):
            raise EvidenceError("evidence item must be an object or string")
        kind = str(raw.get("kind") or "note").strip().lower()
        if kind not in EVIDENCE_KINDS:
            raise EvidenceError(f"invalid evidence kind: {kind}")
        ref = str(raw.get("ref") or raw.get("path") or raw.get("command")
                  or raw.get("label") or "").strip()
        if not ref:
            raise EvidenceError("evidence item requires ref/path")
        if len(ref) > MAX_EVIDENCE_REF:
            raise EvidenceError("evidence ref is too long")
        digest = raw.get("digest") or raw.get("sha256")
        if kind == "file":
            path = _safe_file(ref)
            if path is not None and not digest:
                digest = _file_digest(path)
        captured = _iso(raw.get("captured_at")) or now.isoformat()
        captured_dt = datetime.fromisoformat(captured)
        age = now - captured_dt
        fresh = bool(raw.get("fresh", True)) and age <= timedelta(hours=EVIDENCE_TTL_HOURS)
        out.append({
            "id": str(raw.get("id") or f"ev_{index + 1}"),
            "kind": kind,
            "ref": ref,
            "label": str(raw.get("label") or raw.get("title") or ref)[:240],
            "digest": str(digest)[:128] if digest else None,
            "captured_at": captured,
            "fresh": fresh,
        })
    return out


def evidence_digest(evidence: list[dict]) -> str:
    return _digest(evidence)


def packet_has_stale_evidence(packet: dict | None) -> bool:
    return any(not e.get("fresh", True) for e in (packet or {}).get("evidence") or [])


def escalation_triggers(step: dict, packet: dict | None) -> list[str]:
    """Triggers that must stop an AI approval and hand the decision to a human."""
    triggers = set(str(x) for x in jloads(step.get("escalate_json") or "[]", []))
    if not step.get("reversible", True):
        triggers.add("irreversible_action")
    if str(step.get("risk_level") or "normal") == "critical":
        triggers.add("high_risk")
    evidence = (packet or {}).get("evidence") or []
    explicit = [e for e in evidence if e.get("kind") != "summary"]
    if step.get("evidence_required") and not explicit:
        triggers.add("evidence_missing")
    if any(not e.get("fresh", True) for e in evidence):
        triggers.add("evidence_stale")
    if any(a.get("status") == "unmet" for a in (packet or {}).get("acceptance") or []):
        triggers.add("acceptance_unmet")
    return sorted(triggers)


def authority_of(step: dict) -> str:
    value = str(step.get("decision_authority") or "").strip()
    if value in ("ai", "human", "joint"):
        return value
    return "human" if step.get("gate") else ("ai" if step.get("kind") == "decision" else "human")


def _question(step: dict) -> str:
    explicit = str(step.get("decision_question") or "").strip()
    if explicit:
        return explicit
    title = str(step.get("title") or step.get("step_id") or "")
    if step.get("gate"):
        return f"是否接受步骤「{title}」并继续？"
    return f"是否批准步骤「{title}」的结论？"


def _options(step: dict) -> list[dict]:
    raw = jloads(step.get("decision_options_json") or "[]", [])
    if isinstance(raw, list) and raw:
        return [x for x in raw if isinstance(x, dict)][:8]
    return [
        {"id": "approve", "label": "通过", "decision": "approve"},
        {"id": "reject", "label": "打回", "decision": "reject", "requires_note": True},
        {"id": "escalate", "label": "升级人工", "decision": "escalate", "requires_note": True},
    ]


def _acceptance(step: dict, evidence: list[dict]) -> list[dict]:
    refs = [str(e.get("id")) for e in evidence if e.get("id")]
    out = []
    for item in jloads(step.get("acceptance_json") or "[]", []):
        if isinstance(item, dict):
            text = str(item.get("criterion") or item.get("text") or "").strip()
            status = str(item.get("status") or "unknown")
            item_refs = [str(x) for x in item.get("evidence_refs") or refs]
        else:
            text = str(item).strip()
            status, item_refs = "unknown", refs
        if text:
            out.append({"criterion": text, "status": status,
                        "evidence_refs": item_refs})
    return out


def _evidence(step: dict) -> list[dict]:
    provided = step.get("evidence")
    raw = provided if isinstance(provided, list) else jloads(
        step.get("evidence_json") or "[]", [])
    out: list[dict] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, str):
            item = {"kind": "note", "ref": item}
        if not isinstance(item, dict):
            continue
        captured = item.get("captured_at")
        fresh = bool(item.get("fresh", True))
        if captured:
            try:
                dt = datetime.fromisoformat(str(captured))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                fresh = fresh and (datetime.now(timezone.utc) - dt
                                   <= timedelta(hours=EVIDENCE_TTL_HOURS))
            except (TypeError, ValueError):
                pass
        out.append({
            "id": str(item.get("id") or f"ev_{index + 1}"),
            "kind": str(item.get("kind") or "note"),
            "ref": str(item.get("ref") or item.get("path") or ""),
            "label": str(item.get("label") or item.get("ref") or ""),
            "digest": item.get("digest") or item.get("sha256"),
            "captured_at": captured,
            "fresh": fresh,
        })
    summary = str(step.get("summary") or "").strip()
    if summary:
        out.append({
            "id": "summary",
            "kind": "summary",
            "ref": "step.summary",
            "label": summary[:240],
            "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "captured_at": step.get("ended_at") or step.get("updated_at"),
            "fresh": True,
        })
    return out


def _digest(evidence: list[dict]) -> str:
    return hashlib.sha256(jdumps(evidence).encode("utf-8")).hexdigest()


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()


def latest_packet(conn: sqlite3.Connection, workflow_id: str, step_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM gate_packets WHERE workflow_id=? AND step_id=? "
        "ORDER BY revision DESC LIMIT 1", (workflow_id, step_id)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("options_json", "acceptance_json", "evidence_json", "upstream_json",
                "risks_json", "recommendation_json"):
        d[key[:-5]] = jloads(d.pop(key), [] if key != "recommendation_json" else {})
    return d


def decisions_for_step(conn: sqlite3.Connection, workflow_id: str, step_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decisions WHERE workflow_id=? AND step_id=? "
        "ORDER BY created_at, rowid",
        (workflow_id, step_id)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["reasons"] = jloads(d.pop("reasons_json"), [])
        d["rework_targets"] = jloads(d.pop("rework_targets_json"), [])
        out.append(d)
    return out


def ensure_packet(conn: sqlite3.Connection, wf: dict, step: dict,
                  created_by: str = "system", *, commit: bool = True,
                  force: bool = False) -> dict:
    current = latest_packet(conn, wf["id"], step["step_id"])
    if current and current["status"] in ("pending_human", "pending_ai") and not force:
        return current
    revision = max(1, int(step.get("rework_count") or 0) + 1)
    if current and current["revision"] >= revision:
        revision = current["revision"] + 1
    authority = authority_of(step)
    evidence = _evidence(step)
    upstream = []
    by_id = {s["step_id"]: s for s in wf.get("steps") or []}
    for dep in step.get("depends_on") or []:
        up = by_id.get(dep)
        if up:
            upstream.append({
                "step_id": dep, "title": up.get("title"),
                "summary": up.get("summary"), "decision": up.get("status"),
            })
            # Decision packets must show the evidence they are deciding on.
            for item in _evidence(up):
                tagged = {**item, "id": f"{dep}:{item['id']}"}
                if tagged not in evidence:
                    evidence.append(tagged)
    evidence = evidence[:MAX_EVIDENCE_ITEMS]
    risks = jloads(step.get("escalate_json") or "[]", [])
    if not isinstance(risks, list):
        risks = []
    packet_id = new_id()
    now = now_iso()
    previous_recommendation = {}
    if current and current["status"] in ("pending_human", "pending_ai"):
        previous_recommendation = current.get("recommendation") or {}
        conn.execute(
            "UPDATE gate_packets SET status='superseded', superseded_by=? WHERE id=?",
            (packet_id, current["id"]))
    conn.execute(
        "INSERT INTO gate_packets (id, task_id, workflow_id, execution_no, step_id, kind, "
        "revision, status, authority, question, options_json, acceptance_json, evidence_json, "
        "upstream_json, risks_json, recommendation_json, evidence_digest, sla_seconds, "
        "deadline_at, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (packet_id, wf["task_id"], wf["id"], int(wf.get("execution_no") or 1),
         step["step_id"], "gate" if step.get("gate") else "decision", revision,
         "pending_human" if authority in ("human", "joint") else "pending_ai",
         authority, _question(step), jdumps(_options(step)),
         jdumps(_acceptance(step, evidence)), jdumps(evidence), jdumps(upstream),
         jdumps(risks), jdumps(previous_recommendation), _digest(evidence),
         24 * 3600, _deadline(), created_by, now),
    )
    conn.execute(
        "UPDATE workflow_steps SET packet_id=?, submitted_at=? "
        "WHERE workflow_id=? AND step_id=?",
        (packet_id, now, wf["id"], step["step_id"]))
    ledger.emit(
        conn, "gate.packet_created", task_id=wf["task_id"], workflow_id=wf["id"],
        execution_no=wf.get("execution_no"), step_id=step["step_id"],
        actor_type="system" if created_by in ("system", "api") else "role",
        actor=created_by,
        payload={"packet_id": packet_id, "revision": revision,
                 "authority": authority, "evidence_count": len(evidence),
                 "stale": any(not e.get("fresh", True) for e in evidence)},
    )
    if commit:
        conn.commit()
    return latest_packet(conn, wf["id"], step["step_id"]) or {}


def record_decision(conn: sqlite3.Connection, wf: dict, step: dict, *,
                    actor_type: str, actor: str, decision: str,
                    feedback: str | None = None,
                    reasons: list[str] | None = None,
                    rework_targets: list[str] | None = None,
                    override_reason: str | None = None,
                    packet_id: str | None = None,
                    closes_packet: bool = True,
                    commit: bool = True) -> dict:
    packet = latest_packet(conn, wf["id"], step["step_id"])
    if packet_id and (packet is None or packet["id"] != packet_id):
        raise ConflictError("packet_superseded: refresh the review packet")
    if packet is None:
        packet = ensure_packet(conn, wf, step, created_by=actor, commit=commit)
    now = now_iso()
    latency = None
    try:
        start = datetime.fromisoformat(str(packet["created_at"]))
        end = datetime.fromisoformat(now)
        latency = max(0.0, (end - start).total_seconds())
    except (TypeError, ValueError):
        pass
    decision_id = new_id()
    conn.execute(
        "INSERT INTO decisions (id, packet_id, task_id, workflow_id, execution_no, step_id, "
        "authority, decider_type, decider, decision, feedback, reasons_json, "
        "rework_targets_json, evidence_digest, override_reason, latency_seconds, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, packet["id"], wf["task_id"], wf["id"],
         int(wf.get("execution_no") or 1), step["step_id"], packet["authority"],
         actor_type, actor, decision, feedback, jdumps(reasons or []),
         jdumps(rework_targets or []), packet.get("evidence_digest"),
         override_reason, latency, now),
    )
    if closes_packet:
        conn.execute(
            "UPDATE gate_packets SET status='decided' WHERE id=?", (packet["id"],))
    conn.execute(
        "UPDATE workflow_steps SET packet_id=? WHERE workflow_id=? AND step_id=?",
        (packet["id"], wf["id"], step["step_id"]))
    ledger.emit(
        conn, "decision.recorded", task_id=wf["task_id"], workflow_id=wf["id"],
        execution_no=wf.get("execution_no"), step_id=step["step_id"],
        actor_type=actor_type, actor=actor,
        payload={"packet_id": packet["id"], "decision": decision,
                 "authority": packet.get("authority"),
                 "feedback": feedback,
                 "rework_targets": rework_targets or [],
                 "override_reason": override_reason,
                 "closed": closes_packet},
        mirror_activity=True, activity_kind="decision", activity_role=actor,
    )
    if commit:
        conn.commit()
    return {"id": decision_id, "packet_id": packet["id"], "decision": decision}


def record_recommendation(conn: sqlite3.Connection, wf: dict, step: dict, *,
                          actor: str, decision: str, feedback: str | None = None,
                          confidence: float | None = None,
                          commit: bool = True) -> dict:
    """Joint authority: AI records a recommendation, human still decides."""
    packet = latest_packet(conn, wf["id"], step["step_id"])
    if packet is None:
        packet = ensure_packet(conn, wf, step, created_by=actor, commit=commit)
    recommendation = {
        "decision": decision,
        "actor": actor,
        "feedback": feedback,
        "confidence": confidence,
        "at": now_iso(),
    }
    conn.execute(
        "UPDATE gate_packets SET recommendation_json=?, status='pending_human' WHERE id=?",
        (jdumps(recommendation), packet["id"]))
    result = record_decision(
        conn, wf, step, actor_type="role", actor=actor,
        decision=f"recommend_{decision}", feedback=feedback,
        reasons=["joint_authority"], packet_id=packet["id"],
        closes_packet=False, commit=False,
    )
    if commit:
        conn.commit()
    return {**result, "recommendation": recommendation}


def record_escalation(conn: sqlite3.Connection, wf: dict, step: dict, *,
                      actor: str, reason: str, triggers: list[str] | None = None,
                      proposed_decision: str | None = None,
                      proposed_feedback: str | None = None,
                      commit: bool = True) -> dict:
    packet = latest_packet(conn, wf["id"], step["step_id"])
    if packet is None:
        packet = ensure_packet(conn, wf, step, created_by=actor, commit=commit)
    recommendation = {
        "decision": proposed_decision or "escalate",
        "actor": actor,
        "feedback": proposed_feedback or reason,
        "triggers": triggers or [],
        "escalated": True,
        "at": now_iso(),
    }
    conn.execute(
        "UPDATE gate_packets SET recommendation_json=?, status='pending_human' WHERE id=?",
        (jdumps(recommendation), packet["id"]))
    result = record_decision(
        conn, wf, step, actor_type="human" if actor == "human" else "role",
        actor=actor, decision="escalate", feedback=reason,
        reasons=triggers or ["escalated"], packet_id=packet["id"],
        closes_packet=False, commit=False,
    )
    if commit:
        conn.commit()
    return {**result, "recommendation": recommendation}


def human_decision_items(conn: sqlite3.Connection) -> list[dict]:
    """Decision steps waiting for a human (not gates; gates have their own tab)."""
    rows = conn.execute(
        """SELECT p.*, s.title, s.role_slug, w.task_id, t.title AS task_title
             FROM gate_packets p
             JOIN workflow_steps s ON s.workflow_id=p.workflow_id AND s.step_id=p.step_id
             JOIN workflows w ON w.id=p.workflow_id
             LEFT JOIN tasks t ON t.id=w.task_id
            WHERE p.status='pending_human' AND w.status='running'
              AND COALESCE(s.kind,'task')='decision'
            ORDER BY p.created_at""").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["options"] = jloads(d.pop("options_json", "[]"), [])
        d["acceptance"] = jloads(d.pop("acceptance_json", "[]"), [])
        d["evidence"] = jloads(d.pop("evidence_json", "[]"), [])
        d["upstream"] = jloads(d.pop("upstream_json", "[]"), [])
        d["risks"] = jloads(d.pop("risks_json", "[]"), [])
        d["recommendation"] = jloads(d.pop("recommendation_json", "{}"), {})
        out.append(d)
    return out
