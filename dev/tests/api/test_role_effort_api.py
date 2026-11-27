"""角色思考强度 API：默认值、更新、保留 permission_mode、非法值。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload,
)


async def test_role_effort_defaults_and_update(client):
    await client.post(
        f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    created = await client.post(
        f"{API}/roles", headers=auth_headers(),
        json=role_payload(
            "dev", runtime={"id": "claude-code",
                            "permission_mode": "acceptEdits"}))
    assert created.status_code == 201, created.text
    runtime = created.json()["definition"]["runtime"]
    assert runtime["effort"] == "high"
    assert runtime["permission_mode"] == "acceptEdits"

    updated = await client.put(
        f"{API}/roles/dev/runtime", headers=human_headers(),
        json={"effort": "low"})
    assert updated.status_code == 200, updated.text
    runtime = updated.json()["definition"]["runtime"]
    assert runtime["id"] == "claude-code"
    assert runtime["effort"] == "low"
    assert runtime["permission_mode"] == "acceptEdits"

    bad = await client.put(
        f"{API}/roles/dev/runtime", headers=human_headers(),
        json={"effort": "ultra"})
    assert bad.status_code == 422


async def test_cindy_effort_is_max(client):
    cindy = await client.get(f"{API}/roles/cindy", headers=auth_headers())
    assert cindy.status_code == 200, cindy.text
    assert cindy.json()["definition"]["runtime"]["effort"] == "max"
