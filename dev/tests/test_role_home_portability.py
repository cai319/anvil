"""跨平台/迁移：角色 home_path 按当前 AGENTS_DIR 重算。"""
from __future__ import annotations

from anvil import config
from anvil.db import connect
from anvil.services import roles as roles_service


def test_repair_home_paths_recomputes_from_agents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn = connect(str(tmp_path / "test.db"))
    roles_service.create(conn, slug="cindy", display_name="Cindy",
                         definition={"runtime": {"id": "claude-code"}})
    expected = str(tmp_path / "agents" / "cindy")
    assert roles_service.get(conn, "cindy")["home_path"] == expected

    # 模拟从 macOS 迁移过来的旧绝对路径
    conn.execute(
        "UPDATE roles SET home_path=? WHERE slug=?",
        ("/Users/someone/aliceAgent/data/agents/cindy", "cindy"),
    )
    conn.commit()
    # 读取时即按当前 AGENTS_DIR 归一化
    assert roles_service.get(conn, "cindy")["home_path"] == expected

    repaired = roles_service.repair_home_paths(conn)
    assert repaired == 1
    row = conn.execute("SELECT home_path FROM roles WHERE slug='cindy'").fetchone()
    assert row["home_path"] == expected
    assert (tmp_path / "agents" / "cindy").is_dir()
