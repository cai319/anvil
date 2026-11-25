"""Candidate archive (proposals kind=strategy) and CAS state machine (P3)."""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import rsi_operators

ACTIVE_STATUSES = ("draft", "queued", "evaluating", "pending", "applied")
TRANSITIONS = {
    "draft": {"pending", "queued", "evaluating", "rejected"},
    "queued": {"evaluating", "pending", "rejected"},
    "evaluating": {"pending", "rejected", "inconclusive", "superseded"},
    "pending": {"applied", "rejected", "rolled_back"},
    "applied": {"rolled_back"},
}


def _row(row) -> dict:
    d = dict(row)
    for key in ("proposed_json", "evidence_json", "manifest_json",
                "predictions_json", "diagnosis_json"):
        d[key.removesuffix("_json")] = jloads(d.pop(key, None), [] if key == "evidence_json" else {})
    d["edits"] = (d.get("proposed") or {}).get("edits", [])
    return d


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (candidate_id,)).fetchone()
    return _row(row) if row else None


def list_candidates(conn: sqlite3.Connection, *, role: str | None = None,
                    status: str | None = None) -> list[dict]:
    clauses = ["kind='strategy'"]
    params: list[object] = []
    if role:
        clauses.append("role_slug=?")
        params.append(role)
    if status:
        clauses.append("status=?")
        params.append(status)
    rows = conn.execute(
        "SELECT * FROM proposals WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at DESC", params).fetchall()
    return [_row(r) for r in rows]


def create_candidate(conn: sqlite3.Connection, role_slug: str, *,
                     cycle_id: str | None = None,
                     edits: list[dict],
                     tier: str = "human_approval",
                     base_version: int | None = None,
                     candidate_id: str | None = None,
                     dedupe_key: str | None = None,
                     evidence: list | None = None,
                     predictions: dict | None = None,
                     diagnosis: dict | None = None,
                     rationale: str | None = None,
                     risk: str | None = None,
                     status: str = "draft") -> dict:
    edits = rsi_operators.validate_edits(edits)
    if dedupe_key:
        found = conn.execute(
            "SELECT id FROM proposals WHERE role_slug=? AND dedupe_key=? "
            "AND status IN (" + ",".join("?" * len(ACTIVE_STATUSES)) + ")",
            (role_slug, dedupe_key, *ACTIVE_STATUSES)).fetchone()
        if found is not None:
            raise ValueError(f"duplicate active candidate: {found['id']}")
    pid = candidate_id or new_id()
    now = now_iso()
    manifest = {"schema_version": 1, "role_slug": role_slug, "edits": edits}
    conn.execute(
        "INSERT INTO proposals (id, role_slug, field, current_json, proposed_json, "
        "rationale, risk, status, created_at, decided_at, kind, tier, cycle_id, "
        "dedupe_key, base_version, applied_version, parent_id, evidence_json, "
        "manifest_json, predictions_json, diagnosis_json, eval_id, updated_at, "
        "decided_by, decision_note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, role_slug, "strategy", None, jdumps({"edits": edits}),
         rationale, risk, status, now, None, "strategy", tier, cycle_id,
         dedupe_key, base_version, None, None, jdumps(evidence or []),
         jdumps(manifest), jdumps(predictions or {}), jdumps(diagnosis or {}),
         None, now, None, None))
    conn.commit()
    return get_candidate(conn, pid)  # type: ignore[return-value]


def attach_eval(conn: sqlite3.Connection, candidate_id: str, run_id: str) -> None:
    conn.execute(
        "UPDATE proposals SET eval_id=?, updated_at=? WHERE id=?",
        (run_id, now_iso(), candidate_id))
    conn.commit()


def transition_candidate(conn: sqlite3.Connection, candidate_id: str,
                         status: str, *, decided_by: str | None = None,
                         decision_note: str | None = None) -> dict:
    current = get_candidate(conn, candidate_id)
    if current is None:
        raise LookupError(candidate_id)
    if status not in TRANSITIONS.get(current["status"], set()):
        raise ConflictError(
            f"candidate {candidate_id} cannot go {current['status']} -> {status}")
    now = now_iso()
    cur = conn.execute(
        "UPDATE proposals SET status=?, updated_at=?, decided_at=?, "
        "decided_by=?, decision_note=? WHERE id=? AND status=?",
        (status, now, now if status in ("applied", "rejected", "inconclusive",
                                        "rolled_back", "superseded") else None,
         decided_by, decision_note, candidate_id, current["status"]))
    if cur.rowcount == 0:
        raise ConflictError("candidate status changed; refresh")
    conn.commit()
    return get_candidate(conn, candidate_id)  # type: ignore[return-value]


def mark_applied(conn: sqlite3.Connection, candidate_id: str, version: int,
                 *, decided_by: str = "human") -> dict:
    now = now_iso()
    cur = conn.execute(
        "UPDATE proposals SET status='applied', applied_version=?, decided_at=?, "
        "decided_by=?, updated_at=? WHERE id=? AND status IN ('pending','draft')",
        (version, now, decided_by, now, candidate_id))
    if cur.rowcount == 0:
        raise ConflictError("candidate is not pending/draft")
    conn.commit()
    return get_candidate(conn, candidate_id)  # type: ignore[return-value]
