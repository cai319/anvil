"""运行时 MCP 目录介绍：固定读取 runtime catalog 配置，不探测外部进程。"""
from __future__ import annotations

import os
import tempfile

from anvil.db import connect
from anvil.services import runtime, tooling


def test_mcp_catalog_entry_uses_runtime_description():
    entry = runtime._mcp_catalog_entry(
        "codegraph",
        {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]},
        "代码智能与知识图谱：索引代码库，查询符号、调用链和影响面。",
    )
    assert entry["description"] == "代码智能与知识图谱：索引代码库，查询符号、调用链和影响面。"
    assert entry["transport"] == "stdio"
    assert entry["hint"] == "codegraph serve"


def test_mcp_catalog_entry_accepts_server_description():
    entry = runtime._mcp_catalog_entry(
        "custom",
        {
            "type": "stdio", "command": "custom-mcp",
            "description": "来自 MCP server 条目的介绍",
        },
    )
    assert entry["description"] == "来自 MCP server 条目的介绍"


def test_catalog_merges_imported_registry_servers(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "import.db"))
    try:
        tooling.create_mcp_server(
            conn, name="demo", description="导入的 MCP", transport="stdio",
            command="node", args=["server.js"], runtime="fake")
        tooling.create_mcp_server(
            conn, name="other-runtime", description="不应出现", transport="stdio",
            command="node", args=["other.js"], runtime="other")
        monkeypatch.setattr(runtime, "load_definitions", lambda: {
            "fake": {"catalog": {"skills_dir": str(tmp_path / "skills")}},
        })
        catalog = runtime.catalog(conn, "fake")
        assert catalog["mcp_servers"] == [{
            "name": "demo", "description": "导入的 MCP",
            "transport": "stdio", "hint": "node server.js",
        }]
    finally:
        conn.close()


def test_mcp_config_for_marks_claude_code_http_servers(tmp_path):
    conn = connect(str(tmp_path / "mcp.db"))
    try:
        tooling.create_mcp_server(
            conn, name="remote", transport="http",
            url="http://127.0.0.1:9000/mcp", runtime="claude-code")
        tooling.create_mcp_server(
            conn, name="remote-codex", transport="http",
            url="http://127.0.0.1:9001/mcp", runtime="codex-cli")
        claude = tooling.mcp_config_for(conn, ["remote"], "claude-code")
        assert claude["remote"] == {
            "url": "http://127.0.0.1:9000/mcp", "type": "http"}
        other = tooling.mcp_config_for(conn, ["remote-codex"], "codex-cli")
        assert other["remote-codex"] == {"url": "http://127.0.0.1:9001/mcp"}
    finally:
        conn.close()


def test_mcp_config_for_keeps_stdio_shape(tmp_path):
    conn = connect(str(tmp_path / "mcp-stdio.db"))
    try:
        tooling.create_mcp_server(
            conn, name="local", transport="stdio", command="node",
            args=["server.js"], runtime="claude-code")
        cfg = tooling.mcp_config_for(conn, ["local"], "claude-code")
        assert cfg["local"] == {"command": "node", "args": ["server.js"]}
    finally:
        conn.close()


def test_mcp_catalog_entry_without_description_keeps_only_hint():
    entry = runtime._mcp_catalog_entry(
        "codegraph", {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]})
    assert entry["description"] == ""
    assert entry["hint"] == "codegraph serve"
    entry = runtime._mcp_catalog_entry(
        "macro-registry", {
            "type": "stdio",
            "command": os.path.join(tempfile.gettempdir(), ".venv", "bin", "python"),
            "args": [os.path.join(tempfile.gettempdir(), "macro_cli.py"), "serve"],
        })
    assert entry["description"] == ""
    assert entry["hint"] == "python macro_cli.py"
