"""GET /tasks/{tid}/children：委派/派遣统一 child 投影端点。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.util import now_iso
from tests.conftest import API, auth_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post(
            f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    created = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-PARENT", assignees=["pm", "developer"]),
        headers=auth_headers())
    assert created.status_code == 201, created.text


def _insert_children(app) -> None:
    now = now_iso()
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id,title,status,type,parent_id,parent_step_id,assignee,"
            "created_by,acceptance_json,scope_paths_json,created_at,updated_at) "
            "VALUES ('C-DEL','委派子任务','in_review','delegation','T-PARENT','S1',"
            "'developer','agent:pm','[\"验收\"]','[\"poc/\"]',?,?)", (now, now))
        conn.execute(
            "INSERT INTO tasks (id,title,status,type,parent_id,parent_step_id,assignee,"
            "created_by,acceptance_json,scope_paths_json,created_at,updated_at) "
            "VALUES ('C-DISP','派遣子任务','todo','dispatch','T-PARENT',NULL,"
            "'developer','human','[]','[]',?,?)", (now, now))
        conn.execute(
            "INSERT INTO task_dispatches (id,source_task_id,source_workflow_id,"
            "source_step_id,source_slice_key,requester_run_id,requested_by,"
            "target_task_id,target_role,request_hash,return_context_key,return_run_id,"
            "return_step_id,reason_code,reason_json,status,attempts,created_at,updated_at) "
            "VALUES ('D-1','T-PARENT',NULL,NULL,'A',NULL,'human','C-DISP','developer',"
            "'h1',NULL,NULL,NULL,'explicit','{}','return_ready',0,?,?)", (now, now))
        conn.commit()
    finally:
        conn.close()


async def test_task_children_unified_projection(client, app):
    await _seed(client)
    _insert_children(app)

    r = await client.get(
        f"{API}/tasks/T-PARENT/children", headers=auth_headers("pm"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"total": 2, "delegation": 1, "dispatch": 1}
    by_type = {item["relation_type"]: item for item in body["items"]}
    assert by_type["delegation"]["blocking"] is True
    assert by_type["delegation"]["status"] == "in_review"
    assert by_type["delegation"]["step_title"] is None
    assert by_type["dispatch"]["blocking"] is False
    assert by_type["dispatch"]["dispatch_status"] == "return_ready"
    assert by_type["dispatch"]["return_pending"] is True
    assert by_type["dispatch"]["status"] == "todo"


async def test_task_children_empty_and_404(client, app):
    await _seed(client)
    r = await client.get(
        f"{API}/tasks/T-PARENT/children", headers=auth_headers("pm"))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "items": [],
        "counts": {"total": 0, "delegation": 0, "dispatch": 0},
    }

    missing = await client.get(
        f"{API}/tasks/T-NOPE/children", headers=auth_headers("pm"))
    assert missing.status_code == 404
