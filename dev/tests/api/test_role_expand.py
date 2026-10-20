"""Agent 自行扩展团队：继承 runtime、每日配额、human 不可调用、通知 human。"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, role_payload, runtime_payload, task_payload  # noqa: E402

GEN = {
    "display_name": "寻路专家",
    "description": "负责 A* 寻路与性能",
    "capabilities": {"algorithms": 0.9},
    "responsibilities": ["寻路算法", "性能调优"],
    "system_prompt": "你是寻路专家，主动同步进展。",
}


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("pm"), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(id="T-EXP"), headers=auth_headers())


async def test_agent_expands_team_and_notifies_human(client, seeded, monkeypatch):
    monkeypatch.setattr("anvil.api.roles.llm.generate", lambda prompt: json.dumps(GEN))
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                          json={"brief": "负责 A* 寻路", "task_id": "T-EXP"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["display_name"] == "寻路专家"
    got = await client.get(f"{API}/roles/{body['slug']}", headers=auth_headers())
    definition = got.json()["definition"]
    assert definition["runtime"]["id"] == "claude-code"
    assert definition["created_by"] == "agent:pm"
    # human 在任务会话收到通知
    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    assert any(i["kind"] == "mention" and "新 agent" in i["title"] for i in inbox["items"])


async def test_expand_quota_and_human_forbidden(client, seeded, monkeypatch):
    monkeypatch.setattr("anvil.api.roles.llm.generate", lambda prompt: json.dumps(GEN))
    for i in range(3):
        r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                              json={"brief": f"角色 {i}"})
        assert r.status_code == 201, r.text
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                          json={"brief": "第四个"})
    assert r.status_code == 429
    # human 会话不能调用
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("human"),
                          json={"brief": "human"})
    assert r.status_code == 403
