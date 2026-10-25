"""v1.30 任务归档：仅终态可归档，默认列表隐藏，支持批量/取消。"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())


async def _done_task(client, tid: str):
    r = await client.post(f"{API}/tasks", json=task_payload(id_=tid), headers=human_headers())
    assert r.status_code == 201, r.text
    assert (await client.post(f"{API}/tasks/{tid}/claim", json={}, headers=auth_headers())).status_code == 200
    assert (await client.post(
        f"{API}/tasks/{tid}/submit", json={"summary": "done"}, headers=auth_headers()
    )).status_code == 200
    assert (await client.post(
        f"{API}/tasks/{tid}/review", json={"approve": True}, headers=human_headers()
    )).status_code == 200


async def test_archive_only_terminal_and_list_filters(client):
    await _seed(client)
    await _done_task(client, "T-DONE")
    todo = await client.post(f"{API}/tasks", json=task_payload(id_="T-TODO"), headers=human_headers())
    assert todo.status_code == 201

    rejected = await client.post(f"{API}/tasks/T-TODO/archive", headers=human_headers())
    assert rejected.status_code == 409

    archived = await client.post(f"{API}/tasks/T-DONE/archive", headers=human_headers())
    assert archived.status_code == 200
    assert archived.json()["archived_at"]

    default_ids = {t["id"] for t in (await client.get(f"{API}/tasks", headers=human_headers())).json()}
    assert "T-DONE" not in default_ids and "T-TODO" in default_ids

    included = {
        t["id"] for t in (
            await client.get(f"{API}/tasks?include_archived=true", headers=human_headers())
        ).json()
    }
    assert {"T-DONE", "T-TODO"} <= included

    only = (await client.get(f"{API}/tasks?archived_only=true", headers=human_headers())).json()
    assert [t["id"] for t in only] == ["T-DONE"]

    restored = await client.post(f"{API}/tasks/T-DONE/unarchive", headers=human_headers())
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    default_ids = {t["id"] for t in (await client.get(f"{API}/tasks", headers=human_headers())).json()}
    assert "T-DONE" in default_ids


async def test_archive_completed_bulk_only_terminal(client):
    await _seed(client)
    await _done_task(client, "T-A")
    await _done_task(client, "T-B")
    await client.post(f"{API}/tasks", json=task_payload(id_="T-C"), headers=human_headers())

    r = await client.post(f"{API}/tasks/archive-completed", headers=human_headers())
    assert r.status_code == 200, r.text
    archived = {t["id"] for t in r.json()}
    assert archived == {"T-A", "T-B"}

    default_ids = {t["id"] for t in (await client.get(f"{API}/tasks", headers=human_headers())).json()}
    assert default_ids == {"T-C"}


async def test_reopen_clears_archived_at(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json=task_payload(id_="T-CLOSE"), headers=human_headers())
    assert (await client.post(f"{API}/tasks/T-CLOSE/claim", json={}, headers=auth_headers())).status_code == 200
    assert (await client.post(
        f"{API}/tasks/T-CLOSE/cancel", json={}, headers=human_headers()
    )).status_code == 200
    assert (await client.post(
        f"{API}/tasks/T-CLOSE/archive", headers=human_headers()
    )).status_code == 200
    reopened = await client.post(f"{API}/tasks/T-CLOSE/reopen", headers=human_headers())
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "todo"
    assert reopened.json()["archived_at"] is None
