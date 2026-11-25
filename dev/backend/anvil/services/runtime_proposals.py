"""Runtime improvement proposals (RIP/RTP) lifecycle and markdown (v1.50 P4)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import ledger

ACTIVE_STATUSES = ("draft", "open", "triaged", "accepted", "implemented")
COMPONENT_TYPES = {
    "skill", "mcp_server", "runtime_yaml", "feature_flag", "tool_preset",
    "model", "environment", "harness_part", "docs_template", "other",
}
FORBIDDEN_COMPONENTS = {"eval", "security", "anvil-source", "role-config"}
TRANSITIONS = {
    "draft": {"open", "triaged", "rejected", "duplicate", "wontfix", "stale"},
    "open": {"triaged", "accepted", "rejected", "duplicate", "wontfix", "stale"},
    "triaged": {"accepted", "rejected", "duplicate", "wontfix", "stale"},
    "accepted": {"implemented", "rejected", "wontfix"},
    "implemented": {"verified", "rejected"},
    "verified": {"closed", "rejected"},
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "proposal"


def _normalize(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text)
    text = re.sub(r"(/[\w.-]+)+", "<path>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return re.sub(r"\s+", " ", text).strip()


def signature(*, component_type: str, component_key: str, runtime_id: str | None,
              runtime_version: str | None, message: str,
              phase: str | None = None) -> str:
    payload = "|".join([
        component_type, component_key, runtime_id or "",
        runtime_version or "", _normalize(message), phase or "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _md_path(proposal_id: str) -> Path:
    return config.RUNTIME_PROPOSALS_DIR / proposal_id / "proposal.md"


def markdown_path(proposal_id: str) -> Path:
    return _md_path(proposal_id)


def _row(row) -> dict:
    d = dict(row)
    d["evidence"] = jloads(d.pop("evidence_json", None), {})
    return d


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runtime_proposals WHERE id=?", (proposal_id,)).fetchone()
    return _row(row) if row else None


def list_proposals(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM runtime_proposals WHERE status=? "
            "ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_proposals ORDER BY created_at DESC").fetchall()
    return [_row(r) for r in rows]


def _render(proposal: dict) -> str:
    evidence = proposal.get("evidence") or {}
    evidence_text = "- " + json.dumps(evidence, ensure_ascii=False, indent=2)
    return f"""---
id: {proposal['id']}
status: {proposal['status']}
component_type: {proposal['component_type']}
component_key: {proposal['component_key']}
runtime: {proposal.get('runtime_id') or '-'}
severity: {proposal['severity']}
signature: {proposal['dedupe_sig']}
occurrences: {proposal['evidence_count']}
origin: {proposal['origin']}
---

# {proposal['id']}: {proposal['title']}

## Summary
{proposal['summary']}

## Impact
{proposal.get('risk_md') or '待补充'}

## Evidence
{evidence_text}

## Root-cause hypothesis
{proposal.get('root_cause_hypothesis') or '待验证'}

## Proposed change
{proposal['proposed_change_md']}

## Verification plan
{proposal.get('verification_md') or '待补充可执行 probe'}

## Risks and alternatives
{proposal.get('risk_md') or '待补充'}

