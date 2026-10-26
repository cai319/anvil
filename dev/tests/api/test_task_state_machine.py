"""任务终态：done 为成功终态，closed 仅表示人工终止。"""
from __future__ import annotations

import pytest

from anvil.db import open_conn
from anvil.services import workflow as workflow_service
from anvil.util import now_iso
from tests.conftest import API, auth_headers, human_headers


async def _create(client, tid: str):
    r = await client.post(f"{API}/tasks", headers=auth_headers(),
                          json={"id": tid, "title": f"状态机 {tid}"})
    assert r.status_code == 201, r.text


async def _make_done(client, tid: str):
    await _create(client, tid)
    r = await client.patch(f"{API}/tasks/{tid}", headers=human_headers(),
                           json={"status": "in_review"})
    assert r.status_code == 200, r.text
    r = await client.post(f"{API}/tasks/{tid}/review", headers=human_headers(),
                          json={"approve": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"


@pytest.mark.asyncio
async def test_non_terminal_can_terminate_and_reopen(client):
    await _create(client, "SM-1")
    r = await client.post(f"{API}/tasks/SM-1/cancel", headers=human_headers())
    assert r.status_code == 200 and r.json()["status"] == "closed"
    r = await client.post(f"{API}/tasks/SM-1/reopen", headers=human_headers())
    assert r.status_code == 200 and r.json()["status"] == "todo"


@pytest.mark.asyncio
async def test_reopen_wakes_suspended_step_run(client, app):
    """重开任务时立即 resume 挂起中的步骤 run，不等 watchdog。"""
    await _create(client, "SM-REOPEN")
    conn = open_conn(app.state.db_path)
    try:
        wf = workflow_service.create_workflow(conn, "SM-REOPEN", {"steps": [
            {"id": "S1", "title": "x", "role": "dev"}]})
        workflow_service.advance(conn, wf["id"],
                                 starter=lambda *a: {"id": "R-SM"})
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at, "
            "step_id, workflow_id, native_session_id) "
            "VALUES ('R-SM','dev','SM-REOPEN','suspended',?,'S1',?,'rs:dev')",
            (now_iso(), wf["id"]))
        conn.commit()
    finally:
        conn.close()

    calls = []
    app.state.orchestrator.pump = (
        lambda db_path, run_id, event, payload: calls.append((run_id, event)))
    r = await client.post(f"{API}/tasks/SM-REOPEN/cancel", headers=human_headers())
    assert r.status_code == 200, r.text
    r = await client.post(f"{API}/tasks/SM-REOPEN/reopen", headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"
    assert calls == [("R-SM", "resume")]


@pytest.mark.asyncio
@pytest.mark.parametrize("proc_status,queue_len,should_wake", [
    ("idle", 0, True),
    ("busy", 0, False),
    ("idle", 1, False),
])
async def test_reopen_wakes_only_idle_persistent_step_run(
        client, app, monkeypatch, proc_status, queue_len, should_wake):
    """常驻 run 仍标 running：空闲才 resume，正在忙/排队时不打断。"""
    await _create(client, "SM-IDLE")
    conn = open_conn(app.state.db_path)
    try:
        wf = workflow_service.create_workflow(conn, "SM-IDLE", {"steps": [
            {"id": "S1", "title": "x", "role": "dev"}]})
        workflow_service.advance(conn, wf["id"],
                                 starter=lambda *a: {"id": "R-IDLE"})
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at, "
            "step_id, workflow_id, native_session_id) "
            "VALUES ('R-IDLE','dev','SM-IDLE','running',?,'S1',?,'rs:dev')",
            (now_iso(), wf["id"]))
        conn.commit()
    finally:
        conn.close()

    class _Mgr:
        def status(self, role):
            return {"alive": True, "status": proc_status,
                    "queue_len": queue_len, "switching": False}

    monkeypatch.setattr("anvil.services.daemon.get_manager", lambda: _Mgr())
    calls = []
    app.state.orchestrator.pump = (
        lambda db_path, run_id, event, payload: calls.append((run_id, event)))
    await client.post(f"{API}/tasks/SM-IDLE/cancel", headers=human_headers())
    r = await client.post(f"{API}/tasks/SM-IDLE/reopen", headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"
    assert (calls == [("R-IDLE", "resume")]) is should_wake


@pytest.mark.asyncio
async def test_done_is_final(client):
    await _make_done(client, "SM-DONE")
    for path, payload in (
        ("cancel", {}),
        ("reopen", {}),
        ("unclaim", {}),
    ):
        r = await client.post(f"{API}/tasks/SM-DONE/{path}",
                              headers=human_headers(), json=payload)
        assert r.status_code == 409, (path, r.text)
    for target in ("closed", "in_progress", "todo"):
        r = await client.patch(f"{API}/tasks/SM-DONE", headers=human_headers(),
                               json={"status": target})
        assert r.status_code == 409, (target, r.text)


@pytest.mark.asyncio
async def test_closed_only_reopens_to_todo(client):
    await _create(client, "SM-CLOSED")
    await client.post(f"{API}/tasks/SM-CLOSED/cancel", headers=human_headers())
    r = await client.post(f"{API}/tasks/SM-CLOSED/cancel", headers=human_headers())
    assert r.status_code == 409
    r = await client.patch(f"{API}/tasks/SM-CLOSED", headers=human_headers(),
                           json={"status": "todo"})
    assert r.status_code == 409
    r = await client.post(f"{API}/tasks/SM-CLOSED/unclaim", headers=human_headers())
    assert r.status_code == 409
    r = await client.post(f"{API}/tasks/SM-CLOSED/reopen", headers=human_headers())
    assert r.status_code == 200 and r.json()["status"] == "todo"


@pytest.mark.asyncio
async def test_patch_non_terminal_transition_still_allowed(client):
    await _create(client, "SM-PATCH")
    r = await client.patch(f"{API}/tasks/SM-PATCH", headers=human_headers(),
                           json={"status": "in_review"})
    assert r.status_code == 200 and r.json()["status"] == "in_review"


@pytest.mark.asyncio
async def test_action_cards_validate_terminal_state(client):
    await _make_done(client, "SM-CARD-DONE")
    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.cancel", "task_id": "SM-CARD-DONE"})
    assert r.status_code == 409
    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.reopen", "task_id": "SM-CARD-DONE"})
    assert r.status_code == 409

    await _create(client, "SM-CARD-OPEN")
    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.reopen", "task_id": "SM-CARD-OPEN"})
    assert r.status_code == 409
    await client.post(f"{API}/tasks/SM-CARD-OPEN/cancel", headers=human_headers())
    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.reopen", "task_id": "SM-CARD-OPEN"})
    assert r.status_code == 201, r.text
