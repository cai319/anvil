"""等待状态验证：5 秒调度轮次不能打断“等其它角色/等模型/等 human”的会话。

覆盖场景：
1. 等其它角色且对方仍在运行 → 不唤醒、不回收；
2. 等其它角色且对方空闲 → 退避到期后唤醒等待方；
3. 等 human → 永不自动唤醒；
4. 等模型（进程 busy）→ tick 不动作，租约自动续租；
5. 等模型超过 run 超时 → 重置会话 + process_exit（进入自动恢复，不 @human）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil import config
from anvil.db import open_conn
from anvil.scheduler import Scheduler
from anvil.services import daemon as daemon_service
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

from tests.graph.conftest import make_env

pytest.importorskip("anvil.graph")

OLD = "2020-01-01T00:00:00+00:00"


class _Orch:
    def __init__(self):
        self.pumped = []

    def pump(self, db_path, run_id, event, payload=None):
        self.pumped.append((run_id, event, payload or {}))

    def resume_review(self, db_path, run_id, approve):
        return False


def _seed_suspended_workflow(env, task_id="T-W"):
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (task_id, "t", "in_progress", "[]", "[]", OLD, OLD))
    conn.execute(
        "INSERT INTO workflows (id, task_id, definition_json, status, created_at, updated_at) "
        "VALUES ('W-W',?,'{}','running',?,?)", (task_id, OLD, OLD))
    conn.execute(
        "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, status, "
        "run_id, started_at, updated_at) VALUES ('W-W','S1','x','dev','running','R-W',?,?)",
        (OLD, OLD))
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at, timeout_seconds) "
        "VALUES ('R-W','dev',?,'suspended',?,3600)", (task_id, OLD))
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, run_id, kind, payload_json, created_at) "
        "VALUES (?,'dev','R-W','run','{\"status\":\"suspended\"}',?)", (task_id, OLD))
    conn.commit()
    conn.close()


def _no_reminders(monkeypatch):
    monkeypatch.setattr("anvil.services.reminders.fire_due_reminders",
                        lambda conn: 0)


def test_tick_does_not_wake_role_waiting_for_busy_peer(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    _no_reminders(monkeypatch)
    monkeypatch.setattr(config, "STEP_NUDGE_SECONDS", 60)
    _seed_suspended_workflow(env, "T-BUSY-PEER")
    conn = open_conn(env["db_path"])
    tasks_service.mention(conn, "T-BUSY-PEER", "dev", "tester", "请复核 S1")
    # 对方仍有活跃 run（正在处理），等待方不应被打断
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
        "VALUES ('R-T','tester','T-BUSY-PEER','running',?)", (OLD,))
    conn.commit()
    conn.close()

    orch = _Orch()
    Scheduler(env["db_path"], orch).tick_once()
    assert orch.pumped == []
    conn = open_conn(env["db_path"])
    assert conn.execute("SELECT status FROM runs WHERE id='R-W'").fetchone()["status"] == "suspended"
    assert conn.execute("SELECT status FROM workflow_steps WHERE step_id='S1'").fetchone()["status"] == "running"
    conn.close()


def test_tick_wakes_waiting_role_when_peer_is_idle(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    _no_reminders(monkeypatch)
    monkeypatch.setattr(config, "STEP_NUDGE_SECONDS", 60)
    _seed_suspended_workflow(env, "T-IDLE-PEER")
    conn = open_conn(env["db_path"])
    tasks_service.mention(conn, "T-IDLE-PEER", "dev", "tester", "请复核 S1")
    conn.commit()
    conn.close()

    orch = _Orch()
    Scheduler(env["db_path"], orch).tick_once()
    assert ("R-W", "resume", {}) in orch.pumped


def test_tick_never_auto_retries_human_wait(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    _no_reminders(monkeypatch)
    monkeypatch.setattr(config, "STEP_NUDGE_SECONDS", 60)
    _seed_suspended_workflow(env, "T-HUMAN-WAIT")
    conn = open_conn(env["db_path"])
    tasks_service.mention(conn, "T-HUMAN-WAIT", "dev", "human", "请确认口径")
    conn.commit()
    conn.close()

    orch = _Orch()
    Scheduler(env["db_path"], orch).tick_once()
    assert orch.pumped == []


def _seed_busy_role(env, task_id="T-MODEL", busy_old=False, timeout=3600):
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, lease_expires_at, "
        "scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "t", "in_progress", "dev", OLD, "[]", "[]", OLD, OLD))
    conn.execute(
        "INSERT INTO task_role_claims (task_id, role_slug, status, lease_expires_at, updated_at) "
        "VALUES (?,?,?,?,?)", (task_id, "dev", "working", OLD, OLD))
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, "
        "started_at, timeout_seconds) VALUES ('R-M','dev',?,'running','rs:dev',?,?)",
        (task_id, OLD, timeout))
    conn.commit()
    conn.close()
    busy_since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat() \
        if busy_old else now_iso()
    return busy_since


class _Mgr:
    enabled = True

    def __init__(self, busy_since, task_id=None):
        self.busy_since = busy_since
        self.task_id = task_id
        self.reset_calls = []

    def status(self, role):
        return {"alive": True, "status": "busy", "busy_since": self.busy_since,
                "current_run_id": "R-M", "current_task_id": self.task_id, "queue_len": 0}

    def reset_session(self, role, cic=False, task_id=None):
        self.reset_calls.append((role, cic))

    def mark_needs_fresh_context(self, role, note="", task_id=None):
        raise AssertionError("活跃进程不应被标记为 fresh context")


def test_tick_leaves_busy_model_call_alone_and_renews_lease(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    _no_reminders(monkeypatch)
    busy_since = _seed_busy_role(env, "T-MODEL-BUSY")
    mgr = _Mgr(busy_since, task_id="T-MODEL-BUSY")
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)

    orch = _Orch()
    Scheduler(env["db_path"], orch).tick_once()
    assert orch.pumped == []
    assert mgr.reset_calls == []
    conn = open_conn(env["db_path"])
    assert conn.execute("SELECT status FROM runs WHERE id='R-M'").fetchone()["status"] == "running"
    task = conn.execute("SELECT status, lease_expires_at FROM tasks WHERE id='T-MODEL-BUSY'").fetchone()
    claim = conn.execute(
        "SELECT status, lease_expires_at FROM task_role_claims "
        "WHERE task_id='T-MODEL-BUSY' AND role_slug='dev'").fetchone()
    conn.close()
    assert task["status"] == "in_progress" and task["lease_expires_at"] > OLD
    assert claim["status"] == "working" and claim["lease_expires_at"] > OLD


def test_tick_resets_hung_model_call_after_timeout(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    _no_reminders(monkeypatch)
    busy_since = _seed_busy_role(env, "T-MODEL-HUNG", busy_old=True, timeout=60)
    mgr = _Mgr(busy_since, task_id="T-MODEL-HUNG")
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)

    orch = _Orch()
    Scheduler(env["db_path"], orch).tick_once()
    assert mgr.reset_calls == [("dev", True)]  # 新会话 + CIC
    assert ("R-M", "process_exit", {"exit_code": 1,
                                    "failure_class": "platform_timeout"}) in orch.pumped
