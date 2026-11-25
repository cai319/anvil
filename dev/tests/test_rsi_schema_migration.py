"""P0 RSI schema/migration tests (spec RSI-VER-002/003/013).

These tests intentionally fail before the v1.50 migration lands:
- role_versions/proposals extensions
- eval_runs / runtime_proposals tables
- runs.role_space_version
- notes provenance columns
- legacy row backfill + idempotency
"""
from __future__ import annotations

import sqlite3

from anvil import db
from anvil.util import jdumps, now_iso


EXPECTED_ROLE_VERSION_COLUMNS = {
    "kind", "status", "source", "content_hash", "space_manifest_path",
    "space_manifest_hash", "charter_digest", "eval_run_id", "parent_version",
    "origin_candidate_id", "promoted_at", "promoted_by", "note",
    "updated_at", "coalesced_count", "deleted_at", "purged_at",
    "integrity_status", "last_verified_at",
}

EXPECTED_PROPOSAL_COLUMNS = {
    "kind", "tier", "cycle_id", "dedupe_key", "base_version", "applied_version",
    "parent_id", "evidence_json", "manifest_json", "predictions_json",
    "diagnosis_json", "eval_id", "updated_at", "decided_by", "decision_note",
}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v150_tables_and_columns_exist(tmp_path):
    conn = db.connect(str(tmp_path / "schema.db"))
    try:
        assert EXPECTED_ROLE_VERSION_COLUMNS <= _cols(conn, "role_versions")
        assert EXPECTED_PROPOSAL_COLUMNS <= _cols(conn, "proposals")
        assert "role_space_version" in _cols(conn, "runs")
        notes = _cols(conn, "notes")
        assert {"origin", "source_cycle_id", "source_role_version",
                "last_modified_by"} <= notes
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"eval_runs", "runtime_proposals"} <= tables
    finally:
        conn.close()


def test_legacy_role_versions_are_backfilled_as_config(tmp_path):
    conn = db.connect(str(tmp_path / "legacy.db"))
    try:
        # Simulate a legacy row that predates kind/status/content_hash.
        conn.execute(
            "INSERT INTO role_versions (slug, version, display_name, definition_json, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("legacy-role", 1, "Legacy", jdumps({"runtime": {"id": "claude-code"}}),
             "update", now_iso()),
        )
        conn.commit()
        db._migrate_rsi_v150(conn)
        row = conn.execute(
            "SELECT kind, status, content_hash, updated_at FROM role_versions "
            "WHERE slug='legacy-role' AND version=1").fetchone()
        assert row["kind"] == "config"
        assert row["status"] == "archived"
        assert row["content_hash"]
        assert row["updated_at"]
        # Idempotent: a second run must not fail or duplicate.
        db._migrate_rsi_v150(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug='legacy-role'").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_runtime_proposals_and_eval_runs_have_single_active_indexes(tmp_path):
    conn = db.connect(str(tmp_path / "indexes.db"))
    try:
        # Partial unique index on runtime proposal signature.
        cols = (
            "id, dedupe_sig, component_type, component_key, title, status, "
            "summary, proposed_change_md, created_at, updated_at"
        )
        vals = (
            "RTP-1", "sig-1", "skill", "skill:x", "x", "open",
            "s", "p", now_iso(), now_iso(),
        )
        marks = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO runtime_proposals ({cols}) VALUES ({marks})", vals)
        try:
            conn.execute(f"INSERT INTO runtime_proposals ({cols}) VALUES ({marks})",
                         ("RTP-2", "sig-1", "skill", "skill:x", "x", "open",
                          "s", "p", now_iso(), now_iso()))
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "active runtime proposal dedupe index must reject duplicates"
        # eval_runs exists and accepts a minimal row.
        conn.execute(
            "INSERT INTO eval_runs (id, role_slug, suite_id, suite_digest, created_at) "
            "VALUES (?,?,?,?,?)",
            ("E-1", "qa-role", "suite", "digest", now_iso()))
        conn.commit()
    finally:
        conn.close()
