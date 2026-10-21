"""角色画像隔离：新建角色不继承其它角色的 system_prompt/responsibilities。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, role_payload, runtime_payload  # noqa: E402


async def test_new_role_does_not_inherit_profile(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())

    r = await client.post(
        f"{API}/roles", headers=auth_headers(),
        json=role_payload("role-a", system_prompt="A", responsibilities=["a"]),
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/roles", headers=auth_headers(),
        json=role_payload("role-b"),
    )
    assert r.status_code == 201, r.text

    a = (await client.get(f"{API}/roles/role-a/profile", headers=auth_headers())).json()
    b = (await client.get(f"{API}/roles/role-b/profile", headers=auth_headers())).json()
    assert a == {"system_prompt": "A", "responsibilities": ["a"]}
    assert b == {"system_prompt": "", "responsibilities": []}
