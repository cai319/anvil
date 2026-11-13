"""onboarding 助理（Cindy）是系统默认角色：不可删除、不可作为任务参与成员/工作流步骤。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.services import roles as roles_service  # noqa: E402
from tests.conftest import API, auth_headers, human_headers, task_payload  # noqa: E402


async def test_cindy_created_on_startup(client):
    """Cindy 是内置默认助理，启动后无需先规划/派发就应存在且画像完整。"""
    r = await client.get(f"{API}/roles/cindy")
    assert r.status_code == 200, r.text
    body = r.json()
    definition = body["definition"]
    assert body["display_name"].startswith("Cindy")
    assert definition["kind"] == "onboarding"
    assert definition["working_mode"]
    assert definition["skill_description"]
    assert definition["responsibilities"]
    assert "Cindy" in definition["system_prompt"]


async def test_ensure_onboarding_endpoint_is_idempotent(client):
    r = await client.post(f"{API}/roles/onboarding/ensure", headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "cindy"
    profile = await client.get(f"{API}/roles/cindy/profile", headers=auth_headers())
    assert profile.status_code == 200, profile.text
    assert profile.json()["system_prompt"]
    assert profile.json()["responsibilities"]


async def test_cindy_cannot_be_deleted_or_assigned(client, app):
    from anvil.db import open_conn
    conn = open_conn(app.state.db_path)
    roles_service.ensure_onboarding(conn)
    conn.close()

    # 不可删除
    r = await client.delete(f"{API}/roles/cindy", headers=human_headers())
    assert r.status_code == 409, r.text

    # 不可作为任务成员
    r = await client.post(f"{API}/tasks", json=task_payload(
        id="T-ONB", assignees=["cindy"]), headers=auth_headers())
    assert r.status_code == 409, r.text

    # 不可通过 assign 端点绑定
    await client.post(f"{API}/tasks", json=task_payload(id="T-ONB2"), headers=auth_headers())
    r = await client.post(f"{API}/tasks/T-ONB2/assign", json={"roles": ["cindy"]},
                          headers=human_headers())
    assert r.status_code == 409, r.text


def test_ensure_onboarding_backfills_missing_profile(app):
    """旧版 Cindy 只有 kind 时，ensure 会补齐默认任务助理画像。"""
    import json
    from anvil.db import open_conn

    conn = open_conn(app.state.db_path)
    conn.execute(
        "UPDATE roles SET definition_json=? WHERE slug='cindy'",
        (json.dumps({"kind": "onboarding"}),))
    conn.commit()
    role = roles_service.ensure_onboarding(conn)
    definition = json.loads(role["definition_json"])
    assert definition["working_mode"]
    assert definition["skill_description"]
    assert definition["responsibilities"]
    assert definition["system_prompt"]
    conn.close()


def test_cindy_cannot_claim_task(app):
    from anvil.db import open_conn
    from anvil.services import tasks as tasks_service
    from anvil.util import ConflictError, now_iso

    conn = open_conn(app.state.db_path)
    roles_service.ensure_onboarding(conn)
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T-CLAIM','t','todo',?,?)", (now, now))
    conn.commit()
    with pytest.raises(ConflictError):
        tasks_service.claim(conn, "T-CLAIM", "cindy")
    conn.close()


def test_cindy_cannot_execute_workflow_step(app):
    from anvil.db import open_conn
    from anvil.services import workflow as wf_service
    from anvil.util import ConflictError, now_iso

    conn = open_conn(app.state.db_path)
    roles_service.ensure_onboarding(conn)
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T-ONB3','t','todo',?,?)", (now, now))
    conn.commit()
    with pytest.raises(ConflictError):
        wf_service.create_workflow(conn, "T-ONB3", {"steps": [
            {"id": "S1", "title": "x", "role": "cindy"}]})
    conn.close()
