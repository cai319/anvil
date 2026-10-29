"""Agent 自行扩展团队：继承 runtime、每日配额、human 不可调用、通知 human。"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

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
    await client.post(f"{API}/tasks", json={**task_payload(id="T-EXP"),
                                            "planner": "pm"},
                      headers=auth_headers())


async def _authorize_role_creation(client, slugs: list[str]):
    """走完整 HITL：planner 提交 role_suggestion → human 批准 authorize 草案。"""
    proposed = await client.post(
        f"{API}/board/drafts", headers=auth_headers("pm"),
        json={"goal": "测试新角色", "source_task": "T-EXP", "kind": "role_suggestion",
              "tree": {
                  "apply_mode": "authorize", "rationale": "测试新角色",
                  "requested_assignees": slugs,
                  "suggested_roles": [
                      {"slug": s, "display_name": s, "description": "测试角色"}
                      for s in slugs
                  ],
              }})
    assert proposed.status_code == 201, proposed.text
    approved = await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [proposed.json()["id"]]})
    assert approved.status_code == 200, approved.text


async def test_expand_requires_approved_role_suggestion(client, seeded, monkeypatch):
    """新建角色必须先有 human 批准的 role_suggestion 授权。"""
    monkeypatch.setattr("anvil.api.roles.utility.generate",
                        lambda conn, prompt, **kwargs: json.dumps(GEN))
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                          json={"brief": "绕过审批直接建角色", "task_id": "T-EXP"})
    assert r.status_code == 403, r.text


async def test_agent_expands_team_and_notifies_human(client, seeded, monkeypatch):
    monkeypatch.setattr("anvil.api.roles.utility.generate",
                        lambda conn, prompt, **kwargs: json.dumps(GEN))
    await _authorize_role_creation(client, ["pathfinder"])
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                          json={"brief": "负责 A* 寻路", "slug": "pathfinder",
                                "task_id": "T-EXP"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "pathfinder"
    assert body["display_name"] == "寻路专家"
    got = await client.get(f"{API}/roles/{body['slug']}", headers=auth_headers())
    definition = got.json()["definition"]
    assert definition["runtime"]["id"] == "claude-code"
    assert definition["created_by"] == "agent:pm"
    # human 在任务会话收到通知
    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    assert any(i["kind"] == "mention" and "新 agent" in i["title"] for i in inbox["items"])


async def test_expand_quota_and_human_forbidden(client, seeded, monkeypatch):
    monkeypatch.setattr("anvil.api.roles.utility.generate",
                        lambda conn, prompt, **kwargs: json.dumps(GEN))
    slugs = [f"agent-{i}" for i in range(4)]
    await _authorize_role_creation(client, slugs)
    for i, slug in enumerate(slugs[:3]):
        r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                              json={"brief": f"角色 {i}", "slug": slug,
                                    "task_id": "T-EXP"})
        assert r.status_code == 201, r.text
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("pm"),
                          json={"brief": "第四个", "slug": slugs[3],
                                "task_id": "T-EXP"})
    assert r.status_code == 429
    # human 会话不能调用
    r = await client.post(f"{API}/roles/expand", headers=auth_headers("human"),
                          json={"brief": "human"})
    assert r.status_code == 403
