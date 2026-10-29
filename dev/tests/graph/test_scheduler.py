"""后台调度器测试：派发唤醒→spawn、monitor→process_exit、lease 回收。"""
from __future__ import annotations

from anvil import config
from anvil.db import open_conn
from anvil.scheduler import Scheduler
from anvil.services import daemon as daemon_service
from anvil.util import now_iso

from tests.graph.conftest import make_env


def _seed_todo(env, task_id="T-1"):
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, "t", "todo", "[]", "[]", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


def _runs(env):
    conn = open_conn(env["db_path"])
    rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_tick_dispatches_and_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTO_DISPATCH", True)  # 显式开启自动派发
    env = make_env(tmp_path, monkeypatch)
    _seed_todo(env, "T-D")
    sched = Scheduler(env["db_path"], env["orch"])
    sched.tick_once()
    assert env["fake"].spawned, "应拉起子进程"
    runs = _runs(env)
    assert any(r["task_id"] == "T-D" and r["status"] == "running" for r in runs)


def test_advance_skips_closed_task(tmp_path, monkeypatch):
    """任务 closed 后不再为 running workflow 创建新 run。"""
    from anvil.services import workflow as wf_service

    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-CLOSED','t','todo','[]','[]','2026-01-01T00:00:00','2026-01-01T00:00:00')")
    conn.commit()
    wf = wf_service.create_workflow(conn, "T-CLOSED", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute("UPDATE tasks SET status='closed' WHERE id='T-CLOSED'")
    conn.commit()
    conn.close()

    Scheduler(env["db_path"], env["orch"]).tick_once()

    runs = _runs(env)
    assert not any(r["task_id"] == "T-CLOSED" for r in runs)
    conn = open_conn(env["db_path"])
    step = conn.execute(
        "SELECT status FROM workflow_steps WHERE workflow_id=? AND step_id='S1'",
        (wf["id"],)).fetchone()
    conn.close()
    assert step["status"] == "pending"


def test_monitor_detects_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTO_DISPATCH", True)  # 显式开启自动派发
    env = make_env(tmp_path, monkeypatch)
    _seed_todo(env, "T-M")
    sched = Scheduler(env["db_path"], env["orch"])
    sched.tick_once()  # spawn
    env["fake"].alive = False
    env["fake"].exit_code = 1
    sched.tick_once()  # monitor → process_exit
    runs = _runs(env)
    assert any(r["task_id"] == "T-M" and r["status"] == "failed" for r in runs)


def test_lease_reclaim_abandons_run(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, lease_expires_at, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-L', 't', 'in_progress', 'dev', '2020-01-01T00:00:00', '[]', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) VALUES ('R-L', 'dev', 'T-L', 'running', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    sched = Scheduler(env["db_path"], env["orch"])
    sched.tick_once()  # governance.tick 回收过期租约
    conn = open_conn(env["db_path"])
    t = conn.execute("SELECT status FROM tasks WHERE id='T-L'").fetchone()
    r = conn.execute("SELECT status FROM runs WHERE id='R-L'").fetchone()
    conn.close()
    assert t["status"] == "todo"
    assert r["status"] == "abandoned"


def test_reconcile_only_submits_owner_run(tmp_path, monkeypatch):
    """任务 in_review 时，只能 pump 负责人 assignee 的 run，不能误推协作角色的 run。"""
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-OWN', 't', 'in_review', 'dev', '[]', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
        "VALUES ('R-OWN', 'dev', 'T-OWN', 'running', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
        "VALUES ('R-OTHER', 'pm', 'T-OWN', 'running', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    Scheduler(env["db_path"], env["orch"]).tick_once()

    conn = open_conn(env["db_path"])
    own = conn.execute("SELECT status FROM runs WHERE id='R-OWN'").fetchone()["status"]
    other = conn.execute("SELECT status FROM runs WHERE id='R-OTHER'").fetchone()["status"]
    conn.close()
    assert own in ("submitted", "reviewing")
    assert other == "running"


def test_reap_dead_persistent_run(tmp_path, monkeypatch):
    """常驻进程已死但 run 仍 running：超过宽限期强制转 suspended，避免永久卡住。"""
    from anvil.services import daemon as daemon_service

    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-Z', 't', 'in_progress', '[]', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-Z', 'dev', 'T-Z', 'running', 'rs:dev', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO role_sessions (role_slug, session_id, status, updated_at) "
        "VALUES ('dev', 'sid', 'dead', '2020-01-01T00:00:00+00:00')")
    conn.commit()

    class _Mgr:
        enabled = True
        def __init__(self):
            self.fresh = []
        @staticmethod
        def status(role):
            return {"alive": False}
        def mark_needs_fresh_context(self, role, note="", task_id=None):
            self.fresh.append(role)

    mgr = _Mgr()
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    monkeypatch.setattr(config, "RUN_DEAD_GRACE_SECONDS", 60)
    Scheduler(env["db_path"], env["orch"])._reap_dead_runs(conn)
    r = conn.execute("SELECT status FROM runs WHERE id='R-Z'").fetchone()
    conn.close()
    assert r["status"] == "suspended"
    assert mgr.fresh == ["dev"]  # 下次唤醒用全新 CIC 会话


def test_reap_keeps_live_persistent_run(tmp_path, monkeypatch):
    from anvil.services import daemon as daemon_service

    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-LIVE', 't', 'in_progress', '[]', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-LIVE', 'dev', 'T-LIVE', 'running', 'rs:dev', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO role_sessions (role_slug, session_id, status, updated_at) "
        "VALUES ('dev', 'sid', 'busy', '2020-01-01T00:00:00+00:00')")
    conn.commit()

    class _Mgr:
        enabled = True
        def __init__(self):
            self.fresh = []
        @staticmethod
        def status(role):
            return {"alive": True, "status": "busy", "current_run_id": "R-LIVE",
                    "queue_len": 0}
        def mark_needs_fresh_context(self, role, note="", task_id=None):
            self.fresh.append(role)

    mgr = _Mgr()
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    Scheduler(env["db_path"], env["orch"])._reap_dead_runs(conn)
    r = conn.execute("SELECT status FROM runs WHERE id='R-LIVE'").fetchone()
    conn.close()
    assert r["status"] == "running"
    assert mgr.fresh == []


