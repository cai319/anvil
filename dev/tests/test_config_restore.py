"""Config revision restore: definition + strategy files + tools snapshot."""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.services import role_space, roles
from anvil.util import jdumps, now_iso


@pytest.fixture(autouse=True)
def _rsi_enabled(monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)


def _seed(conn):
    roles.create(conn, slug="qa", display_name="QA",
                 definition={"runtime": {"id": "claude-code"}})
    home = config.AGENTS_DIR / "qa"
    (home / "instruction.md").write_text("v1", encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("p1", encoding="utf-8")
    now = now_iso()
    conn.execute(
        "INSERT INTO role_tools (role_slug, preset, max_tool_tokens, "
        "allowlist_json, skills_json, mcp_json, created_at, updated_at) "
        "VALUES ('qa','preset-v1',100,'[]','[]','[]',?,?) "
        "ON CONFLICT(role_slug) DO UPDATE SET preset='preset-v1', "
        "max_tool_tokens=100, allowlist_json='[]', skills_json='[]', "
        "mcp_json='[]', updated_at=excluded.updated_at", (now, now))
    conn.commit()
    return home


def _latest_config_version(conn) -> int:
    return int(conn.execute(
        "SELECT version FROM role_versions WHERE slug='qa' AND kind='config' "
        "ORDER BY version DESC LIMIT 1").fetchone()["version"])


def test_restore_config_restores_definition_files_and_tools(tmp_path):
    conn = db.connect(str(tmp_path / "restore.db"))
    try:
        home = _seed(conn)
        roles.update_profile(conn, "qa", "prompt-v1", ["a"])
        target = _latest_config_version(conn)
        roles.update_profile(conn, "qa", "prompt-v2", ["b"])
        home.joinpath("instruction.md").write_text("v2", encoding="utf-8")
        home.joinpath("PRINCIPLES.md").write_text("p2", encoding="utf-8")
        roles.update_tools(conn, "qa", "preset-v2", [], 200)
        result = role_space.restore_config(conn, "qa", target)
        assert result["tools_restored"] is True
        assert "instruction.md" in result["restored_files"]
        assert home.joinpath("instruction.md").read_text(encoding="utf-8") == "v1"
        assert home.joinpath("PRINCIPLES.md").read_text(encoding="utf-8") == "p1"
        profile = roles.get_profile(conn, "qa")
        assert profile["system_prompt"] == "prompt-v1"
        assert profile["responsibilities"] == ["a"]
        tools = roles.get_tools(conn, "qa")
        assert tools["preset"] == "preset-v1"
        audit = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='role.config_restored'"
        ).fetchone()[0]
        assert audit == 1
    finally:
        conn.close()


def test_restore_legacy_config_warns_and_keeps_tools(tmp_path):
    conn = db.connect(str(tmp_path / "legacy.db"))
    try:
        home = _seed(conn)
        before_tools = roles.get_tools(conn, "qa")
        version = _latest_config_version(conn) + 1
        definition = jdumps({"runtime": {"id": "claude-code"},
                             "system_prompt": "legacy"})
        conn.execute(
            "INSERT INTO role_versions (slug, version, display_name, "
            "definition_json, source, created_at, kind, status) "
            "VALUES ('qa',?,'QA',?,?,?, 'config','archived')",
            (version, definition, "legacy", now_iso()))
        conn.commit()
        dry = role_space.restore_config(conn, "qa", version, dry_run=True)
        assert dry["restored_files"] == []
        assert any("role_tools" in w for w in dry["warnings"])
        role_space.restore_config(conn, "qa", version)
        assert roles.get_profile(conn, "qa")["system_prompt"] == "legacy"
        assert roles.get_tools(conn, "qa") == before_tools
        assert home.joinpath("instruction.md").read_text(encoding="utf-8") == "v1"
    finally:
        conn.close()
