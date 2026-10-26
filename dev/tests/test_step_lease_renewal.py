"""步骤 lease 续租：run 还活着时，长步骤不应因 lease TTL 到期被重置。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from anvil.db import open_conn
from anvil.scheduler import Scheduler
from anvil.services import workflow as wf_service
from anvil.util import now_iso

from tests.graph.conftest import make_env

pytest.importorskip("anvil.graph")


def _running_step(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T-LEASE','t','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T-LEASE", {"steps": [
        {"id": "S1", "title": "长步骤", "role": "dev"}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "R-LEASE"})
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,started_at,step_id,workflow_id) "
        "VALUES ('R-LEASE','dev','T-LEASE','running',?,'S1',?)", (now, wf["id"]))
    conn.commit()
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"]
                if s["step_id"] == "S1")
    return env, conn, wf["id"], step


def test_live_step_lease_is_renewed_before_reap(tmp_path, monkeypatch):
    """lease 已到过期时刻但 run 仍 running：先续租，不能重置步骤。"""
    env, conn, wid, step = _running_step(tmp_path, monkeypatch)
    past = "2000-01-01T00:00:00+00:00"
    conn.execute("UPDATE leases SET expires_at=? WHERE id=?", (past, step["lease_id"]))
    conn.commit()

    sched = Scheduler(env["db_path"], env["orch"])
    sched._renew_step_leases(conn)

    lease = conn.execute("SELECT status, expires_at FROM leases WHERE id=?",
                         (step["lease_id"],)).fetchone()
    assert lease["status"] == "active"
    assert lease["expires_at"] > datetime.now(timezone.utc).isoformat()

    # 同轮 advance 不应把步骤打回 pending。
    sched._advance_workflows(conn)
    row = conn.execute(
        "SELECT status, run_id FROM workflow_steps WHERE workflow_id=? AND step_id='S1'",
        (wid,)).fetchone()
    assert row["status"] == "running"
    assert row["run_id"] == "R-LEASE"
    conn.close()


def test_heartbeat_renews_step_lease_for_matching_run(tmp_path, monkeypatch):
    """agent 的 task heartbeat 也续租当前步骤 lease。"""
    env, conn, wid, step = _running_step(tmp_path, monkeypatch)
    conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                 (step["lease_id"],))
    conn.commit()

    assert wf_service.renew_step_lease_for_run(conn, "R-LEASE") is True
    lease = conn.execute("SELECT expires_at FROM leases WHERE id=?",
                         (step["lease_id"],)).fetchone()
    assert lease["expires_at"] > datetime.now(timezone.utc).isoformat()

    conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                 (step["lease_id"],))
    conn.commit()
    assert wf_service.renew_step_lease_for_run(conn, "R-OTHER") is False
    lease = conn.execute("SELECT expires_at FROM leases WHERE id=?",
                         (step["lease_id"],)).fetchone()
    assert lease["expires_at"] == "2000-01-01T00:00:00+00:00"
    conn.close()


def test_failed_run_is_not_renewed(tmp_path, monkeypatch):
    """run 已失败时不再续租；步骤进入自动恢复队列。"""
    env, conn, wid, step = _running_step(tmp_path, monkeypatch)
    conn.execute("UPDATE runs SET status='failed', ended_at=? WHERE id='R-LEASE'",
                 (now_iso(),))
    conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                 (step["lease_id"],))
    conn.commit()

    sched = Scheduler(env["db_path"], env["orch"])
    sched._advance_workflows(conn)

    row = conn.execute(
        "SELECT status FROM workflow_steps WHERE workflow_id=? AND step_id='S1'",
        (wid,)).fetchone()
    assert row["status"] == "pending"
    lease = conn.execute("SELECT status FROM leases WHERE id=?",
                         (step["lease_id"],)).fetchone()
    assert lease["status"] != "active"
    conn.close()
