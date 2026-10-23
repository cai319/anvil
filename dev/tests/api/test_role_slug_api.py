"""角色 slug 校验：只允许小写 ASCII/数字/-/_，禁止中文等字符。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, role_payload, runtime_payload  # noqa: E402


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())


@pytest.mark.parametrize("slug", [
    "天才", "Role", "bad slug", "a", "-lead", "角色-1", "x" * 65, "emoji😀",
    # 系统伪发送者/身份，不能注册为真实 Agent 角色
    "workflow", "system", "human",
])
async def test_invalid_role_slug_rejected(client, seeded, slug):
    r = await client.post(f"{API}/roles", json=role_payload(slug),
                          headers=auth_headers())
    assert r.status_code == 422, r.text
    assert "slug" in r.text


async def test_valid_role_slug_accepted(client, seeded):
    r = await client.post(f"{API}/roles", json=role_payload("security-auditor"),
                          headers=auth_headers())
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "security-auditor"
