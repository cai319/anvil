"""最终评审状态机：驳回唤醒/不回弹；通过收尾 run。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.db import open_conn
from anvil.util import now_iso
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload("qa-role"))


async def _make_review_task(client, app, tid: str = "T-RJ") -> None:
    await _seed(client)
    r = await client.post(
        f"{API}/tasks",
        json=task_payload(id_=tid, assignees=["qa-role"]),
        headers=human_headers())
    assert r.status_code == 201, r.text
    assert (await client.post(
        f"{API}/tasks/{tid}/claim", json={}, headers=auth_headers("qa-role"))).status_code == 200
    assert (await client.post(
        f"{API}/tasks/{tid}/submit", json={"summary": "done"},
        headers=auth_headers("qa-role"))).status_code == 200
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO workflows (id, task_id, definition_json, status, created_by, "
            "created_at, approved_at, updated_at, execution_no) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            ("WF-RJ", tid, "{}", "done", "human", now_iso(), now_iso(), now_iso()))
        conn.commit()
    finally:
        conn.close()


async def test_review_reject_wakes_and_does_not_bounce(client, app):
    await _make_review_task(client, app)
    rejected = await client.post(
        f"{API}/tasks/T-RJ/review",
        json={"approve": False, "feedback": "证据不足，请补复现"},
        headers=human_headers())
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "in_progress"

    conn = open_conn(app.state.db_path)
    try:
        task = conn.execute(
            "SELECT status, review_rejected_at FROM tasks WHERE id='T-RJ'").fetchone()
        assert task["review_rejected_at"]
        wake = conn.execute(
            "SELECT status, note FROM wake_requests WHERE task_id='T-RJ' "
            "AND role_slug='qa-role' AND reason='task.review_rejected'").fetchone()
        assert wake is not None and "证据不足" in wake["note"]
    finally:
        conn.close()

    # scheduler 兜底 phase 不应把驳回任务立刻推回 in_review
    from anvil.scheduler import Scheduler
    conn = open_conn(app.state.db_path)
    try:
        Scheduler.__new__(Scheduler)._finalize_ready_tasks(conn)
    finally:
        conn.close()
    task = (await client.get(f"{API}/tasks/T-RJ", headers=human_headers())).json()
    assert task["status"] == "in_progress"

    # 再次提交清除驳回标记，允许进入下一轮评审
    assert (await client.post(
        f"{API}/tasks/T-RJ/submit", json={"summary": "rework"},
        headers=auth_headers("qa-role"))).status_code == 200
    task = (await client.get(f"{API}/tasks/T-RJ", headers=human_headers())).json()
    assert task["status"] == "in_review"
    assert not task.get("review_rejected_at")


async def test_review_approve_closes_active_runs(client, app):
    await _make_review_task(client, app, "T-RA")
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at, ended_at) "
            "VALUES ('R-SUSP','qa-role','T-RA','suspended',?,'')", (now_iso(),))
        conn.commit()
    finally:
        conn.close()
    approved = await client.post(
        f"{API}/tasks/T-RA/review", json={"approve": True}, headers=human_headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "done"
    conn = open_conn(app.state.db_path)
    try:
        status = conn.execute("SELECT status FROM runs WHERE id='R-SUSP'").fetchone()["status"]
        assert status == "completed"
    finally:
        conn.close()
