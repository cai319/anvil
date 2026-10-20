"""停滞看门狗：挂起步骤超时唤醒；超过上限判失败。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil import config
from anvil.db import connect
from anvil.scheduler import Scheduler
from anvil.services import workflow as wf_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


class _Orch:
    def __init__(self):
        self.pumped = []

    def pump(self, db_path, run_id, event, payload):
        self.pumped.append((run_id, event))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STEP_NUDGE_SECONDS", 60)
    monkeypatch.setattr(config, "STEP_MAX_NUDGES", 3)
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','t','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute("INSERT INTO runs (id, role_slug, task_id, status, started_at) "
                 "VALUES ('R1','dev','T1','suspended',?)", (old,))
    conn.execute("UPDATE workflow_steps SET status='running', run_id='R1', started_at=? "
                 "WHERE workflow_id=? AND step_id='S1'", (old, wf["id"]))
    conn.execute("INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                 "VALUES ('T1','dev','comment','{}',?)", (old,))
    conn.commit()
    orch = _Orch()
    return conn, Scheduler(db_path, orch), orch, wf["id"]


def test_nudge_suspended_step(env):
    conn, sched, orch, wid = env
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]
    row = conn.execute("SELECT nudge_count FROM workflow_steps WHERE workflow_id=?",
                       (wid,)).fetchone()
    assert row["nudge_count"] == 1


def test_nudge_limit_fails_step(env):
    conn, sched, orch, wid = env
    conn.execute("UPDATE workflow_steps SET nudge_count=3 WHERE workflow_id=?", (wid,))
    conn.commit()
    sched._nudge_stalled(conn)
    row = conn.execute("SELECT status, failure_reason FROM workflow_steps WHERE workflow_id=?",
                       (wid,)).fetchone()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "environment"
