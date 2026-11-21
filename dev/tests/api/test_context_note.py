"""context_note：task-wide、system 作者、跨 slice 带入但不唤醒。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from anvil.db import open_conn
from anvil.util import now_iso
from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("dev", "qa", "other"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def _task(client, tid="T-NOTE"):
    r = await client.post(
        f"{API}/tasks", json=task_payload(id_=tid, assignees=["dev", "qa"]),
        headers=auth_headers())
    assert r.status_code == 201, r.text


def _activity(app, tid):
    conn = sqlite3.connect(app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT kind, role_slug, slice_key, payload_json FROM activities "
            "WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    finally:
        conn.close()


async def test_context_note_is_task_wide_system_activity(client, app):
    await _seed(client)
    await _task(client)

    r = await client.post(
        f"{API}/tasks/T-NOTE/note",
        headers=auth_headers("dev", "tok-note-dev"),
        json={"body": "未来 verify 步骤注意：接口契约见 docs/api.md",
              "for_step": "S2"})
    assert r.status_code == 200, r.text

    row = _activity(app, "T-NOTE")
    assert row["kind"] == "comment"
    assert row["role_slug"] is None       # system 作者：不被“自己发出”过滤
    assert row["slice_key"] is None       # task-wide：任意 slice 可见
    payload = json.loads(row["payload_json"])
    assert payload["author"] == "dev"
    assert payload["for_step"] == "S2"
    assert "接口契约" in payload["body"]
    assert payload["body"].startswith("系统备注")

    # 其它角色/其它 slice 的 unread 能看到，且不会产生 mention/wake/run。
    conn = open_conn(app.state.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE task_id='T-NOTE' "
            "AND kind='mention'").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE task_id='T-NOTE'").fetchone()["n"] == 0
    finally:
        conn.close()


async def test_context_note_visible_to_future_slice_and_not_human_reply(client, app):
    from anvil.services import agent_inbox
    from anvil.services import tasks as tasks_service

    await _seed(client)
    await _task(client)
    conn = open_conn(app.state.db_path)
    try:
        tasks_service.mention(conn, "T-NOTE", "dev", "human", "请确认口径")
        assert tasks_service.waiting_for_human(conn, "T-NOTE", "dev") is True
    finally:
        conn.close()

    r = await client.post(
        f"{API}/tasks/T-NOTE/note",
        headers=auth_headers("dev", "tok-note-dev"),
        json={"body": "给 S2：先验收委派 C1，不要重复", "for_step": "S2"})
    assert r.status_code == 200, r.text

    conn = open_conn(app.state.db_path)
    try:
        # 未来 slice 的角色能看到 note。
        assert agent_inbox.unread_count(conn, "qa", "T-NOTE", slice_key="B") >= 1
        texts = "\n".join(x["text"] for x in agent_inbox.items(
            conn, "qa", "T-NOTE", slice_key="B"))
        assert "先验收委派 C1" in texts
        # system note 不是人类回复，HITL 等待状态不被破坏。
        assert tasks_service.waiting_for_human(conn, "T-NOTE", "dev") is True
    finally:
        conn.close()


async def test_context_note_permission_and_future_prompt(client, app):
    from anvil.db import connect
    from anvil.graph import nodes
    from anvil.services import runs as runs_service
    from anvil.services import workflow as workflow_service

    await _seed(client)
    await _task(client, "T-NOTE-PERM")

    denied = await client.post(
        f"{API}/tasks/T-NOTE-PERM/note",
        headers=auth_headers("other", "tok-note-other"),
        json={"body": "越权备注"})
    assert denied.status_code == 403

    # workflow-only 角色（不在 task_assignees）也可以写 note。
    conn = open_conn(app.state.db_path)
    try:
        wf = workflow_service.create_workflow(conn, "T-NOTE-PERM", {
            "steps": [{"id": "S1", "title": "x", "role": "qa"}]})
        conn.commit()
    finally:
        conn.close()
    allowed = await client.post(
        f"{API}/tasks/T-NOTE-PERM/note",
        headers=auth_headers("qa", "tok-note-qa"),
        json={"body": "workflow-only 角色的备注", "for_step": "S1"})
    assert allowed.status_code == 200, allowed.text

    # 未来步骤的 prompt 未读块会带上 note。
    conn = connect(app.state.db_path)
    try:
        run = runs_service.create_run(
            conn, "qa", "T-NOTE-PERM", prompt="步骤说明", step_id="S1",
            workflow_id=wf["id"])
        prompt = nodes._prompt_for_run(conn, run, {"title": "t", "description": "d"})
        assert "workflow-only 角色的备注" in prompt
    finally:
        conn.close()
