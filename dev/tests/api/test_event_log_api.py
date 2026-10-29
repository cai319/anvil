"""Event ledger API: task-scoped cursor and global filter."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload


@pytest.mark.asyncio
async def test_workflow_events_are_queryable(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("developer"), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-EVT", assignees=["developer"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("developer"), json={
        "goal": "event test", "source_task": "T-EVT", "kind": "workflow",
        "tree": {"steps": [{"id": "S1", "title": "work", "role": "developer"}]},
    })
    approved = await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                                 json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text

    r = await client.get(f"{API}/tasks/T-EVT/events", headers=human_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    types = [item["event_type"] for item in body["items"]]
    assert "execution.started" in types
    assert body["next_after_id"] >= 1

    r = await client.get(f"{API}/event-log?task_id=T-EVT&event_type=execution.started",
                         headers=human_headers())
    assert r.status_code == 200, r.text
    assert any(item["event_type"] == "execution.started" for item in r.json()["items"])
