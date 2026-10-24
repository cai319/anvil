"""任务终态：done 为成功终态，closed 仅表示人工终止。"""
from __future__ import annotations

import pytest

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
