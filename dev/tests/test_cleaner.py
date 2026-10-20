"""P3 Cleaner：MEMORY.md 超限 → 每天最多一次自修剪提醒（零 token 检查）。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import governance
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


def test_cleaner_creates_one_reminder_per_day(tmp_path, monkeypatch):
    agents = tmp_path / "agents" / "dev"
    agents.mkdir(parents=True)
    (agents / "MEMORY.md").write_text("x" * 20000, encoding="utf-8")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "CLEANER_ENABLED", True)
    monkeypatch.setattr(config, "CLEANER_MAX_BYTES", 12000)
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    conn.execute("INSERT INTO roles (slug, display_name, home_path, definition_json, created_at, updated_at) "
                 "VALUES ('dev','Dev',?, '{}', ?, ?)", (str(agents), now, now))
    conn.commit()

    governance.tick(conn, dispatch=False)
    rows = conn.execute("SELECT prompt, status FROM reminders WHERE role_slug='dev'").fetchall()
    assert len(rows) == 1
    assert "MEMORY.md" in rows[0]["prompt"]

    # 同一天再次 tick 不重复
    governance.tick(conn, dispatch=False)
    assert conn.execute("SELECT COUNT(*) AS n FROM reminders WHERE role_slug='dev'").fetchone()["n"] == 1
    conn.close()
