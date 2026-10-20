"""P0 安全：人工确认点必须是自然人；Agent 角色令牌不得代审/代删/代批。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def test_agent_cannot_review_task(client, seeded):
    await client.post(f"{API}/tasks", json=task_payload(id="T-SEC"), headers=auth_headers())
    await client.patch(f"{API}/tasks/T-SEC", json={"status": "in_review"},
                       headers=human_headers())
    # Agent 令牌 → 403
    r = await client.post(f"{API}/tasks/T-SEC/review", json={"approve": True},
                          headers=auth_headers("pm"))
    assert r.status_code == 403
    # 自然人 → 200
    r = await client.post(f"{API}/tasks/T-SEC/review", json={"approve": True},
                          headers=human_headers())
    assert r.status_code == 200 and r.json()["status"] == "done"


async def test_agent_cannot_review_workflow_gate(client, seeded, app):
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-WFSEC", assignees=["pm", "developer"]), headers=auth_headers())
    d = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "g", "source_task": "T-WFSEC", "kind": "workflow",
        "tree": {"steps": [
            {"id": "S1", "title": "需求", "role": "pm", "gate": True},
            {"id": "S2", "title": "实现", "role": "developer", "depends_on": ["S1"]},
        ]}})
    assert d.status_code == 201
    wid = (await client.get(f"{API}/tasks/T-WFSEC/workflow", headers=human_headers())).status_code
    # 先人工批准草案
    assert (await client.post(f"{API}/board/drafts/approve",
                              json={"draft_ids": [d.json()["id"]]},
                              headers=human_headers())).status_code == 200
    wf = (await client.get(f"{API}/tasks/T-WFSEC/workflow", headers=human_headers())).json()
    wid = wf["id"]
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='submitted' WHERE workflow_id=? AND step_id='S1'", (wid,))
    conn.commit(); conn.close()

    r = await client.post(f"{API}/workflows/{wid}/steps/S1/review",
                          json={"approve": True}, headers=auth_headers("developer"))
    assert r.status_code == 403
    r = await client.post(f"{API}/workflows/{wid}/steps/S1/review",
                          json={"approve": True}, headers=human_headers())
    assert r.status_code == 200


async def test_agent_cannot_delete_or_patch_task(client, seeded):
    await client.post(f"{API}/tasks", json=task_payload(id="T-DEL-SEC"), headers=auth_headers())
    assert (await client.patch(f"{API}/tasks/T-DEL-SEC", json={"title": "hack"},
                               headers=auth_headers("pm"))).status_code == 403
    assert (await client.delete(f"{API}/tasks/T-DEL-SEC",
                                headers=auth_headers("pm"))).status_code == 403
    assert (await client.delete(f"{API}/tasks/T-DEL-SEC",
                                headers=human_headers())).status_code == 204


async def test_agent_cannot_approve_drafts_or_budget(client, seeded):
    await client.post(f"{API}/tasks", json=task_payload(id="T-D2"), headers=auth_headers())
    d = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "g", "source_task": "T-D2",
        "tree": {"children": [{"id": "S1", "title": "s", "role": "pm"}]}})
    r = await client.post(f"{API}/board/drafts/approve", json={"draft_ids": [d.json()["id"]]},
                          headers=auth_headers("pm"))
    assert r.status_code == 403
    assert (await client.put(f"{API}/budget", json={"max_parallel_system": 6},
                             headers=auth_headers("pm"))).status_code == 403
