"""Working-set model: daily knowledge as deltas over the active baseline."""
from __future__ import annotations

from anvil import config, db
from anvil.services import role_space, roles
from anvil.util import now_iso


def _seed(conn):
    roles.create(conn, slug="rsi-role", display_name="RSI",
                 definition={"runtime": {"id": "claude-code"}})
    home = config.AGENTS_DIR / "rsi-role"
    (home / "instruction.md").write_text("v1", encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("p1", encoding="utf-8")
    return home, role_space.snapshot(
        conn, "rsi-role", kind="evolution", source="baseline", status="active")


def _add_rsi_card(conn, home, name="delta1.md", content="每日收割"):
    path = home / "notes" / "memory" / "cards" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    conn.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at, kind, scope, "
        "origin, last_modified_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"N-{name}", "rsi-role", f"notes/memory/cards/{name}", content,
         now_iso(), "personal_lesson", "role", "rsi", "rsi"))
    conn.commit()


def test_daily_card_is_working_set_not_version(tmp_path):
    conn = db.connect(str(tmp_path / "ws.db"))
    try:
        home, version = _seed(conn)
        before = role_space.working_set(conn, "rsi-role")
        assert before["base_version"] == version["version"]
        assert before["count"] == 0
        version_row_before = conn.execute(
            "SELECT content_hash FROM role_versions WHERE slug='rsi-role' "
            "AND version=?", (version["version"],)).fetchone()["content_hash"]
        _add_rsi_card(conn, home)
        after = role_space.working_set(conn, "rsi-role")
        assert after["count"] == 1
        assert after["hash"] != before["hash"]
        manifest = role_space.get_manifest(conn, "rsi-role", version["version"])
        assert not any(c["path"].endswith("delta1.md")
                       for c in manifest["knowledge_cards"])
        version_row_after = conn.execute(
            "SELECT content_hash FROM role_versions WHERE slug='rsi-role' "
            "AND version=?", (version["version"],)).fetchone()["content_hash"]
        assert version_row_before == version_row_after
    finally:
        conn.close()


def test_run_records_working_set_hash(tmp_path):
    conn = db.connect(str(tmp_path / "run.db"))
    try:
        home, _ = _seed(conn)
        _add_rsi_card(conn, home)
        from anvil.services import runs
        run = runs.create_run(conn, "rsi-role", None, prompt="hello")
        expected = role_space.working_set(conn, "rsi-role")["hash"]
        assert run["working_set_hash"] == expected
    finally:
        conn.close()


def test_switch_archives_working_set_and_resets_hash(tmp_path):
    conn = db.connect(str(tmp_path / "switch.db"))
    try:
        home, version = _seed(conn)
        _add_rsi_card(conn, home)
        assert role_space.working_set(conn, "rsi-role")["count"] == 1
        role_space.switch(conn, "rsi-role", version["version"])
        assert role_space.working_set(conn, "rsi-role")["count"] == 0
        archive = (home / "notes" / "archive" / f"switch-v{version['version']}"
                   / "notes/memory/cards/delta1.md")
        assert archive.is_file()
    finally:
        conn.close()
