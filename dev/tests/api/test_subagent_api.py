"""运行时 subagent 只读 API：按 run/task/role 查询，Agent 只能读自己。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.util import now_iso
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload

pytestmark = pytest.mark.usefixtures("client")


def _seed(app) -> None:
    conn = sqlite3.connect(app.state.db_path)
    now = now_iso()
    try:
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T1','任务','in_progress',?,?)", (now, now))
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,native_session_id,"
            "slice_key,step_id,started_at) VALUES "
            "('R1','dev','T1','running','sess-1','A','S1',?)", (now,))
        conn.execute(
            "INSERT INTO run_subagents (id,run_id,role_slug,task_id,tool_use_id,"
            "tool_name,subagent_type,status,started_at,updated_at,proc_generation,"
            "native_session_id,parent_tool_use_id,slice_key,step_id) "
            "VALUES ('S1','R1','dev','T1','tu-1','Task','Explore','completed',?,?,2,"
            "'sess-1','ptu-1','A','S1')", (now, now))
        conn.execute(
            "INSERT OR REPLACE INTO sessions (token,role_slug,run_id,task_id,"
            "created_at,expires_at) VALUES "
            "('tok-dev','dev',NULL,NULL,'2026-01-01T00:00:00+00:00',"
            "'2999-01-01T00:00:00+00:00')")
        conn.commit()
    finally:
        conn.close()


async def _seed_roles(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("dev", "other"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def test_subagent_read_endpoints(client, app):
    await _seed_roles(client)
    _seed(app)

    for path in ("/runs/R1/subagents", "/tasks/T1/subagents",
                 "/roles/dev/subagents"):
        r = await client.get(f"{API}{path}", headers=human_headers())
        assert r.status_code == 200, (path, r.text)
        rows = r.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "completed"
        assert row["subagent_type"] == "Explore"
        assert row["proc_generation"] == 2
        assert row["parent_tool_use_id"] == "ptu-1"
        assert row["slice_key"] == "A" and row["step_id"] == "S1"

    filtered = await client.get(
        f"{API}/roles/dev/subagents?status=running", headers=human_headers())
    assert filtered.status_code == 200 and filtered.json() == []

    other = await client.get(
        f"{API}/roles/other/subagents", headers={"X-Anvil-Session": "tok-dev"})
    assert other.status_code == 403
