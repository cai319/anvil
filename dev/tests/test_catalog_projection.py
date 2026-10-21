"""技能/MCP 投影：未勾选时不应写入角色 workspace。"""
from __future__ import annotations

from anvil.db import connect
from anvil.graph import nodes


def test_empty_catalog_projects_nothing(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    workspace = tmp_path / "ws"
    effective = {
        "projection": {"tools": {"skills": [], "mcp": [], "allowlist": []}},
        "runtime": {"id": "claude-code"},
    }

    nodes._project_catalog(conn, effective, str(workspace), "test-role")

    assert not (workspace / ".claude").exists()
