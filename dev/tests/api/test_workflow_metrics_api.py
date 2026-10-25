"""Phase 2 API: metrics, planned/actual graph, dossier and weight override."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload


@pytest.mark.asyncio
async def test_metrics_graph_dossier_and_weight_api(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("developer"), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-MET", assignees=["developer"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("developer"), json={
        "goal": "metrics", "source_task": "T-MET", "kind": "workflow",
        "tree": {"steps": [{"id": "S1", "title": "实现", "role": "developer"}]},
    })
    assert draft.status_code == 201, draft.text
    approved = await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                                 json={"draft_ids": [draft.json()["id"]]})
    assert approved.status_code == 200, approved.text
    wf = (await client.get(f"{API}/tasks/T-MET/workflow", headers=auth_headers())).json()
    assert wf["weight"] in ("light", "standard", "strict")

    metrics = await client.get(f"{API}/workflows/{wf['id']}/metrics", headers=auth_headers())
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["steps"]["total"] == 1

    for mode in ("planned", "actual", "diff"):
        graph = await client.get(
            f"{API}/workflows/{wf['id']}/graph?mode={mode}", headers=auth_headers())
        assert graph.status_code == 200, graph.text
        assert graph.json()["mode"] == mode

    dossier = await client.get(f"{API}/workflows/{wf['id']}/dossier", headers=auth_headers())
    assert dossier.status_code == 200, dossier.text
    assert dossier.json()["metrics"]["steps"]["total"] == 1

    md = await client.get(f"{API}/tasks/T-MET/dossier?format=md", headers=auth_headers())
    assert md.status_code == 200, md.text
    assert "执行档案" in md.text

    rebuilt = await client.post(
        f"{API}/workflows/{wf['id']}/dossier/rebuild", headers=human_headers())
    assert rebuilt.status_code == 200, rebuilt.text

    weight = await client.post(f"{API}/tasks/T-MET/workflow-weight",
                               headers=human_headers(),
                               json={"weight": "light", "reason": "小型改动"})
    assert weight.status_code == 200, weight.text
    assert weight.json()["requires_restart"] is True
    assert weight.json()["running_weight"] == wf["weight"]

    cleared = await client.delete(f"{API}/tasks/T-MET/workflow-weight",
                                  headers=human_headers())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["weight"] is None


@pytest.mark.asyncio
async def test_restart_applies_weight_as_new_execution(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("developer"), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-RST", assignees=["developer"]), headers=auth_headers())
    draft = await client.post(f"{API}/board/drafts", headers=auth_headers("developer"), json={
        "goal": "restart", "source_task": "T-RST", "kind": "workflow",
        "tree": {"steps": [{"id": "S1", "title": "实现", "role": "developer"}]},
    })
    await client.post(f"{API}/board/drafts/approve", headers=human_headers(),
                      json={"draft_ids": [draft.json()["id"]]})
    old = (await client.get(f"{API}/tasks/T-RST/workflow", headers=auth_headers())).json()
    saved = await client.post(f"{API}/tasks/T-RST/workflow-weight", headers=human_headers(),
                              json={"weight": "light", "reason": "只改文档"})
    assert saved.json()["requires_restart"] is True
    restarted = await client.post(f"{API}/tasks/T-RST/workflow/restart",
                                  headers=human_headers(), json={"reason": "应用轻量流程"})
    assert restarted.status_code == 200, restarted.text
    new = restarted.json()
    assert new["execution_no"] == old["execution_no"] + 1
    assert new["weight"] == "light"
    assert new["weight_source"] == "human"
    old_row = (await client.get(f"{API}/workflows/{old['id']}", headers=auth_headers())).json()
    assert old_row["status"] == "terminated"
    assert old_row["termination_reason"] == "应用轻量流程"
