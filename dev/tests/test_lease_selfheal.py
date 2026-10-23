"""租约自愈：等待模型/等待角色/HITL 等待时续租，不把活跃会话判成失联。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import governance

pytest.importorskip("anvil.graph")

OLD = "2020-01-01T00:00:00+00:00"


def _seed(conn, task_id="T-W", run_status="suspended", sid=None):
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, lease_expires_at, "
        "scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "t", "in_progress", "dev", OLD, "[]", "[]", OLD, OLD))
    conn.execute(
        "INSERT INTO task_role_claims "
        "(task_id, role_slug, status, lease_expires_at, updated_at) "
        "VALUES (?,?,?,?,?)", (task_id, "dev", "working", OLD, OLD))
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES (?,?,?,?,?,?)", (f"R-{task_id}", "dev", task_id, run_status, sid, OLD))
    conn.commit()


def test_lease_renewed_for_suspended_run(tmp_path):
    """角色等待其它角色/HITL（run=suspended）时，租约到期应续租而非回收。"""
    conn = connect(str(tmp_path / "g.db"))
    _seed(conn, run_status="suspended", sid=None)
    governance.tick(conn, dispatch=False)
    task = conn.execute("SELECT status, lease_expires_at FROM tasks WHERE id='T-W'").fetchone()
    claim = conn.execute(
        "SELECT status, lease_expires_at FROM task_role_claims "
        "WHERE task_id='T-W' AND role_slug='dev'").fetchone()
    run = conn.execute("SELECT status FROM runs WHERE id='R-T-W'").fetchone()
    conn.close()
    assert task["status"] == "in_progress"
    assert task["lease_expires_at"] > OLD
    assert claim["status"] == "working"
    assert claim["lease_expires_at"] > OLD
    assert run["status"] == "suspended"


def test_lease_renewed_for_live_persistent_run(tmp_path, monkeypatch):
    """角色正在等模型响应（进程 alive）时，即使长时间没 heartbeat 也不回收。"""
    class _Mgr:
        enabled = True
        @staticmethod
        def status(role):
            return {"alive": True, "status": "busy", "current_run_id": "R-T-W",
                    "queue_len": 0}

    monkeypatch.setattr("anvil.services.daemon.get_manager", lambda: _Mgr())
    conn = connect(str(tmp_path / "g.db"))
    _seed(conn, run_status="running", sid="rs:dev")
    governance.tick(conn, dispatch=False)
    task = conn.execute("SELECT status FROM tasks WHERE id='T-W'").fetchone()
    claim = conn.execute(
        "SELECT status FROM task_role_claims WHERE task_id='T-W' AND role_slug='dev'"
    ).fetchone()
    run = conn.execute("SELECT status FROM runs WHERE id='R-T-W'").fetchone()
    conn.close()
    assert task["status"] == "in_progress"
    assert claim["status"] == "working"
    assert run["status"] == "running"


def test_lease_reclaimed_for_dead_run(tmp_path):
    """没有存活会话的 running run 仍按原逻辑回收（防止真正失联的 claim 永久占用）。"""
    conn = connect(str(tmp_path / "g.db"))
    _seed(conn, run_status="running", sid=None)
    governance.tick(conn, dispatch=False)
    task = conn.execute("SELECT status FROM tasks WHERE id='T-W'").fetchone()
    claim = conn.execute(
        "SELECT status FROM task_role_claims WHERE task_id='T-W' AND role_slug='dev'"
    ).fetchone()
    conn.close()
    assert task["status"] == "todo"
    assert claim["status"] == "released"
