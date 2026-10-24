"""Workflow template studio: versioning, import, apply and draft editing."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload


def template_definition() -> dict:
    return {
        "schema_version": 1,
        "role_slots": [
            {"key": "executor", "label": "执行者", "suggested_roles": ["developer"]},
        ],
        "steps": [
            {"id": "S1", "title": "执行任务", "role_slot": "executor",
             "depends_on": [], "acceptance": ["完成"]},
        ],
    }


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer", "tester"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-TPL", assignees=["developer"]), headers=auth_headers())


async def _published_template(client) -> dict:
    r = await client.post(f"{API}/workflow-templates", headers=human_headers(), json={
        "name": "单步模板", "definition": template_definition(), "use_mode": "both",
    })
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await client.put(f"{API}/workflow-templates/{tid}/draft", headers=human_headers(), json={
        "definition": template_definition(), "layout": {},
        "name": "单步模板", "description": "", "use_mode": "both", "base_revision": 1,
    })
    assert r.status_code == 200, r.text
    r = await client.post(f"{API}/workflow-templates/{tid}/publish",
                          headers=human_headers(), json={})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_template_publish_and_apply(client, seeded):
    template = await _published_template(client)
    tid = template["id"]
    assert template["published_version"] == 1

    preview = await client.post(f"{API}/workflow-templates/{tid}/preview-apply",
                                headers=human_headers(), json={
        "task_id": "T-TPL", "version": 1,
        "role_mapping": {"executor": "developer"},
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["issues"] == []

    applied = await client.post(f"{API}/tasks/T-TPL/workflow/from-template",
                                headers=human_headers(), json={
        "template_id": tid, "version": 1,
        "role_mapping": {"executor": "developer"},
    })
    assert applied.status_code == 200, applied.text
    wf = applied.json()["workflow"]
    assert wf["template_id"] == tid
    assert wf["template_version"] == 1
    assert wf["steps"][0]["role_slug"] == "developer"

    version = await client.get(f"{API}/workflow-templates/{tid}/versions/1",
                               headers=human_headers())
    assert version.status_code == 200, version.text
    assert version.json()["state"] == "published"

    rolled = await client.post(f"{API}/workflow-templates/{tid}/rollback",
                               headers=human_headers(), json={"version": 1})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["draft_version"] == 2


@pytest.mark.asyncio
async def test_import_workflow_keeps_loop_and_slots(client, seeded):
    await _published_template(client)
    # Create a workflow from a draft with a plan_slices + loop.
    tree = {
        "steps": [
            {"id": "P1", "title": "切片计划", "role": "developer",
             "kind": "plan_slices", "depends_on": []},
        ],
        "loop": {
            "from": "P1",
            "execute": {"role": "developer", "title": "执行"},
            "verify": {"role": "tester", "title": "验证"},
            "fix": {"role": "developer", "title": "修复"},
            "verify2": {"role": "tester", "title": "复验"},
            "decide": {"role": "pm", "title": "决策", "max_rework": 2},
        },
    }
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-IMP", assignees=["developer", "tester", "pm"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "导入", "source_task": "T-IMP", "kind": "workflow", "tree": tree})
    assert draft.status_code == 201, draft.text
    await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                      json={"draft_ids": [draft.json()["id"]]})
    wf = (await client.get(f"{API}/tasks/T-IMP/workflow", headers=auth_headers())).json()

    imported = await client.post(f"{API}/workflow-templates/import", headers=human_headers(),
                                 json={"source_kind": "workflow", "source_id": wf["id"],
                                       "name": "导入模板"})
    assert imported.status_code == 201, imported.text
    definition = imported.json()["draft"]["definition"]
    assert definition["loop"]["from"] == "P1"
    assert {slot["key"] for slot in definition["role_slots"]} >= {"executor", "verifier", "decider"}
    assert all("role_slot" in step for step in definition["steps"])


@pytest.mark.asyncio
async def test_pending_workflow_draft_can_be_edited_by_human(client, seeded):
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-DRAFT", assignees=["developer"]), headers=auth_headers())
    tree = {"steps": [{"id": "S1", "title": "原计划", "role": "developer"}]}
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "草案", "source_task": "T-DRAFT", "kind": "workflow", "tree": tree})
    did = draft.json()["id"]
    updated = await client.patch(f"{API}/board/drafts/{did}", headers=human_headers(), json={
        "tree": {"steps": [{"id": "S1", "title": "人工修改", "role": "developer"}]}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "pending"
    assert "人工修改" in updated.json()["tree_json"]


@pytest.mark.asyncio
async def test_template_mutations_require_human(client, seeded):
    r = await client.post(f"{API}/workflow-templates", headers=auth_headers("pm"),
                          json={"name": "agent 模板"})
    assert r.status_code == 403