def test_reap_suspends_run_not_currently_processed(tmp_path, monkeypatch):
    """角色进程活着但空闲、队列为空：这个 running run 已无人处理，也应回收。"""
    from anvil.services import daemon as daemon_service

    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-OLD', 't', 'in_progress', '[]', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-OLD', 'dev', 'T-OLD', 'running', 'rs:dev', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO role_sessions (role_slug, session_id, status, updated_at) "
        "VALUES ('dev', 'sid', 'idle', '2020-01-01T00:00:00+00:00')")
    conn.commit()

    class _Mgr:
        enabled = True
        def __init__(self):
            self.fresh = []
        @staticmethod
        def status(role):
            return {"alive": True, "status": "idle", "current_run_id": None,
                    "queue_len": 0}
        def mark_needs_fresh_context(self, role, note="", task_id=None):
            self.fresh.append(role)

    mgr = _Mgr()
    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    Scheduler(env["db_path"], env["orch"])._reap_dead_runs(conn)
    r = conn.execute("SELECT status FROM runs WHERE id='R-OLD'").fetchone()
    conn.close()
    assert r["status"] == "suspended"
    assert mgr.fresh == []  # 进程还活着，不要丢弃健康上下文


def test_reap_orphaned_step_run(tmp_path, monkeypatch):
    """步骤已重排但旧 run 仍活跃 → 结束孤儿 run，解除对同角色的派发阻塞。"""
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    old = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id,title,status,scope_paths_json,acceptance_json,created_at,updated_at) "
        "VALUES ('T-O','t','in_progress','[]','[]',?,?)", (old, old))
    conn.execute(
        "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
        "VALUES ('W-O','T-O','{}','running',?,?)", (old, old))
    conn.execute(
        "INSERT INTO workflow_steps (workflow_id,step_id,title,role_slug,status,run_id,updated_at) "
        "VALUES ('W-O','S1','待重排','dev','pending',NULL,?)", (old,))
    conn.execute(
        "INSERT INTO workflow_steps (workflow_id,step_id,title,role_slug,status,run_id,updated_at) "
        "VALUES ('W-O','S2','执行中','dev','running','R-A',?)", (old,))
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,started_at,step_id,workflow_id) "
        "VALUES ('R-O','dev','T-O','running',?,'S1','W-O')", (old,))
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,started_at,step_id,workflow_id) "
        "VALUES ('R-A','dev','T-O','running',?,'S2','W-O')", (old,))
    conn.commit()

    Scheduler(env["db_path"], env["orch"])._reap_orphaned_step_runs(conn)

    orphan = conn.execute("SELECT status, ended_at FROM runs WHERE id='R-O'").fetchone()
    active = conn.execute("SELECT status FROM runs WHERE id='R-A'").fetchone()
    assert orphan["status"] == "failed" and orphan["ended_at"]
    assert active["status"] == "running"
    conn.close()


