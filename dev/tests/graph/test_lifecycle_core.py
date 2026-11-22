"""P0 生命周期核心：spawn 重置计时+冻结超时、process_exit 失败分类+suspended 守卫、monitor run 级超时。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from anvil.db import open_conn
from anvil.scheduler import Scheduler
from anvil.services import runs as runs_service

from tests.graph.conftest import make_env


def _mk_run(env, role="dev", task_id=None, status="running", sid="pg:12345",
            started_delta_s=0, timeout_s=None):
    """直插 run（绕过图），用于 process_exit / monitor 的定向场景。"""
    conn = open_conn(env["db_path"])
    started = (datetime.now(timezone.utc) - timedelta(seconds=started_delta_s)).isoformat()
    rid = f"R-{time.time_ns()}"
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at, native_session_id, timeout_seconds) "
        "VALUES (?,?,?,?,?,?,?)",
        (rid, role, task_id, status, started, sid, timeout_s),
    )
    conn.commit()
    conn.close()
    return rid


def _get(env, rid):
    conn = open_conn(env["db_path"])
    r = runs_service.get_run(conn, rid)
    conn.close()
    return r


# ---- Task 3：spawn 重置 started_at 并冻结运行时 YAML 超时 ---------------------

def test_spawn_resets_started_at_and_freezes_timeout(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    run = runs_service.create_run(conn, "dev", task_id=None)
    old = run["started_at"]
    conn.close()
    time.sleep(0.05)
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    r = _get(env, run["id"])
    assert r["status"] == "running"
    assert r["native_session_id"] == "pg:12345"
    assert r["started_at"] > old          # P0-1：spawn/续跑重置计时
    assert r["timeout_seconds"] == 3600   # P0-2：冻结自 claude-code.yaml invocation.timeout_seconds


# ---- v1.47.0 回归：daemon 投递已提升 running，spawn 不得误杀刚拉起的进程 --------

def test_spawn_keeps_run_promoted_to_running_by_delivery(tmp_path, monkeypatch):
    """真实 DaemonManager._send_locked 在写 stdin 前会把 projecting 置为 running；
    spawn_node 随后的 CAS 必然失败，但不能当成并发终止去 clear_run——否则刚投递
    的常驻进程会被立即杀掉，run 永久卡在 running 且无进程。"""
    from anvil.graph import nodes
    from anvil.services import daemon as daemon_service

    env = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(nodes, "_persistent_session",
                        lambda conn, effective: {"ok": True})

    class _Mgr:
        def __init__(self):
            self.cleared = []

        def ensure_and_deliver(self, conn, role, **kw):
            # 与真实 _send_locked 一致：投递时把 created/projecting 提升为 running
            runs_service.set_status(
                conn, kw["run_id"], "running",
                from_statuses=("created", "projecting", "suspended"))
            return {"error": None, "queued": False}

        def clear_run(self, role, run_id):
            self.cleared.append((role, run_id))
            return True

    mgr = _Mgr()
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    conn = open_conn(env["db_path"])
    run = runs_service.create_run(conn, "dev", task_id=None)
    conn.close()

    env["orch"].pump(env["db_path"], run["id"], "wake", {})

    r = _get(env, run["id"])
    assert r["status"] == "running"
    assert r["native_session_id"] == "rs:dev"   # 会话绑定保留
    assert mgr.cleared == []                    # 进程未被误杀


def test_spawn_cleans_run_terminated_during_delivery(tmp_path, monkeypatch):
    """投递后若并发终止已把 run 置为终态，spawn 仍须清理会话，不能复活终态。"""
    from anvil.graph import nodes
    from anvil.services import daemon as daemon_service

    env = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(nodes, "_persistent_session",
                        lambda conn, effective: {"ok": True})

    class _Mgr:
        def __init__(self):
            self.cleared = []

        def ensure_and_deliver(self, conn, role, **kw):
            runs_service.set_status(
                conn, kw["run_id"], "failed", ended_at="2026-01-01T00:00:00+00:00")
            return {"error": None, "queued": False}

        def clear_run(self, role, run_id):
            self.cleared.append((role, run_id))
            return True

    mgr = _Mgr()
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    conn = open_conn(env["db_path"])
    run = runs_service.create_run(conn, "dev", task_id=None)
    conn.close()

    env["orch"].pump(env["db_path"], run["id"], "wake", {})

    r = _get(env, run["id"])
    assert r["status"] == "failed"
    assert not r["native_session_id"]
    assert mgr.cleared == [("dev", run["id"])]


# ---- Task 4：process_exit 失败分类 + suspended 守卫 ---------------------------

def test_process_exit_classifies_agent_exit(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    rid = _mk_run(env, status="running")
    env["orch"].pump(env["db_path"], rid, "process_exit", {"exit_code": 2})
    r = _get(env, rid)
    assert r["status"] == "failed"
    assert r["failure_class"] == "agent_exit_2"


def test_process_exit_platform_timeout_passthrough(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    rid = _mk_run(env, status="running")
    env["orch"].pump(env["db_path"], rid, "process_exit",
                     {"exit_code": 1, "failure_class": "platform_timeout"})
    r = _get(env, rid)
    assert r["status"] == "failed"
    assert r["failure_class"] == "platform_timeout"


def test_suspended_not_overwritten_by_process_exit(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    rid = _mk_run(env, status="suspended")
    env["orch"].pump(env["db_path"], rid, "process_exit", {"exit_code": 1})
    assert _get(env, rid)["status"] == "suspended"  # 守卫：suspended 不被 process_exit 覆盖


def test_stale_process_exit_after_restart_is_ignored(tmp_path, monkeypatch):
    """重启后重新 spawn 会重置 started_at；基于旧 started_at 的 process_exit 必须让路。"""
    env = make_env(tmp_path, monkeypatch)
    rid = _mk_run(env, status="running", sid="rs:dev", started_delta_s=600)
    old_started = _get(env, rid)["started_at"]

    conn = open_conn(env["db_path"])
    runs_service.mark_spawned(conn, rid, "rs:dev")  # 重启后重新拉起，计时重置
    conn.close()

    env["orch"].pump(env["db_path"], rid, "process_exit",
                     {"exit_code": 1, "failure_class": "agent_exit_process",
                      "observed_started_at": old_started})
    r = _get(env, rid)
    assert r["status"] == "running"
    assert r["failure_class"] is None


# ---- P0 补丁：@mention 唤醒（resume）不得泄漏旧进程 -------------------------

def test_resume_terminates_old_process(tmp_path, monkeypatch):
    """实测 bug：同一角色被反复 @ 唤醒时，旧 claude 进程无人终止 → 叠出 3-5 个进程。"""
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    run = runs_service.create_run(conn, "dev", task_id=None)
    conn.close()
    env["orch"].pump(env["db_path"], run["id"], "wake", {})   # 第一次 spawn
    assert len(env["fake"].spawned) == 1
    env["fake"].alive = True                                   # 旧进程仍在运行
    env["orch"].pump(env["db_path"], run["id"], "resume", {})  # @mention 唤醒
    assert "pg:12345" in env["fake"].terminated                # 旧进程被终止（防泄漏）
    assert len(env["fake"].spawned) == 2                       # 新进程重新拉起
    assert _get(env, run["id"])["status"] == "running"


# ---- Task 5：monitor 用 run 级超时（YAML 冻结值优先，RUN_TIMEOUT 兜底）--------

def test_monitor_uses_run_level_timeout(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    # 场景1：超过旧 600s 默认但 run 级 3600s 未到 → 不杀（P0-1 实测误杀场景）
    rid1 = _mk_run(env, started_delta_s=700, timeout_s=3600, sid="pg:12345")
    # 场景2：run 级超时已过 → 终止 + platform_timeout 分类
    rid2 = _mk_run(env, started_delta_s=100, timeout_s=50, sid="pg:12345")
    sched = Scheduler(env["db_path"], env["orch"])
    sched.tick_once()
    assert _get(env, rid1)["status"] == "running"      # 不误杀
    r2 = _get(env, rid2)
    assert r2["status"] == "failed"
    assert r2["failure_class"] == "platform_timeout"
    assert "pg:12345" in env["fake"].terminated
