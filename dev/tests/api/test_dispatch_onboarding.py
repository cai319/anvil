"""计划先行默认化：多角色任务由 onboarding 助理（Cindy）先规划；单角色直接派发。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil import config  # noqa: E402
from anvil.services import repo_catalog  # noqa: E402
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)


@pytest.fixture
async def seeded(client):
    await client.post("/api/v1/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer", "tester"):
        await client.post("/api/v1/roles", json=role_payload(slug), headers=auth_headers())


async def _create_task(client, tid, assignees):
    r = await client.post("/api/v1/tasks", json=task_payload(
        id=tid, assignees=assignees), headers=auth_headers())
    assert r.status_code == 201, r.text


async def test_multi_role_dispatch_plans_with_cindy(client, seeded):
    await _create_task(client, "T-MULTI", ["pm", "developer", "tester"])
    r = await client.post("/api/v1/tasks/T-MULTI/dispatch", headers=human_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "planning"
    assert body["planner"] == "cindy"
    assert body["run"]["role_slug"] == "cindy"
    # Cindy 是自动创建的内置 onboarding 角色
    role = await client.get("/api/v1/roles/cindy", headers=auth_headers())
    assert role.status_code == 200
    # 只拉起 planner，参与角色没有被直接派发
    runs = (await client.get("/api/v1/runs", headers=human_headers())).json()
    assert [x["role_slug"] for x in runs if x.get("task_id") == "T-MULTI"] == ["cindy"]


async def test_zero_assignee_dispatch_plans_with_cindy(client, seeded):
    await _create_task(client, "T-NOROLE", [])
    r = await client.post("/api/v1/tasks/T-NOROLE/dispatch", headers=human_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "planning"
    assert body["planner"] == "cindy"


async def test_single_role_dispatch_is_direct(client, seeded):
    await _create_task(client, "T-SINGLE", ["developer"])
    r = await client.post("/api/v1/tasks/T-SINGLE/dispatch", headers=human_headers())
    assert r.status_code == 201
    body = r.json()
    assert body["mode"] == "direct"
    assert [x["role_slug"] for x in body["runs"]] == ["developer"]


@pytest.mark.asyncio
async def test_single_role_tagless_dispatch_plans_with_cindy(client, seeded, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_BINDING_MODE", "auto")
    monkeypatch.setattr(config, "KNOWLEDGE_CINDY_MODE", "tagless")
    repo_catalog.invalidate_catalog()
    r = await client.post("/api/v1/tasks", headers=auth_headers(), json=task_payload(
        id="T-TAGLESS", assignees=["developer"], tags=[], repo=None))
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/tasks/T-TAGLESS/dispatch", headers=human_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "planning"
    assert body["planner"] == "cindy"


@pytest.mark.asyncio
async def test_dispatch_returns_pending_review_when_draft_exists(client, seeded):
    await _create_task(client, "T-DRAFT", ["pm", "developer"])
    draft = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "g", "source_task": "T-DRAFT",
        "tree": {"leader": "pm", "children": [{"id": "S1", "title": "s", "role": "pm"}]}})
    assert draft.status_code == 201
    r = await client.post("/api/v1/tasks/T-DRAFT/dispatch", headers=human_headers())
    assert r.status_code == 201
    assert r.json()["mode"] == "pending_review"
    assert r.json()["draft_id"] == draft.json()["id"]
