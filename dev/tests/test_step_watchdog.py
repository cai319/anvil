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


def _add_context_return(conn, *, age_seconds: int = 0) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at, "
        "context_return_context_key, context_return_run_id, "
        "context_return_after_turn, context_return_last_attempt_at) "
        "VALUES ('R-TMP','qa','T1','suspended',?,'T1::slice::x','R1',1,?)",
        (stamp, stamp))
    conn.commit()


def test_nudge_suspended_step(env):
    conn, sched, orch, wid = env
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]
    row = conn.execute("SELECT nudge_count FROM workflow_steps WHERE workflow_id=?",
                       (wid,)).fetchone()
    assert row["nudge_count"] == 1


def test_nudge_skips_live_context_return(env):
    """新 arm 的回切标记仍应阻塞看门狗。"""
    conn, sched, orch, wid = env
    _add_context_return(conn)
    sched._nudge_stalled(conn)
    assert orch.pumped == []
    row = conn.execute("SELECT nudge_count FROM workflow_steps WHERE workflow_id=?",
                       (wid,)).fetchone()
    assert row["nudge_count"] == 0


def test_nudge_resumes_after_context_return_expires(env):
    """标记超过 TTL 后不再阻塞，nudge_count 必须能递增。"""
    conn, sched, orch, wid = env
    _add_context_return(
        conn, age_seconds=int(config.CONTEXT_RETURN_MAX_WAIT_SECONDS) + 60)
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]
    row = conn.execute("SELECT nudge_count FROM workflow_steps WHERE workflow_id=?",
                       (wid,)).fetchone()
    assert row["nudge_count"] == 1
    marker = conn.execute(
        "SELECT context_return_after_turn, context_returned_at FROM runs "
        "WHERE id='R-TMP'").fetchone()
    assert marker["context_return_after_turn"] == 0
    assert marker["context_returned_at"]


def test_reap_orphaned_suspended_step_run_clears_inbound(env):
    """step 已重排但 run 仍 suspended：回收孤儿 run 并清掉指向它的回切标记。"""
    conn, sched, orch, wid = env
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,workflow_id,step_id,started_at) "
        "VALUES ('R-ORPH','dev','T1','suspended',?,'S1',?)", (wid, now))
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,started_at,"
        "context_return_context_key,context_return_run_id,"
        "context_return_after_turn,context_return_last_attempt_at) "
        "VALUES ('R-TMP','qa','T1','suspended',?,'K','R-ORPH',1,?)", (now, now))
    conn.commit()

    sched._reap_orphaned_step_runs(conn)

    orphan = conn.execute("SELECT status FROM runs WHERE id='R-ORPH'").fetchone()
    assert orphan["status"] == "failed"
    marker = conn.execute(
        "SELECT context_return_after_turn FROM runs WHERE id='R-TMP'").fetchone()
    assert marker["context_return_after_turn"] == 0


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


def _add_delegation(conn, wid: str, *, child_id: str = "C1",
                    status: str = "in_progress", age_seconds: int = 0) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    conn.execute(
        "INSERT INTO tasks (id, title, status, type, parent_id, parent_step_id, "
        "parent_workflow_id, parent_execution_no, assignee, created_at, updated_at) "
        "VALUES (?,?,?,'delegation','T1','S1',?,1,'qa',?,?)",
        (child_id, "委派子任务", status, wid, stamp, stamp))
    conn.commit()


def test_nudge_skips_step_waiting_for_delegation(env):
    """委派负责人在 todo/in_progress：父 step 合法休眠，不触发 nudge。"""
    conn, sched, orch, wid = env
    _add_delegation(conn, wid)
    sched._nudge_stalled(conn)
    assert orch.pumped == []
    row = conn.execute(
        "SELECT nudge_count FROM workflow_steps WHERE workflow_id=?", (wid,)).fetchone()
    assert row["nudge_count"] == 0


def test_nudge_wakes_step_when_delegation_review_pending(env):
    """委派已提交 in_review：等待权回到父 step，必须唤醒验收而不是继续静默。"""
    conn, sched, orch, wid = env
    _add_delegation(conn, wid, status="in_review")
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R1'").fetchone()["wake_note"]
    assert "delegation-accept" in note and "C1" in note


def test_nudge_resumes_after_delegation_wait_timeout(env, monkeypatch):
    """等待有界：超过 DELEGATION_WAIT_SECONDS 后恢复 nudge，由父 step 上报/求助。"""
    monkeypatch.setattr(config, "DELEGATION_WAIT_SECONDS", 60)
    conn, sched, orch, wid = env
    _add_delegation(conn, wid, age_seconds=120)
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R1'").fetchone()["wake_note"]
    assert "仍在等待委派" in note


def test_wait_state_none_after_delegation_done(env):
    conn, sched, orch, wid = env
    _add_delegation(conn, wid, status="done")
    sched._nudge_stalled(conn)
    assert orch.pumped == [("R1", "resume")]


