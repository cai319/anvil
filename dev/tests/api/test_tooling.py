"""M3 工具治理：skills CRUD/投影、MCP 握商、工具预设。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anvil import config
from tests.conftest import API, app_factory, auth_headers, role_payload, runtime_payload


@pytest.fixture
def app(tmp_path, monkeypatch):
    data = tmp_path / "data"
    agents = data / "agents"
    repos = data / "repos"
    runtimes = data / "runtimes"
    for d in (agents, repos, runtimes):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "REPOS_DIR", repos)
    monkeypatch.setattr(config, "USER_RUNTIMES_DIR", runtimes)
    return app_factory.create_app(
        {"db_url": f"sqlite+aiosqlite:///{tmp_path}/test.db", "test_auth": True}
    )


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(executable_path="/bin/echo"),
                      headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload(), headers=auth_headers())


async def test_skill_crud_versions_and_projection(client):
    await _seed(client)
    created = await client.post(f"{API}/skills", json={
        "name": "my-skill", "description": "测试技能",
        "content": "---\nname: my-skill\ndescription: test\n---\n\nUse it.\n",
    }, headers=auth_headers())
    assert created.status_code == 201
    sid = created.json()["id"]

    updated = await client.put(f"{API}/skills/{sid}", json={"content": "v2 content"},
                               headers=auth_headers())
    assert updated.status_code == 200
    versions = (await client.get(f"{API}/skills/{sid}/versions", headers=auth_headers())).json()
    assert len(versions) == 2
    compat = (await client.get(f"{API}/skills/{sid}/compat", headers=auth_headers())).json()
    assert compat["compatible"] == "all"

    # 角色勾选 skill → 派发后投影到 .claude/skills/<name>/SKILL.md
    await client.put(f"{API}/roles/qa-role/catalog", json={"skills": ["my-skill"], "mcp": []},
                     headers=auth_headers())
    run = (await client.post(f"{API}/runs", json={"role_slug": "qa-role"}, headers=auth_headers())).json()
    home = Path(run["worktree_path"]) if run["worktree_path"] else Path(config.AGENTS_DIR) / "qa-role"
    # 无任务 run 用角色 home 作为工作区
    target = Path(config.AGENTS_DIR) / "qa-role" / ".claude" / "skills" / "my-skill" / "SKILL.md"
    assert target.is_file()
    assert "v2 content" in target.read_text(encoding="utf-8")

    deleted = await client.delete(f"{API}/skills/{sid}", headers=auth_headers())
    assert deleted.status_code == 204


def _fake_mcp_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}}), flush=True)\n"
        "    elif method == 'tools/list':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':[{'name':'ping','description':'ping tool'}]}}), flush=True)\n",
        encoding="utf-8",
    )
    return script


async def test_mcp_crud_probe_and_projection(client, tmp_path):
    await _seed(client)
    script = _fake_mcp_script(tmp_path)
    created = await client.post(f"{API}/mcp/servers", json={
        "name": "fake-mcp", "transport": "stdio", "command": sys.executable,
        "args": [str(script)],
    }, headers=auth_headers())
    assert created.status_code == 201
    mid = created.json()["id"]

    probed = await client.post(f"{API}/mcp/servers/{mid}/test", headers=auth_headers())
    assert probed.status_code == 200
    assert probed.json()["ok"] is True
    assert probed.json()["tools"][0]["name"] == "ping"

    tools = (await client.get(f"{API}/mcp/servers/{mid}/tools", headers=auth_headers())).json()
    assert tools["tools"][0]["name"] == "ping"
    assert tools["tools"][0]["estimated_tokens"] > 0

    # 角色勾选 MCP → 投影到 settings.json
    await client.put(f"{API}/roles/qa-role/catalog", json={"skills": [], "mcp": ["fake-mcp"]},
                     headers=auth_headers())
    await client.post(f"{API}/runs", json={"role_slug": "qa-role"}, headers=auth_headers())
    settings = Path(config.AGENTS_DIR) / "qa-role" / ".claude" / "settings.json"
    assert settings.is_file()
    assert "fake-mcp" in settings.read_text(encoding="utf-8")

    deleted = await client.delete(f"{API}/mcp/servers/{mid}", headers=auth_headers())
    assert deleted.status_code == 204


async def test_tool_presets(client):
    presets = (await client.get(f"{API}/tools/presets", headers=auth_headers())).json()
    assert "minimal" in presets
    assert "max_tool_tokens" in presets["minimal"]
