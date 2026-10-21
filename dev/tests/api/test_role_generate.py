"""角色生成画像：完整 JSON 与不完整 JSON 的诊断。"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload  # noqa: E402

COMPLETE = {
    "working_mode": "自主推进",
    "skill_description": "擅长测试",
    "responsibilities": ["职责一"],
    "system_prompt": "你是测试角色。",
}


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())


async def test_generate_complete_profile(client, monkeypatch):
    await _seed(client)
    monkeypatch.setattr(
        "anvil.api.roles.utility.generate",
        lambda conn, prompt, **kwargs: json.dumps(COMPLETE, ensure_ascii=False),
    )
    r = await client.post(f"{API}/roles/dev/generate", headers=human_headers(),
                          json={"brief": "负责任务审查"})
    assert r.status_code == 200, r.text
    assert r.json() == COMPLETE


async def test_generate_incomplete_profile_is_diagnosable(client, monkeypatch):
    await _seed(client)
    partial = {
        "working_mode": "",
        "skill_description": "擅长测试",
        # 缺少 system_prompt / responsibilities
    }
    monkeypatch.setattr(
        "anvil.api.roles.utility.generate",
        lambda conn, prompt, **kwargs: json.dumps(partial, ensure_ascii=False),
    )
    r = await client.post(f"{API}/roles/dev/generate", headers=human_headers(),
                          json={"brief": "负责任务审查"})
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "画像不完整" in detail
    assert "working_mode" in detail
    assert "system_prompt" in detail
    assert "responsibilities" in detail
    assert "skill_description" in detail  # 原始输出片段

    # 生成失败不应修改磁盘上的 profile
    profile = (await client.get(f"{API}/roles/dev/profile", headers=auth_headers())).json()
    assert profile == {"system_prompt": "", "responsibilities": []}