def test_flush_wake_request_resumes_suspended_step(env):
    """scheduler 投递 step 唤醒请求：写 once 标记并 resume 对应 run。"""
    from anvil.services import wake as wake_service

    conn, sched, orch, wid = env
    req_id = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", workflow_id=wid, step_id="S1",
        reason="delegation.submitted",
        note="[委派待验收] 子任务 C1 已提交")
    conn.commit()

    sched._flush_wake_requests(conn)

    assert orch.pumped == [("R1", "resume")]
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R1'").fetchone()["wake_note"]
    assert "委派待验收" in note
    row = conn.execute("SELECT status FROM wake_requests WHERE id=?", (req_id,)).fetchone()
    assert row["status"] == "done"


def test_flush_wake_request_skips_finished_step(env):
    from anvil.services import wake as wake_service

    conn, sched, orch, wid = env
    conn.execute("UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
    req_id = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", workflow_id=wid, step_id="S1",
        reason="delegation.accepted", note="[委派已解除]")
    conn.commit()

    sched._flush_wake_requests(conn)

    assert orch.pumped == []
    row = conn.execute("SELECT status FROM wake_requests WHERE id=?", (req_id,)).fetchone()
    assert row["status"] == "skipped"


def test_flush_wake_request_creates_run_for_rejected_delegation(env):
    """打回后负责人没有活跃 run：scheduler 建 run 并投递返工提示。"""
    from anvil.services import wake as wake_service

    conn, sched, orch, wid = env
    now = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO roles (slug,display_name,home_path,definition_json,"
        "created_at,updated_at,enabled) VALUES ('qa','QA','/tmp/qa','{}',?,?,1)",
        (now, now))
    conn.execute(
        "INSERT INTO tasks (id, title, status, type, parent_id, parent_step_id, "
        "assignee, created_at, updated_at) "
        "VALUES ('C2','返工子任务','in_progress','delegation','T1','S1','qa',?,?)",
        (now, now))
    conn.commit()
    req_id = wake_service.enqueue(
        conn, task_id="C2", role_slug="qa", reason="delegation.rejected",
        note="[委派打回] 缺少命令输出，请返工")
    conn.commit()

    sched._flush_wake_requests(conn)

    run = conn.execute(
        "SELECT id, status FROM runs WHERE task_id='C2' AND role_slug='qa'").fetchone()
    assert run is not None and run["status"] == "created"
    assert (run["id"], "wake") in orch.pumped
    note = conn.execute("SELECT wake_note FROM runs WHERE id=?", (run["id"],)).fetchone()
    assert "缺少命令输出" in note["wake_note"]


def test_reap_interrupted_subagents_for_non_running_run(env):
    """run 已 suspended/failed：残留 running 子代理必须收尾为 interrupted。"""
    conn, sched, orch, wid = env
    now = now_iso()
    conn.execute(
        "UPDATE runs SET status='suspended' WHERE id='R1'")
    conn.execute(
        "INSERT INTO run_subagents (id,run_id,role_slug,task_id,tool_use_id,"
        "status,started_at,updated_at) VALUES "
        "('S1','R1','dev','T1','tu-1','running',?,?)", (now, now))
    conn.commit()

    sched._reap_interrupted_subagents(conn)

    row = conn.execute(
        "SELECT status, interrupt_reason FROM run_subagents WHERE id='S1'").fetchone()
    assert row["status"] == "interrupted"
    assert row["interrupt_reason"] == "run_suspended"


def test_reap_interrupted_subagents_for_deleted_run(env):
    conn, sched, orch, wid = env
    now = now_iso()
    conn.execute(
        "INSERT INTO run_subagents (id,run_id,role_slug,task_id,tool_use_id,"
        "status,started_at,updated_at) VALUES "
        "('S2','R-MISSING','dev','T1','tu-2','running',?,?)", (now, now))
    conn.commit()

    sched._reap_interrupted_subagents(conn)

    row = conn.execute(
        "SELECT status, interrupt_reason FROM run_subagents WHERE id='S2'").fetchone()
    assert row["status"] == "interrupted"
    assert row["interrupt_reason"] == "run_deleted"


def test_running_subagent_does_not_suppress_step_nudge(env):
    """运行时 subagent 不属于等待关系：看门狗照常 nudge，状态机不得依赖它。"""
    conn, sched, orch, wid = env
    now = now_iso()
    conn.execute(
        "INSERT INTO run_subagents (id,run_id,role_slug,task_id,tool_use_id,"
        "status,started_at,updated_at) VALUES "
        "('S3','R1','dev','T1','tu-3','running',?,?)", (now, now))
    conn.commit()

    sched._nudge_stalled(conn)

    assert orch.pumped == [("R1", "resume")]


def _enqueue_step_wake(conn, wid, *, note="[委派待验收] 请处理"):
    from anvil.services import wake as wake_service
    req = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", workflow_id=wid, step_id="S1",
        reason="delegation.submitted", note=note)
    conn.commit()
    return req


