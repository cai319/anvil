"""工具目录/压缩包导入 API：skill 目录与 MCP bundle。"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.services import runtime as runtime_service
from tests.conftest import API, human_headers


@pytest.mark.asyncio
async def test_import_skill_directory(client, tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(runtime_service, "load_definitions", lambda: {
        "fake-runtime": {"catalog": {"skills_dir": str(skills_dir)}},
    })
    files = [
        ("files", ("my-skill/SKILL.md", b"description: Test skill\n# body\n", "text/markdown")),
        ("files", ("my-skill/scripts/run.sh", b"echo hi\n", "text/plain")),
    ]
    data = {
        "runtime": "fake-runtime",
        "paths": json.dumps(["my-skill/SKILL.md", "my-skill/scripts/run.sh"]),
        "overwrite": "false",
    }
    r = await client.post(f"{API}/tooling/import/skills", data=data, files=files,
                          headers=human_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    assert [x["name"] for x in body["imported"]] == ["my-skill"]
    assert (skills_dir / "my-skill" / "SKILL.md").is_file()
    assert (skills_dir / "my-skill" / "scripts" / "run.sh").is_file()

    # 同名再次导入默认跳过。
    r = await client.post(f"{API}/tooling/import/skills", data=data, files=files,
                          headers=human_headers())
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_import_mcp_bundle_rewrites_relative_paths(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(runtime_service, "load_definitions", lambda: {
        "fake-runtime": {"catalog": {}},
    })
    files = [
        ("files", ("bundle/mcp.json", json.dumps({
            "mcpServers": {
                "demo-mcp": {
                    "type": "stdio", "command": "node",
                    "args": ["./server.js"], "description": "Demo MCP server",
                },
            },
        }).encode(), "application/json")),
        ("files", ("bundle/server.js", b"console.log('mcp')\n", "text/javascript")),
    ]
    data = {
        "runtime": "fake-runtime",
        "paths": json.dumps(["bundle/mcp.json", "bundle/server.js"]),
        "overwrite": "false",
    }
    r = await client.post(f"{API}/tooling/import/mcp", data=data, files=files,
                          headers=human_headers())
    assert r.status_code == 201, r.text
    assert [x["name"] for x in r.json()["imported"]] == ["demo-mcp"]

    servers = (await client.get(f"{API}/mcp/servers", headers=human_headers())).json()
    server = next(x for x in servers if x["name"] == "demo-mcp")
    assert server["description"] == "Demo MCP server"
    assert server["runtime"] == "fake-runtime"
    assert server["args"][0].startswith(str(tmp_path / "data" / "imported_mcp"))
    from pathlib import Path
    assert Path(server["args"][0]).is_file()
