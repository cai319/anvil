"""角色 skill/MCP 目录治理测试（M3 工具治理）。"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload

pytestmark = [pytest.mark.usefixtures("client")]


async def test_role_catalog_save_and_read(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload(slug="dev"))
    r = await client.put(
        f"{API}/roles/dev/catalog",
        json={"skills": ["pdf", "xlsx"], "mcp": ["codegraph"]},
        headers=human_headers(),
    )
    assert r.status_code == 200
    assert r.json()["skills"] == ["pdf", "xlsx"]
    assert r.json()["mcp"] == ["codegraph"]

    g = await client.get(f"{API}/roles/dev/catalog", headers=auth_headers())
    assert g.json() == {"skills": ["pdf", "xlsx"], "mcp": ["codegraph"]}


async def test_runtime_catalog_structure(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    r = await client.get(f"{API}/runtimes/claude-code/catalog", headers=auth_headers())
    d = r.json()
    assert "skills" in d and "mcp_servers" in d
    assert isinstance(d["skills"], list)
    assert isinstance(d["mcp_servers"], list)


async def test_role_profile_save_and_read(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload(slug="dev"))
    r = await client.put(
        f"{API}/roles/dev/profile",
        json={"system_prompt": "你是后端工程师", "responsibilities": ["api", "db"]},
        headers=human_headers(),
    )
    assert r.status_code == 200
    assert r.json()["system_prompt"] == "你是后端工程师"
    assert r.json()["responsibilities"] == ["api", "db"]
    g = await client.get(f"{API}/roles/dev/profile", headers=auth_headers())
    assert g.json() == {"system_prompt": "你是后端工程师", "responsibilities": ["api", "db"]}