def test_wake_running_target_persists_note_and_retries(env):
    """目标 run 仍 running：note 必须先持久化，再重试到 suspended。"""
    conn, sched, orch, wid = env
    conn.execute("UPDATE runs SET status='running' WHERE id='R1'")
    conn.commit()
    req = _enqueue_step_wake(conn, wid)

    sched._flush_wake_requests(conn)

    assert orch.pumped == []
    row = conn.execute("SELECT status, attempts FROM wake_requests WHERE id=?",
                       (req,)).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R1'").fetchone()["wake_note"]
    assert "委派待验收" in note

    conn.execute("UPDATE runs SET status='suspended' WHERE id='R1'")
    conn.commit()
    sched._flush_wake_requests(conn)
    assert ("R1", "resume") in orch.pumped
    row = conn.execute("SELECT status FROM wake_requests WHERE id=?", (req,)).fetchone()
    assert row["status"] == "done"


def test_wake_pump_failure_keeps_request_pending(env):
    """pump 失败：不能先标 done；保留 pending 供后续重试。"""
    for name, orch in (("pump", _FailingOrch()),):
        conn, sched0, _orch, wid = env
        sched = Scheduler(sched0.db_path, orch)
        req = _enqueue_step_wake(conn, wid, note=f"[委派待验收] {name}")
        sched._flush_wake_requests(conn)
        row = conn.execute("SELECT status FROM wake_requests WHERE id=?",
                           (req,)).fetchone()
        assert row["status"] == "pending"
        note = conn.execute(
            "SELECT wake_note FROM runs WHERE id='R1'").fetchone()["wake_note"]
        assert "委派待验收" in note


def test_wake_skips_closed_task_and_hitl_review(env):
    conn, sched, orch, wid = env
    req = _enqueue_step_wake(conn, wid)
    conn.execute("UPDATE tasks SET status='closed' WHERE id='T1'")
    conn.commit()
    sched._flush_wake_requests(conn)
    assert orch.pumped == []
    assert conn.execute("SELECT status FROM wake_requests WHERE id=?",
                        (req,)).fetchone()["status"] == "skipped"

    # 任务级 wake：reviewing run 不能被 resume 绕过 HITL。
    from anvil.services import wake as wake_service
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO roles (slug,display_name,home_path,"
                 "definition_json,created_at,updated_at,enabled) "
                 "VALUES ('dev','Dev','/tmp/dev','{}',?,?,1)", (now, now))
    conn.execute("INSERT INTO runs (id,role_slug,task_id,status,started_at) "
                 "VALUES ('R-HITL','dev','T1','reviewing',?)", (now,))
    conn.execute("UPDATE tasks SET status='in_review' WHERE id='T1'")
    conn.commit()
    req2 = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", reason="delegation.rejected",
        note="[委派打回] 返工")
    conn.commit()
    sched._flush_wake_requests(conn)
    assert ("R-HITL", "resume") not in orch.pumped
    assert conn.execute("SELECT status FROM wake_requests WHERE id=?",
                        (req2,)).fetchone()["status"] == "skipped"


def test_wake_run_missing_releases_lease(env):
    from anvil.services import leases, wake as wake_service

    conn, sched, orch, wid = env
    lease = leases.acquire(conn, "step", f"{wid}:S1", holder="dev")
    conn.execute(
        "UPDATE workflow_steps SET status='running', run_id='R-MISSING', "
        "lease_id=?, fence_token=? WHERE workflow_id=?",
        (lease["id"], lease["fence"], wid))
    conn.commit()
    req = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", workflow_id=wid, step_id="S1",
        reason="delegation.submitted", note="[委派待验收]")
    conn.commit()

    sched._flush_wake_requests(conn)

    row = conn.execute(
        "SELECT status, run_id, lease_id FROM workflow_steps WHERE workflow_id=?",
        (wid,)).fetchone()
    # 重置后 advance 可能立即重新启动；关键是不能继续持有旧 lease/旧 run。
    assert row["run_id"] != "R-MISSING"
    assert row["lease_id"] != lease["id"]
    lease_row = conn.execute("SELECT status FROM leases WHERE id=?",
                             (lease["id"],)).fetchone()
    assert lease_row["status"] == "released"
    assert conn.execute("SELECT status FROM wake_requests WHERE id=?",
                        (req,)).fetchone()["status"] == "done"


class _FailingOrch:
    def pump(self, db_path, run_id, event, payload):
        raise RuntimeError("pump failed")


def test_wake_enqueue_updates_pending_note(env):
    from anvil.services import wake as wake_service

    conn, sched, orch, wid = env
    first = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", workflow_id=wid, step_id="S1",
        reason="delegation.rejected", note="第一次 feedback")
    second = wake_service.enqueue(
        conn, task_id="T1", role_slug="dev", workflow_id=wid, step_id="S1",
        reason="delegation.rejected", note="第二次 feedback")
    conn.commit()
    assert first == second
    rows = conn.execute("SELECT note FROM wake_requests WHERE id=?", (first,)).fetchall()
    assert len(rows) == 1 and "第二次" in rows[0]["note"]
