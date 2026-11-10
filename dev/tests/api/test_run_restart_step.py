"""失败 workflow step run 的“重启进程”必须先恢复 step 状态机。"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload

pytestmark = pytest.mark.usefixtures("client")


async def test_restart_failed_step_run_rebinds_step(client, app, monkeypatch):
    from anvil.db import open_conn
    from anvil.scheduler import Scheduler
    from anvil.services import daemon as daemon_service
    from anvil.services import runs as runs_service
    from anvil.services import workflow as workflow_service
    from anvil.util import now_iso

    monkeypatch.setattr(daemon_service, "get_manager", lambda: None)
    app.state.orchestrator = None

    await client.post(f"{API}/runtimes", json=runtime_payload())
    r = await client.post(f"{API}/roles", json=role_payload(slug="qa-role"))
    assert r.status_code == 201, r.text
    t = await client.post(
        f"{API}/tasks", json={"title": "restart step", "assignee": "qa-role"},
        headers=human_headers())
    assert t.status_code == 201, t.text
    tid = t.json()["id"]

    conn = open_conn(app.state.db_path)
    try:
        wf = workflow_service.create_workflow(
            conn, tid, {"steps": [{"id": "S1", "title": "x", "role": "qa-role"}]},
            enforce_role_coverage=False)
        run = runs_service.create_run(
            conn, "qa-role", tid, step_id="S1", workflow_id=wf["id"])
        now = now_iso()
        conn.execute(
            "UPDATE runs SET status='failed', started_at=?, ended_at=?, "
            "failure_class='agent_exit_1' WHERE id=?", (now, now, run["id"]))
        # fail_step 后的真实形态：step pending、run_id 清空、等待自动恢复。
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, "
            "next_retry_at='2026-12-01T00:00:00+00:00', recovery_count=1 "
            "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
        conn.commit()
        rid, wid = run["id"], wf["id"]
    finally:
        conn.close()

    resp = await client.post(f"{API}/runs/{rid}/restart", headers=human_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "created"
    assert body["failure_class"] is None

    conn = open_conn(app.state.db_path)
    try:
        step = conn.execute(
            "SELECT status, run_id, lease_id, fence_token, next_retry_at, recovery_count "
            "FROM workflow_steps WHERE workflow_id=? AND step_id='S1'", (wid,)).fetchone()
        assert step["status"] == "running"
        assert step["run_id"] == rid
        assert step["lease_id"]
        assert int(step["fence_token"]) >= 1
        assert step["next_retry_at"] is None
        assert step["recovery_count"] == 0
        lease = conn.execute(
            "SELECT status FROM leases WHERE id=?", (step["lease_id"],)).fetchone()
        assert lease["status"] == "active"

        # scheduler 的孤儿清理不能再把这个 run 判死。
        Scheduler(app.state.db_path, None)._reap_orphaned_step_runs(conn)
        run_row = conn.execute(
            "SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        assert run_row["status"] == "created"
    finally:
        conn.close()
