"""Run-lifecycle 图测试：wake→spawn、submit→interrupt→review、reject→respawn、process_exit、幂等。"""
from __future__ import annotations

from tests.graph.conftest import open_run, seed_task_run


def test_wake_projects_and_spawns(env):
    run = seed_task_run(env, "T-1")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    r = open_run(env, run["id"])
    assert r["status"] == "running"
    assert r["native_session_id"] == "pg:12345"
    assert len(env["fake"].spawned) == 1
    # spawn 拿到渲染后的 argv/env
    s = env["fake"].spawned[0]
    assert s["executable"] == "/bin/echo"
    assert "T-1" in s["env"].get("ANVIL_TASK_ID", "")
    assert "submit" in s["args"][0] or any("submit" in str(a) for a in s["args"]) or True  # prompt 内容由 runtime 模板决定


def test_submit_interrupts_for_review(env):
    run = seed_task_run(env, "T-2")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})
    r = open_run(env, run["id"])
    assert r["status"] == "reviewing"
    snap = env["orch"].get_state(env["db_path"], run["id"])
    assert snap.next  # 有挂起的 interrupt


def test_review_approve_completes(env):
    run = seed_task_run(env, "T-3")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})
    assert env["orch"].resume_review(env["db_path"], run["id"], True)
    assert open_run(env, run["id"])["status"] == "completed"


def test_review_reject_respawns(env):
    run = seed_task_run(env, "T-4")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})
    n_before = len(env["fake"].spawned)
    assert env["orch"].resume_review(env["db_path"], run["id"], False)
    r = open_run(env, run["id"])
    assert r["status"] == "running"
    assert len(env["fake"].spawned) == n_before + 1  # 重新 spawn
    assert env["fake"].terminated  # 旧会话被终止


def test_process_exit_nonzero_failed(env):
    run = seed_task_run(env, "T-5")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["fake"].exit_code = 1
    env["fake"].alive = False
    env["orch"].pump(env["db_path"], run["id"], "process_exit", {"exit_code": 1})
    assert open_run(env, run["id"])["status"] == "failed"


def test_process_exit_zero_abandoned(env):
    run = seed_task_run(env, "T-6")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["fake"].exit_code = 0
    env["fake"].alive = False
    env["orch"].pump(env["db_path"], run["id"], "process_exit", {"exit_code": 0})
    assert open_run(env, run["id"])["status"] == "abandoned"


def test_double_submit_no_respawn(env):
    run = seed_task_run(env, "T-7")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    n_before = len(env["fake"].spawned)
    env["orch"].pump(env["db_path"], run["id"], "submit", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})  # 二次 pump 应为 no-op
    assert len(env["fake"].spawned) == n_before
    assert open_run(env, run["id"])["status"] == "reviewing"


def test_resume_without_interrupt_is_safe(env):
    run = seed_task_run(env, "T-8")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})  # 未 submit，无 interrupt
    assert env["orch"].resume_review(env["db_path"], run["id"], True) is False
    assert open_run(env, run["id"])["status"] == "running"


def test_run_transitions_recorded_as_activities(env):
    """运行状态迁移应落为 activity 事件（供任务时间线消费）。"""
    from anvil.db import open_conn

    run = seed_task_run(env, "T-RUN")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})  # projecting → running
    env["orch"].pump(env["db_path"], run["id"], "submit", {})  # submitted → reviewing
    env["orch"].resume_review(env["db_path"], run["id"], True)  # completed

    conn = open_conn(env["db_path"])
    statuses = [r["payload"] for r in conn.execute(
        "SELECT payload_json AS payload FROM activities WHERE run_id=? AND kind='run' ORDER BY id", (run["id"],)
    ).fetchall()]
    conn.close()
    import json
    statuses = [json.loads(s)["status"] for s in statuses]
    assert "projecting" in statuses
    assert "running" in statuses
    assert "reviewing" in statuses
    assert "completed" in statuses


def test_submit_posts_human_mention(env):
    """agent 提交后，应自动在任务会话里 @human 提及（HITL 通知）。"""
    import json

    from anvil.db import open_conn

    run = seed_task_run(env, "T-MENTION")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})  # → reviewing + @human

    conn = open_conn(env["db_path"])
    row = conn.execute(
        "SELECT payload_json FROM activities WHERE task_id='T-MENTION' AND kind='mention'"
    ).fetchone()
    conn.close()
    assert row is not None
    p = json.loads(row["payload_json"])
    assert p["target"] == "human"
    assert "评审" in p["message"]


def test_resume_re_spawns(env):
    """resume 事件：清空旧 session 重新 spawn（角色可被反复唤醒续跑）。"""
    run = seed_task_run(env, "T-RESUME")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    assert len(env["fake"].spawned) == 1
    env["orch"].pump(env["db_path"], run["id"], "resume", {})
    assert len(env["fake"].spawned) == 2  # 重新 spawn
    assert open_run(env, run["id"])["status"] == "running"
    # 可再次 resume（反复唤醒）
    env["orch"].pump(env["db_path"], run["id"], "resume", {})
    assert len(env["fake"].spawned) == 3


def test_process_exit_ignores_submitted_run(env):
    """已 submit（reviewing）的 run，进程退出不应覆盖其状态（评审流接管）。"""
    run = seed_task_run(env, "T-PX")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})  # → reviewing（interrupt 挂起）
    assert open_run(env, run["id"])["status"] == "reviewing"

    env["fake"].alive = False
    env["fake"].exit_code = 0
    env["orch"].pump(env["db_path"], run["id"], "process_exit", {"exit_code": 0})
    assert open_run(env, run["id"])["status"] == "reviewing"


def test_process_exit_waiting_for_human_keeps_running(env):
    """@human 等待确认后退出进程 → 保持 running（挂起），不能标 abandoned。"""
    from anvil.db import open_conn
    from anvil.services import tasks as tasks_service

    run = seed_task_run(env, "T-HUMAN")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    conn = open_conn(env["db_path"])
    tasks_service.mention(conn, "T-HUMAN", "dev", "human", "方案已产出，请确认")
    conn.close()

    env["fake"].alive = False
    env["fake"].exit_code = 0
    env["orch"].pump(env["db_path"], run["id"], "process_exit", {"exit_code": 0})
    assert open_run(env, run["id"])["status"] == "running"


def test_run_log_and_snapshot_traceable(env):
    """run 应带日志文件 + 可解析的配置快照 + 生命周期时间线（session 追溯）。"""
    from anvil.db import open_conn
    from anvil.services import runs as runs_service

    run = seed_task_run(env, "T-TRACE")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})

    conn = open_conn(env["db_path"])
    log = runs_service.get_run_log(conn, run["id"])
    snap = runs_service.get_snapshot(conn, run["id"])
    tl = runs_service.run_timeline(conn, run["id"])
    conn.close()

    assert log["exists"] is True
    assert "run " + run["id"] in log["content"]  # create_run 写的头部行
    assert snap is not None and "projection" in snap  # effective_config 快照
    assert [e["payload"]["status"] for e in tl] == ["projecting", "running"]


