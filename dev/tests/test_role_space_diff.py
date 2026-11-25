"""Read-only evolution version apply diff (RSI-VER-016)."""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config, db
from anvil.services import role_space, roles
from anvil.util import now_iso


def _note(conn, role: str, path: str, content: str, *,
          origin: str, modifier: str) -> None:
    conn.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at, kind, "
        "scope, origin, last_modified_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"N-{path}", role, path, content, now_iso(), "personal_lesson",
         "role", origin, modifier))


def _seed(conn):
    roles.create(conn, slug="qa", display_name="QA",
                 definition={"runtime": {"id": "claude-code"}})
    home = config.AGENTS_DIR / "qa"
    (home / "instruction.md").write_text("v1", encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("p1", encoding="utf-8")
    (home / "notes/memory/cards").mkdir(parents=True, exist_ok=True)
    (home / "notes/memory/cards/target.md").write_text("target", encoding="utf-8")
    return home


def test_preview_switch_reports_files_cards_and_working_set(tmp_path):
    conn = db.connect(str(tmp_path / "diff.db"))
    try:
        home = _seed(conn)
        baseline = role_space.snapshot(
            conn, "qa", kind="evolution", source="baseline", status="active")
        candidate = role_space.snapshot(
            conn, "qa", kind="evolution", source="rsi_candidate",
            status="candidate", force=True,
            files=["instruction.md", "PRINCIPLES.md",
                   "notes/memory/cards/target.md", "notes/playbooks/new.md"],
            file_overrides={
                "instruction.md": b"v2",
                "notes/playbooks/new.md": b"new playbook",
            })
        (home / "notes/memory/cards/rsi-extra.md").write_text(
            "rsi extra", encoding="utf-8")
        _note(conn, "qa", "notes/memory/cards/rsi-extra.md", "rsi extra",
              origin="rsi", modifier="rsi")
        (home / "notes/memory/cards/human-extra.md").write_text(
            "human extra", encoding="utf-8")
        _note(conn, "qa", "notes/memory/cards/human-extra.md", "human extra",
              origin="human", modifier="human")
        conn.commit()

        diff = role_space.preview_switch(conn, "qa", candidate["version"])
        assert diff["active_version"] == baseline["version"]
        assert diff["target_version"] == candidate["version"]
        files = {f["path"]: f for f in diff["files"]}
        assert files["instruction.md"]["status"] == "modified"
        assert files["PRINCIPLES.md"]["status"] == "unchanged"
        assert files["notes/playbooks/new.md"]["status"] == "added"
        assert diff["knowledge"]["restore"] == 1
        assert "notes/memory/cards/rsi-extra.md" in diff["knowledge"]["archive"]
        assert "notes/memory/cards/human-extra.md" in diff["knowledge"]["preserve_human"]
        assert diff["working_set"]["count"] == 1
        assert diff["charter_changed"] is False
        # Read-only: no role files changed.
        assert (home / "instruction.md").read_text(encoding="utf-8") == "v1"

        replay = role_space.preview_switch(conn, "qa", baseline["version"])
        assert replay["active_version"] == baseline["version"]
        assert {f["status"] for f in replay["files"]} == {"unchanged"}
        assert "notes/memory/cards/rsi-extra.md" in replay["knowledge"]["archive"]
    finally:
        conn.close()


def test_preview_switch_rejects_corrupt_manifest(tmp_path):
    conn = db.connect(str(tmp_path / "corrupt.db"))
    try:
        _seed(conn)
        version = role_space.snapshot(
            conn, "qa", kind="evolution", source="baseline", status="active")
        row = conn.execute(
            "SELECT space_manifest_path FROM role_versions WHERE slug='qa' "
            "AND version=?", (version["version"],)).fetchone()
        Path(row["space_manifest_path"]).write_text('{"tampered": true}',
                                                    encoding="utf-8")
        with pytest.raises(ValueError):
            role_space.preview_switch(conn, "qa", version["version"])
    finally:
        conn.close()
