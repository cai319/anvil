"""Per-role RSI pilot gate (definition.rsi.enabled)."""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.services import roles, rsi
from anvil.util import now_iso


@pytest.fixture(autouse=True)
def _rsi_enabled(monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)


def test_only_opted_in_roles_observe_and_reflect(tmp_path):
    conn = db.connect(str(tmp_path / "gate.db"))
    try:
        roles.create(conn, slug="on-role", display_name="On",
                     definition={"runtime": {"id": "claude-code"},
                                 "rsi": {"enabled": True}})
        roles.create(conn, slug="off-role", display_name="Off",
                     definition={"runtime": {"id": "claude-code"}})
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at, updated_at) "
            "VALUES ('T-GATE','t','done','on-role',?,?)",
            (now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO task_assignees (task_id, role_slug, joined_at) "
            "VALUES ('T-GATE','on-role',?)", (now_iso(),))
        conn.execute(
            "INSERT INTO task_assignees (task_id, role_slug, joined_at) "
            "VALUES ('T-GATE','off-role',?)", (now_iso(),))
        conn.commit()

        observed = rsi.observe_task_done(
            conn, "T-GATE", completion_key="review:1")
        assert [x["role"] for x in observed] == ["on-role"]
        assert rsi.due(conn, "off-role")["reason"] == "role_disabled"
        assert rsi.start_reflection(conn, "off-role") is None
        assert rsi.record_retro(conn, "off-role", "T-GATE", "x") == 0
    finally:
        conn.close()


def test_toggle_role_gate_writes_config_revision(tmp_path):
    conn = db.connect(str(tmp_path / "toggle.db"))
    try:
        roles.create(conn, slug="off-role", display_name="Off",
                     definition={"runtime": {"id": "claude-code"}})
        assert rsi.role_enabled(conn, "off-role") is False
        result = roles.set_rsi_enabled(conn, "off-role", True)
        assert result["rsi"]["enabled"] is True
        assert rsi.role_enabled(conn, "off-role") is True
        rows = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug='off-role' "
            "AND kind='config'").fetchone()[0]
        assert rows >= 1
    finally:
        conn.close()


def test_global_switch_off_disables_even_opted_role(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", False)
    conn = db.connect(str(tmp_path / "global.db"))
    try:
        roles.create(conn, slug="on-role", display_name="On",
                     definition={"runtime": {"id": "claude-code"},
                                 "rsi": {"enabled": True}})
        assert rsi.role_enabled(conn, "on-role") is False
        assert rsi.due(conn, "on-role")["reason"] == "disabled"
    finally:
        conn.close()
