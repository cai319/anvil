"""角色技能/MCP：默认全不选、按角色独立保存、可清空。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload  # noqa: E402


async def test_new_role_catalog_is_empty(client):
    """新建角色默认不勾选任何 skill/MCP。"""
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())

    r = await client.get(f"{API}/roles/dev/catalog")
    assert r.status_code == 200, r.text
    assert r.json() == {"skills": [], "mcp": []}


async def test_role_catalog_is_per_role_and_clearable(client):
    """角色 A 的选择不影响角色 B；清空后回到全不选。"""
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev-a"), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev-b"), headers=auth_headers())

    r = await client.put(f"{API}/roles/dev-a/catalog", headers=human_headers(),
                         json={"skills": ["pdf"], "mcp": ["codegraph"]})
    assert r.status_code == 200, r.text

    a = (await client.get(f"{API}/roles/dev-a/catalog")).json()
    b = (await client.get(f"{API}/roles/dev-b/catalog")).json()
    assert a == {"skills": ["pdf"], "mcp": ["codegraph"]}
    assert b == {"skills": [], "mcp": []}

    r = await client.put(f"{API}/roles/dev-a/catalog", headers=human_headers(),
                         json={"skills": [], "mcp": []})
    assert r.status_code == 200, r.text
    a = (await client.get(f"{API}/roles/dev-a/catalog")).json()
    assert a == {"skills": [], "mcp": []}
