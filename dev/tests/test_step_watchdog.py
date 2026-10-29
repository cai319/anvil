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


def test_nudge_skips_step_waiting_for_human(env):
    """合法的 HITL 等待不自动重试；human 回复后由 mention 端点唤醒。"""
    from anvil.services import tasks as tasks_service

    conn, sched, orch, wid = env
    tasks_service.mention(conn, "T1", "dev", "human", "请确认审计口径")
    conn.commit()
    sched._nudge_stalled(conn)
    assert orch.pumped == []


def test_nudge_resumes_after_human_replied(env):
    from anvil.services import tasks as tasks_service

    conn, sched, orch, wid = env
    tasks_service.mention(conn, "T1", "dev", "human", "请确认审计口径")
    tasks_service.comment(conn, "T1", "human", "同意，按 strict 口径")
    conn.commit()
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]


def test_nudge_skips_step_waiting_for_busy_role(env):
    """等待另一个角色响应，且对方正在运行 → 不打断等待。"""
    from anvil.services import tasks as tasks_service

    conn, sched, orch, wid = env
    tasks_service.mention(conn, "T1", "dev", "tester", "请复核 S2")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
        "VALUES ('R-T', 'tester', 'T1', 'running', '2026-01-01T00:00:00')")
    conn.commit()
    sched._nudge_stalled(conn)
    assert orch.pumped == []


def test_nudge_resumes_when_target_role_is_idle(env):
    """对方不在运行（已空闲/失联）→ 等待方仍会被唤醒检查，避免永久等不到回复。"""
    from anvil.services import tasks as tasks_service

    conn, sched, orch, wid = env
    tasks_service.mention(conn, "T1", "dev", "tester", "请复核 S2")
    conn.commit()
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]


def test_nudge_reset_releases_step_lease(env):
    """run 已结束但步骤仍 running：重置 pending 时必须释放旧 lease。"""
    from anvil.services import leases

    conn, sched, orch, wid = env
    lease = leases.acquire(conn, "step", f"{wid}:S1", holder="dev")
    conn.execute(
        "UPDATE workflow_steps SET lease_id=?, fence_token=? WHERE workflow_id=?",
        (lease["id"], lease["fence"], wid))
    conn.execute("UPDATE runs SET status='completed' WHERE id='R1'")
    conn.commit()

    sched._nudge_stalled(conn)

    row = conn.execute(
        "SELECT status, lease_id FROM workflow_steps WHERE workflow_id=?", (wid,)).fetchone()
    assert row["status"] == "pending"
    assert row["lease_id"] is None
    lease_row = conn.execute("SELECT status FROM leases WHERE id=?",
                             (lease["id"],)).fetchone()
    assert lease_row["status"] == "released"


def test_nudge_limit_requeues_for_auto_recovery(env):
    """唤醒次数用完不 @human，而是进入自动恢复队列（指数退避后重跑）。"""
    conn, sched, orch, wid = env
    conn.execute("UPDATE workflow_steps SET nudge_count=3 WHERE workflow_id=?", (wid,))
    conn.commit()
    sched._nudge_stalled(conn)
    row = conn.execute(
        "SELECT status, failure_reason, recovery_count, next_retry_at "
        "FROM workflow_steps WHERE workflow_id=?", (wid,)).fetchone()
    assert row["status"] == "pending"
    assert row["failure_reason"] == "environment"
    assert row["recovery_count"] == 1
    assert row["next_retry_at"]
    assert conn.execute(
        "SELECT 1 FROM activities WHERE task_id='T1' AND kind='mention'").fetchone() is None


def test_nudge_limit_exhausted_fails_without_human(env, monkeypatch):
    monkeypatch.setattr(config, "AUTO_RECOVER_MAX_ATTEMPTS", 1)
    conn, sched, orch, wid = env
    conn.execute(
        "UPDATE workflow_steps SET nudge_count=3, recovery_count=1 "
        "WHERE workflow_id=?", (wid,))
    conn.commit()
    sched._nudge_stalled(conn)
    row = conn.execute("SELECT status, failure_reason FROM workflow_steps WHERE workflow_id=?",
                       (wid,)).fetchone()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "environment"
    assert conn.execute(
        "SELECT 1 FROM activities WHERE task_id='T1' AND kind='mention'").fetchone() is None
