"""工作流模板删除契约：永久删除、版本级联、活跃引用拦截、状态筛选。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def template_definition() -> dict:
    return {
        "schema_version": 1,
        "role_slots": [
            {"key": "executor", "label": "执行者", "suggested_roles": ["developer"]},
        ],
        "steps": [
            {"id": "S1", "title": "执行任务", "role_slot": "executor",
             "depends_on": [], "acceptance": ["完成"]},
        ],
    }


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("developer"), headers=auth_headers())


async def _create_template(client, name: str = "删除测试模板", *, publish: bool = False,
                           archive: bool = False) -> dict:
    created = await client.post(f"{API}/workflow-templates", headers=human_headers(), json={
        "name": name, "definition": template_definition(), "use_mode": "both",
    })
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    if publish:
        saved = await client.put(f"{API}/workflow-templates/{tid}/draft",
                                 headers=human_headers(), json={
            "definition": template_definition(), "layout": {},
            "name": name, "description": "", "use_mode": "both", "base_revision": 1,
        })
        assert saved.status_code == 200, saved.text
        published = await client.post(f"{API}/workflow-templates/{tid}/publish",
                                      headers=human_headers(), json={})
        assert published.status_code == 200, published.text
    if archive:
        archived = await client.post(f"{API}/workflow-templates/{tid}/archive",
                                     headers=human_headers())
        assert archived.status_code == 200, archived.text
    detail = await client.get(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert detail.status_code == 200, detail.text
    return detail.json()


def _link_workflow(app, template_id: str, status: str, wid: str) -> None:
    conn = sqlite3.connect(app.state.db_path)
    try:
        now = _now()
        conn.execute(
            "INSERT INTO workflows "
            "(id, task_id, definition_json, status, created_by, created_at, updated_at, "
            " template_id, template_version) VALUES (?,?,?,?,?,?,?,?,?)",
            (wid, "T-REF", "{}", status, "human", now, now, template_id, 1),
        )
        conn.commit()
    finally:
        conn.close()


def _set_plan_seed(app, task_id: str, template_id: str) -> None:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "UPDATE tasks SET plan_seed_json=? WHERE id=?",
            (json.dumps({"template_id": template_id, "version": 1, "name": "seed"}),
             task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_task_status(app, task_id: str, status: str) -> None:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        conn.commit()
    finally:
        conn.close()


def _versions(app, template_id: str) -> int:
    conn = sqlite3.connect(app.state.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM workflow_template_versions WHERE template_id=?",
            (template_id,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_delete_draft_template_cascades_versions_and_audit(client, app, seeded):
    template = await _create_template(client)
    tid = template["id"]
    assert _versions(app, tid) == 1

    deleted = await client.delete(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"{API}/workflow-templates/{tid}")).status_code == 404
    assert _versions(app, tid) == 0

    conn = sqlite3.connect(app.state.db_path)
    try:
        audit = conn.execute(
            "SELECT payload_json FROM audit WHERE action='template.delete' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert audit is not None
        assert tid in audit[0]
    finally:
        conn.close()

    again = await client.delete(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_delete_published_or_archived_template_without_active_refs(client, seeded):
    published = await _create_template(client, "已发布模板", publish=True)
    archived = await _create_template(client, "已归档模板", archive=True)

    for template in (published, archived):
        resp = await client.delete(
            f"{API}/workflow-templates/{template['id']}", headers=human_headers())
        assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_delete_blocked_by_running_workflow(client, app, seeded):
    template = await _create_template(client, publish=True)
    tid = template["id"]
    task = await client.post(f"{API}/tasks", json=task_payload(id_="T-REF"),
                             headers=human_headers())
    assert task.status_code == 201, task.text
    _link_workflow(app, tid, "running", "W-RUNNING")

    blocked = await client.delete(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert blocked.status_code == 409, blocked.text
    assert (await client.get(f"{API}/workflow-templates/{tid}")).status_code == 200

    _set_task_status(app, "T-REF", "done")
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute("UPDATE workflows SET status='done' WHERE id='W-RUNNING'")
        conn.commit()
    finally:
        conn.close()
    allowed = await client.delete(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert allowed.status_code == 204, allowed.text


@pytest.mark.asyncio
async def test_delete_blocked_by_active_plan_seed(client, app, seeded):
    template = await _create_template(client, publish=True)
    tid = template["id"]
    task = await client.post(f"{API}/tasks", json=task_payload(id_="T-SEED"),
                             headers=human_headers())
    assert task.status_code == 201, task.text
    _set_plan_seed(app, "T-SEED", tid)

    blocked = await client.delete(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert blocked.status_code == 409, blocked.text

    _set_task_status(app, "T-SEED", "done")
    allowed = await client.delete(f"{API}/workflow-templates/{tid}", headers=human_headers())
    assert allowed.status_code == 204, allowed.text


@pytest.mark.asyncio
async def test_delete_requires_human_and_status_filters(client, seeded):
    active = await _create_template(client, "活跃模板")
    archived = await _create_template(client, "归档模板", archive=True)

    agent = await client.delete(f"{API}/workflow-templates/{active['id']}",
                                headers=auth_headers("pm"))
    assert agent.status_code == 403, agent.text

    active_list = await client.get(f"{API}/workflow-templates?status=active")
    archived_list = await client.get(f"{API}/workflow-templates?status=archived")
    all_list = await client.get(f"{API}/workflow-templates")
    assert active["id"] in {x["id"] for x in active_list.json()}
    assert archived["id"] not in {x["id"] for x in active_list.json()}
    assert archived["id"] in {x["id"] for x in archived_list.json()}
    assert {active["id"], archived["id"]} <= {x["id"] for x in all_list.json()}

    missing = await client.delete(f"{API}/workflow-templates/not-found", headers=human_headers())
    assert missing.status_code == 404