def test_reap_stalled_projecting_run(tmp_path, monkeypatch):
    """projecting 卡住（编排器崩溃窗口）：无会话重投 wake，有会话修正为 running。"""
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-P', 't', 'in_progress', '[]', '[]', '2020-01-01T00:00:00', '2020-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-P', 'dev', 'T-P', 'projecting', NULL, '2020-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-Q', 'dev', 'T-P', 'projecting', 'pg:1', '2020-01-01T00:00:00')")
    conn.commit()

    class _Orch:
        def __init__(self):
            self.pumped = []

        def pump(self, db_path, run_id, event, payload):
            self.pumped.append((run_id, event))

    orch = _Orch()
    Scheduler(env["db_path"], orch)._reap_stalled_phases(conn)
    assert orch.pumped == [("R-P", "wake")]
    q = conn.execute("SELECT status FROM runs WHERE id='R-Q'").fetchone()
    conn.close()
    assert q["status"] == "running"


def test_run_timeout_failed(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
        "VALUES ('T-TO', 't', 'todo', '[]', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    # run 已 running 且 started_at 远古（超过 RUN_TIMEOUT_SECONDS）
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-TO', 'dev', 'T-TO', 'running', 'pg:999', '2020-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    sched = Scheduler(env["db_path"], env["orch"])
    sched.tick_once()  # monitor 超时回收 → terminate + process_exit(1) → failed
    conn = open_conn(env["db_path"])
    r = conn.execute("SELECT status FROM runs WHERE id='R-TO'").fetchone()
    conn.close()
    assert r["status"] == "failed"
    assert env["fake"].terminated  # 超时 run 被 terminate

def test_monitor_grace_for_persistent_startup(tmp_path, monkeypatch):
    """常驻进程刚拉起、daemon 尚未登记时，monitor 不能立刻把 run 判死。"""
    env = make_env(tmp_path, monkeypatch)

    class FakeMgr:
        enabled = True

        def status(self, role):
            return {"alive": False, "switching": False, "status": "dead"}

    monkeypatch.setattr(daemon_service, "get_manager", lambda: FakeMgr())
    conn = open_conn(env["db_path"])
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, native_session_id, started_at) "
        "VALUES ('R-P','dev',NULL,'running','rs:dev',?)", (now_iso(),))
    conn.commit()
    conn.close()

    sched = Scheduler(env["db_path"], env["orch"])
    sched.tick_once()
    assert next(r for r in _runs(env) if r["id"] == "R-P")["status"] == "running"

    # 超过宽限期仍 dead → 才判 agent_exit_process
    conn = open_conn(env["db_path"])
    conn.execute("UPDATE runs SET started_at='2020-01-01T00:00:00+00:00' WHERE id='R-P'")
    conn.commit()
    conn.close()
    sched.tick_once()
    row = next(r for r in _runs(env) if r["id"] == "R-P")
    assert row["status"] == "failed"
    assert row["failure_class"] == "agent_exit_process"


def test_tick_isolates_phase_failures(tmp_path, monkeypatch):
    """单个阶段异常不能中止整轮巡检（其他阶段仍要执行）。"""
    env = make_env(tmp_path, monkeypatch)
    sched = Scheduler(env["db_path"], env["orch"])
    called: list[str] = []

    def boom(_conn):
        raise RuntimeError("phase boom")

    monkeypatch.setattr(sched, "_advance_workflows", boom)
    monkeypatch.setattr(sched, "_pump_created", lambda conn: called.append("pump"))
    monkeypatch.setattr(sched, "_monitor", lambda conn: called.append("monitor"))
    monkeypatch.setattr(sched, "_reap_dead_runs", lambda conn: called.append("reap"))
    sched.tick_once()
    assert called == ["pump", "monitor", "reap"]