## Decision log
- {now_iso()} status={proposal['status']}
{('- ' + proposal['decision_note']) if proposal.get('decision_note') else ''}
"""


def _write_md(proposal: dict) -> Path:
    path = _md_path(proposal["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(proposal), encoding="utf-8")
    conn_note = path
    _ = conn_note
    return path


def _emit_evidence(conn: sqlite3.Connection, proposal_id: str,
                   evidence: dict) -> None:
    ledger.emit(
        conn, "runtime_proposal.evidence", actor_type="system", actor="rsi",
        payload={"proposal_id": proposal_id, "evidence": evidence},
        correlation_id=proposal_id)


def create_or_update(conn: sqlite3.Connection, *, component_type: str,
                     component_key: str, title: str, summary: str,
                     proposed_change_md: str, severity: str = "medium",
                     origin: str = "agent", origin_role: str | None = None,
                     runtime_id: str | None = None,
                     runtime_version: str | None = None,
                     evidence: dict | None = None,
                     root_cause_hypothesis: str | None = None,
                     verification_md: str | None = None,
                     risk_md: str | None = None,
                     phase: str | None = None) -> dict:
    if component_type in FORBIDDEN_COMPONENTS:
        raise ValueError(f"forbidden component: {component_type}")
    if component_type not in COMPONENT_TYPES:
        raise ValueError(f"unknown component_type: {component_type}")
    if not title.strip() or not summary.strip() or not proposed_change_md.strip():
        raise ValueError("title/summary/proposed_change_md are required")
    message = ""
    if evidence:
        message = str(evidence.get("message") or evidence.get("error") or "")
    sig = signature(
        component_type=component_type, component_key=component_key,
        runtime_id=runtime_id, runtime_version=runtime_version,
        message=message or title, phase=phase)
    existing = conn.execute(
        "SELECT * FROM runtime_proposals WHERE dedupe_sig=? AND status IN ("
        + ",".join("?" * len(ACTIVE_STATUSES)) + ") ORDER BY created_at DESC LIMIT 1",
        (sig, *ACTIVE_STATUSES)).fetchone()
    now = now_iso()
    if existing is not None:
        pid = existing["id"]
        conn.execute(
            "UPDATE runtime_proposals SET evidence_count=evidence_count+1, "
            "updated_at=? WHERE id=?", (now, pid))
        _emit_evidence(conn, pid, evidence or {})
        conn.commit()
        proposal = get_proposal(conn, pid)
        proposal["evidence"] = evidence or proposal.get("evidence") or {}
        _write_md(proposal)
        return {"created": False, "proposal": proposal}
    pid = "RTP-" + new_id().replace("-", "")[:12].upper()
    conn.execute(
        "INSERT INTO runtime_proposals (id, dedupe_sig, component_type, "
        "component_key, runtime_id, title, severity, status, origin, origin_role, "
        "summary, root_cause_hypothesis, proposed_change_md, verification_md, "
        "risk_md, evidence_count, markdown_path, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, sig, component_type, component_key, runtime_id, title, severity,
         "draft", origin, origin_role, summary, root_cause_hypothesis,
         proposed_change_md, verification_md, risk_md, 1, str(_md_path(pid)),
         now, now))
    _emit_evidence(conn, pid, evidence or {})
    conn.commit()
    proposal = get_proposal(conn, pid)
    proposal["evidence"] = evidence or {}
    _write_md(proposal)
    return {"created": True, "proposal": proposal}


def add_evidence(conn: sqlite3.Connection, proposal_id: str,
                 evidence: dict) -> dict:
    if get_proposal(conn, proposal_id) is None:
        raise LookupError(proposal_id)
    conn.execute(
        "UPDATE runtime_proposals SET evidence_count=evidence_count+1, updated_at=? "
        "WHERE id=?", (now_iso(), proposal_id))
    _emit_evidence(conn, proposal_id, evidence)
    conn.commit()
    proposal = get_proposal(conn, proposal_id)
    proposal["evidence"] = evidence
    _write_md(proposal)
    return proposal


def transition(conn: sqlite3.Connection, proposal_id: str, status: str, *,
               actor: str = "human", note: str | None = None) -> dict:
    current = get_proposal(conn, proposal_id)
    if current is None:
        raise LookupError(proposal_id)
    if status not in TRANSITIONS.get(current["status"], set()):
        raise ConflictError(
            f"proposal {proposal_id} cannot go {current['status']} -> {status}")
    now = now_iso()
    cur = conn.execute(
        "UPDATE runtime_proposals SET status=?, updated_at=?, decided_by=?, "
        "decided_at=?, decision_note=COALESCE(?, decision_note) "
        "WHERE id=? AND status=?",
        (status, now, actor, now, note, proposal_id, current["status"]))
    if cur.rowcount == 0:
        raise ConflictError("proposal status changed; refresh")
    conn.commit()
    proposal = get_proposal(conn, proposal_id)
    _write_md(proposal)
    return proposal


def export(conn: sqlite3.Connection, proposal_id: str, *,
           target_dir: Path | None = None) -> Path:
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise LookupError(proposal_id)
    source = _write_md(proposal)
    target_dir = Path(target_dir) if target_dir else (
        Path.cwd() / "docs" / "runtime-proposals")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{proposal_id}-{_slug(proposal['title'])}.md"
    target = target_dir / name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    conn.execute(
        "UPDATE runtime_proposals SET export_path=?, updated_at=? WHERE id=?",
        (str(target), now_iso(), proposal_id))
    conn.commit()
    return target
