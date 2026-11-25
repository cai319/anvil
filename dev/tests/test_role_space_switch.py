"""P3 role-space switch/read-back tests (spec RSI-VER-006/007)."""
from __future__ import annotations

from anvil import config, db
from anvil.services import role_space, roles
from anvil.util import now_iso


def _seed(conn) -> None:
    roles.create(
        conn, slug="rsi-role", display_name="RSI 角色",
        definition={"runtime": {"id": "claude-code"},
                    "responsibilities": ["backend"]})
    home = config.AGENTS_DIR / "rsi-role"
    (home / "instruction.md").write_text(
        "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
        "<!-- ANVIL:STRATEGY:BEGIN -->\n策略 v1\n<!-- ANVIL:STRATEGY:END -->\n",
        encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("准则 v1\n", encoding="utf-8")
    card = home / "notes" / "memory" / "cards" / "rsi1.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("RSI 卡 v1\n", encoding="utf-8")
    human = home / "notes" / "memory" / "cards" / "human1.md"
    human.write_text("人工卡\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at, kind, "
        "scope, origin, last_modified_by) VALUES (?,?,?,?,?,?,?,?,?)",
        ("N-RSI1", "rsi-role", "notes/memory/cards/rsi1.md", "RSI 卡 v1",
         now_iso(), "personal_lesson", "role", "rsi", "rsi"))
    conn.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at, kind, "
        "scope, origin, last_modified_by) VALUES (?,?,?,?,?,?,?,?,?)",
        ("N-H1", "rsi-role", "notes/memory/cards/human1.md", "人工卡",
         now_iso(), "note", "role", "human", "human"))
    conn.commit()


def _snapshot(conn, *, source="baseline", status="active", parent=None):
    return role_space.snapshot(
        conn, "rsi-role", kind="evolution", source=source, status=status,
        parent_version=parent, note="test")


def test_switch_restores_files_and_archives_extra_rsi_cards(tmp_path):
    conn = db.connect(str(tmp_path / "switch.db"))
    try:
        _seed(conn)
        v1 = _snapshot(conn)
        home = config.AGENTS_DIR / "rsi-role"
        # v2 changes strategy and adds an RSI card; human card is added after v1.
        (home / "instruction.md").write_text(
            "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
            "<!-- ANVIL:STRATEGY:BEGIN -->\n策略 v2\n<!-- ANVIL:STRATEGY:END -->\n",
            encoding="utf-8")
        extra = home / "notes" / "memory" / "cards" / "rsi2.md"
        extra.write_text("RSI 卡 v2\n", encoding="utf-8")
        conn.execute(
            "INSERT INTO notes (id, role_slug, path, content, created_at, kind, "
            "scope, origin, last_modified_by) VALUES (?,?,?,?,?,?,?,?,?)",
            ("N-RSI2", "rsi-role", "notes/memory/cards/rsi2.md", "RSI 卡 v2",
             now_iso(), "personal_lesson", "role", "rsi", "rsi"))
        human2 = home / "notes" / "memory" / "cards" / "human2.md"
        human2.write_text("新人工卡\n", encoding="utf-8")
        conn.commit()
        v2 = _snapshot(conn, source="rsi_promote", status="candidate",
                       parent=v1["version"])

        result = role_space.switch(conn, "rsi-role", v1["version"])
        assert "策略 v1" in (home / "instruction.md").read_text(encoding="utf-8")
        assert (home / "notes/memory/cards/rsi1.md").read_text(
            encoding="utf-8") == "RSI 卡 v1\n"
        assert not (home / "notes/memory/cards/rsi2.md").exists()
        archive = (home / "notes/archive" / f"switch-v{v1['version']}"
                   / "notes/memory/cards/rsi2.md")
        assert archive.read_text(encoding="utf-8") == "RSI 卡 v2\n"
        assert (home / "notes/memory/cards/human1.md").exists()
        assert (home / "notes/memory/cards/human2.md").exists()
        assert result["archived_cards"] == 1
        active = conn.execute(
            "SELECT version FROM role_versions WHERE slug=? AND kind='evolution' "
            "AND status='active'", ("rsi-role",)).fetchone()
        assert active["version"] == v1["version"]
        assert v2["status"] == "candidate"
    finally:
        conn.close()


def test_switch_preserves_human_edited_rsi_card(tmp_path):
    conn = db.connect(str(tmp_path / "partial.db"))
    try:
        _seed(conn)
        v1 = _snapshot(conn)
        home = config.AGENTS_DIR / "rsi-role"
        edited = home / "notes" / "memory" / "cards" / "rsi1.md"
        edited.write_text("人工改过的 RSI 卡\n", encoding="utf-8")
        conn.execute(
            "UPDATE notes SET last_modified_by='human' WHERE id='N-RSI1'")
        conn.commit()
        result = role_space.switch(conn, "rsi-role", v1["version"])
        assert edited.read_text(encoding="utf-8") == "人工改过的 RSI 卡\n"
        assert result["partial_restore"] is True
        assert result["preserved_cards"] == 1
    finally:
        conn.close()


def test_actual_performance_attributes_runs_to_version(tmp_path):
    conn = db.connect(str(tmp_path / "perf.db"))
    try:
        _seed(conn)
        v1 = _snapshot(conn)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", ("T-P", "任务", "done", now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, origin, "
            "role_space_version, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
            ("R-P", "rsi-role", "T-P", "completed", "task", v1["version"],
             now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO token_usage (role_slug, task_id, run_id, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("rsi-role", "T-P", "R-P", 100, 50, 0, 0, now_iso()))
        conn.commit()
        perf = role_space.actual_performance(conn, "rsi-role", v1["version"])
        assert perf["runs"] == 1
        assert perf["tasks"] == 1
        assert perf["success_rate"] == 1.0
        assert perf["tokens"] == 150
    finally:
        conn.close()
