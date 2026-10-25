"""Gate packet API smoke."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload


@pytest.mark.asyncio
async def test_gate_packet_api(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("developer", "tester"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-PKT", assignees=["developer"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("developer"), json={
        "goal": "packet", "source_task": "T-PKT", "kind": "workflow",
        "tree": {"steps": [{"id": "S1", "title": "评审", "role": "developer",
                            "gate": True, "reviewer": "tester"}]},
    })
    assert draft.status_code == 201, draft.text
    approved = await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                                 json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text
    wf = (await client.get(f"{API}/tasks/T-PKT/workflow", headers=auth_headers())).json()
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='running' WHERE workflow_id=? AND step_id='S1'",
                 (wf["id"],))
    conn.commit()
    conn.close()
    submitted = await client.post(f"{API}/workflows/{wf['id']}/steps/S1/submit",
                                  headers=auth_headers("developer"), json={"summary": "完成"})
    assert submitted.status_code == 200, submitted.text
    packet = await client.get(f"{API}/workflows/{wf['id']}/steps/S1/packet",
                              headers=human_headers())
    assert packet.status_code == 200, packet.text
    body = packet.json()
    assert body["packet"]["authority"] == "human"
    assert "评审" in body["packet"]["question"]
    assert body["can_decide"]["human"] is True


@pytest.mark.asyncio
async def test_joint_decision_api_recommends_then_human_approves(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("developer", "lead"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-JOINT", assignees=["developer", "lead"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("developer"), json={
        "goal": "joint", "source_task": "T-JOINT", "kind": "workflow",
        "tree": {"steps": [
            {"id": "S1", "title": "实现", "role": "developer"},
            {"id": "D1", "title": "联合验收", "role": "lead", "kind": "decision",
             "decision_authority": "joint", "depends_on": ["S1"], "on_reject": ["S1"]},
        ]},
    })
    assert draft.status_code == 201, draft.text
    approved = await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                                 json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text
    wf = (await client.get(f"{API}/tasks/T-JOINT/workflow", headers=auth_headers())).json()
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE workflow_steps SET status='approved' WHERE workflow_id=? AND step_id='S1'",
                 (wf["id"],))
    conn.execute("UPDATE workflow_steps SET status='running' WHERE workflow_id=? AND step_id='D1'",
                 (wf["id"],))
    conn.commit()
    conn.close()

    rec = await client.post(f"{API}/workflows/{wf['id']}/steps/D1/decide",
                            headers=auth_headers("lead"),
                            json={"approve": True, "feedback": "建议通过"})
    assert rec.status_code == 200, rec.text
    d1 = next(s for s in rec.json()["steps"] if s["step_id"] == "D1")
    assert d1["status"] == "submitted"
    packet = await client.get(f"{API}/workflows/{wf['id']}/steps/D1/packet",
                              headers=human_headers())
    body = packet.json()
    assert body["packet"]["status"] == "pending_human"
    assert body["packet"]["recommendation"]["decision"] == "approve"
    assert body["decisions"][-1]["decision"] == "recommend_approve"

    done = await client.post(f"{API}/workflows/{wf['id']}/steps/D1/decide",
                             headers=human_headers(),
                             json={"approve": True, "feedback": "同意",
                                   "packet_id": body["packet"]["id"]})
    assert done.status_code == 200, done.text
    d1 = next(s for s in done.json()["steps"] if s["step_id"] == "D1")
    assert d1["status"] == "approved"
