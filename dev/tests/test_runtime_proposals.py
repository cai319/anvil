"""P4 runtime proposal (RIP) tests (spec RSI-RIP-001..007)."""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.services import runtime_proposals


def _create(conn, **kw):
    base = {
        "component_type": "skill",
        "component_key": "skill:code-audit",
        "title": "code-audit 缺 Python 环境时失败",
        "summary": "任务中 skill 加载失败",
        "proposed_change_md": "在 SKILL.md 增加前置检查",
        "severity": "major",
        "origin": "agent",
        "origin_role": "qa-role",
        "evidence": {"task_id": "T-1", "run_id": "R-1",
                     "message": "python: command not found"},
    }
    base.update(kw)
    return runtime_proposals.create_or_update(conn, **base)


def test_create_and_dedupe_by_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_PROPOSALS_DIR", tmp_path / "rip")
    conn = db.connect(str(tmp_path / "rip.db"))
    try:
        first = _create(conn)
        assert first["created"] is True
        assert first["proposal"]["status"] == "draft"
        assert first["proposal"]["evidence_count"] == 1
        second = _create(conn, evidence={"task_id": "T-2", "run_id": "R-2",
                                        "message": "python: command not found"})
        assert second["created"] is False
        assert second["proposal"]["id"] == first["proposal"]["id"]
        assert second["proposal"]["evidence_count"] == 2
        assert len(runtime_proposals.list_proposals(conn)) == 1
    finally:
        conn.close()


def test_human_lifecycle_and_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_PROPOSALS_DIR", tmp_path / "rip")
    conn = db.connect(str(tmp_path / "rip.db"))
    try:
        created = _create(conn)["proposal"]
        rid = created["id"]
        for status in ("open", "triaged", "accepted", "implemented", "verified"):
            runtime_proposals.transition(conn, rid, status, actor="human")
        proposal = runtime_proposals.get_proposal(conn, rid)
        assert proposal["status"] == "verified"
        md = runtime_proposals.markdown_path(rid)
        assert md.is_file()
        text = md.read_text(encoding="utf-8")
        assert "## Evidence" in text
        assert "## Proposed change" in text
        assert "## Verification plan" in text
        with pytest.raises(Exception):
            runtime_proposals.transition(conn, rid, "open", actor="human")
    finally:
        conn.close()


def test_export_is_human_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_PROPOSALS_DIR", tmp_path / "rip")
    conn = db.connect(str(tmp_path / "rip.db"))
    try:
        rid = _create(conn)["proposal"]["id"]
        target = tmp_path / "docs"
        path = runtime_proposals.export(conn, rid, target_dir=target)
        assert path.is_file()
        assert path.parent == target
    finally:
        conn.close()
