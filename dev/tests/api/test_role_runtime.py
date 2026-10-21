"""角色绑定运行时：保留其它定义、权限边界。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload  # noqa: E402


async def test_update_role_runtime_preserves_definition(client):
    await client.post(f"{API}/runtimes", json=runtime_payload("rt-a"), headers=auth_headers())
    await client.post(f"{API}/runtimes", json=runtime_payload("rt-b"), headers=auth_headers())
    payload = role_payload("dev", runtime={"id": "rt-a"},
                           system_prompt="keep me", responsibilities=["x"])
    r = await client.post(f"{API}/roles", json=payload, headers=auth_headers())
    assert r.status_code == 201, r.text

    r = await client.put(f"{API}/roles/dev/runtime", headers=human_headers(), json={"id": "rt-b"})
    assert r.status_code == 200, r.text
    definition = r.json()["definition"]
    assert definition["runtime"]["id"] == "rt-b"
    assert definition["system_prompt"] == "keep me"
    assert definition["responsibilities"] == ["x"]


async def test_update_role_runtime_rejects_unknown(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())
    r = await client.put(f"{API}/roles/dev/runtime", headers=human_headers(),
                         json={"id": "nope"})
    assert r.status_code == 404


async def test_update_role_runtime_requires_human(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())
    r = await client.put(f"{API}/roles/dev/runtime", headers=auth_headers(),
                         json={"id": "claude-code"})
    assert r.status_code == 403
