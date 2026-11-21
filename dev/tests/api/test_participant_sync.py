"""最终审批：workflow 角色与任务参与者一致（自动加入 / 阻止缺员）。"""
from __future__ import annotations

import pytest

from anvil.db import open_conn
from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")

TREE_PM_DEV = {"steps": [
    {"id": "S1", "title": "执行", "role": "pm"},
    {"id": "S2", "title": "开发", "role": "developer", "depends_on": ["S1"]},
]}


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer", "tester"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def _draft(client, tid: str, tree: dict) -> dict:
    r = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "角色一致性", "source_task": tid, "kind": "workflow", "tree": tree})
    assert r.status_code == 201, r.text
    return r.json()


async def _approve(client, draft_id: str):
    return await client.post(
        f"{API}/board/drafts/approve", headers=human_headers(),
        json={"draft_ids": [draft_id]})


def _assignees(app, tid: str) -> set[str]:
    conn = open_conn(app.state.db_path)
    try:
        return {row["role_slug"] for row in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (tid,))}
    finally:
        conn.close()


async def test_approval_adds_workflow_roles_to_participants(client, app):
    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-SYNC", assignees=["pm"]), headers=auth_headers())
    draft = await _draft(client, "T-SYNC", TREE_PM_DEV)

    approved = await _approve(client, draft["id"])
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert "developer" in result["participants_added"][draft["id"]]
    assert _assignees(app, "T-SYNC") == {"pm", "developer"}


async def test_approval_blocks_missing_must_participate_role(client, app):
    from anvil.services import tasks as tasks_service

    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-SYNC-MISS", assignees=["pm"]), headers=auth_headers())
    draft = await _draft(client, "T-SYNC-MISS", {"steps": [
        {"id": "S1", "title": "执行", "role": "pm"}]})
    conn = open_conn(app.state.db_path)
    try:
        tasks_service.add_assignees(conn, "T-SYNC-MISS", ["tester"])
    finally:
        conn.close()

    approved = await _approve(client, draft["id"])
    assert approved.status_code == 409, approved.text
    assert "missing" in approved.text and "tester" in approved.text


async def test_approval_rejects_unknown_workflow_role(client, app):
    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-SYNC-GHOST", assignees=["pm"]), headers=auth_headers())
    draft = await _draft(client, "T-SYNC-GHOST", {"steps": [
        {"id": "S1", "title": "执行", "role": "pm"},
        {"id": "S2", "title": "幽灵", "role": "ghost", "depends_on": ["S1"]},
    ]})

    approved = await _approve(client, draft["id"])
    assert approved.status_code == 409, approved.text
    assert "ghost" in approved.text


async def test_cannot_remove_role_referenced_by_running_workflow(client, app):
    from anvil.services import tasks as tasks_service, workflow as wf_service
    from anvil.util import ConflictError, now_iso

    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-SYNC-RUN", assignees=["pm", "developer"]), headers=auth_headers())
    conn = open_conn(app.state.db_path)
    try:
        wf_service.create_workflow(conn, "T-SYNC-RUN", TREE_PM_DEV,
                                   enforce_role_coverage=False)
        conn.commit()
        with pytest.raises(ConflictError, match="running workflow"):
            tasks_service.assign_roles(conn, "T-SYNC-RUN", ["pm"])
    finally:
        conn.close()


async def test_revision_introduced_role_becomes_participant(client, app):
    from anvil.db import open_conn
    from anvil.services import workflow as wf_service

    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-SYNC-REV", assignees=["pm"]), headers=auth_headers())
    conn = open_conn(app.state.db_path)
    try:
        wf = wf_service.create_workflow(
            conn, "T-SYNC-REV", {"steps": [{"id": "S1", "title": "执行",
                                            "role": "pm"}]},
            enforce_role_coverage=False)
        added = wf_service._sync_revision_participants(
            conn, {"id": wf["id"], "task_id": "T-SYNC-REV"},
            {"generated": [{"id": "S2", "role": "developer"}],
             "patched": {}, "nodes": []})
        conn.commit()
        assert added == ["developer"]
        assert _assignees(app, "T-SYNC-REV") == {"pm", "developer"}
    finally:
        conn.close()


async def test_draft_participants_preview(client, app):
    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(
        id_="T-SYNC-PREVIEW", assignees=["pm"]), headers=auth_headers())
    draft = await _draft(client, "T-SYNC-PREVIEW", TREE_PM_DEV)

    preview = await client.get(
        f"{API}/board/drafts/{draft['id']}/participants",
        headers=human_headers())
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["add"] == ["developer"]
    assert data["missing"] == []
    assert data["roles"] == ["developer", "pm"]
