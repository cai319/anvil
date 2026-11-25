"""Inbox aggregates RSI candidates and runtime proposals."""
from __future__ import annotations

from anvil import config, db
from anvil.services import inbox, proposals, roles, runtime_proposals
from anvil.util import now_iso


def test_inbox_includes_rsi_candidate_and_runtime_proposal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_PROPOSALS_DIR", tmp_path / "rip")
    conn = db.connect(str(tmp_path / "inbox.db"))
    try:
        roles.create(conn, slug="qa-role", display_name="QA",
                     definition={"runtime": {"id": "claude-code"}})
        proposals.create_candidate(
            conn, "qa-role", cycle_id="C-1",
            edits=[{
                "operator": "append_knowledge_card", "tier": "autonomous",
                "target": "memory_card", "path": "notes/memory/cards/x.md",
                "content_md": "x", "portrait_refs": ["responsibilities[0]"],
                "diagnosis_ref": "quality",
                "predicted_effect": {"dimension": "quality"},
            }])
        runtime_proposals.create_or_update(
            conn, component_type="skill", component_key="skill:x",
            title="x", summary="y", proposed_change_md="z",
            origin="agent", evidence={"message": "boom"})
        body = inbox.collect(conn)
        kinds = {item["kind"] for item in body["items"]}
        assert "rsi_candidate" in kinds
        assert "runtime_proposal" in kinds
    finally:
        conn.close()
