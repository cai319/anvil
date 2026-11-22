"""服务端角色状态推导：与前端 roleStatus.taskRoleState 的状态优先级对齐。"""
from __future__ import annotations

from anvil.services.role_state import derive_task_role_state


def _state(**overrides):
    base = dict(
        task_status="in_progress",
        claim_status=None,
        workflow_status="running",
        steps=[],
        role="dev",
        slice_key=None,
        run_status=None,
        return_pending=False,
        runtime={"alive": False, "status": "dead", "queue": []},
        related_task_ids={"T1"},
        has_context=False,
    )
    base.update(overrides)
    return derive_task_role_state(**base)["state"]


def test_working_here_wins():
    assert _state(
        runtime={"alive": True, "status": "busy", "current_task_id": "T1", "queue": []},
    ) == "working"


def test_task_closed_and_done():
    assert _state(task_status="closed") == "closed"
    assert _state(task_status="done") == "done"


def test_failed_and_rework():
    assert _state(run_status="failed") == "failed"
    assert _state(steps=[{"role_slug": "dev", "step_id": "S1", "status": "rejected"}]) == "rework"


def test_queued_before_retry_and_waiting_human():
    runtime = {"alive": True, "status": "idle", "queue": [{"task_id": "T1"}]}
    assert _state(runtime=runtime) == "queued"
    assert _state(steps=[{"role_slug": "dev", "step_id": "S1", "status": "pending",
                          "recovery_count": 1}]) == "retry"
    assert _state(task_status="in_review") == "waiting_human"
    assert _state(run_status="submitted") == "waiting_human"


def test_all_done_via_claim_or_steps():
    assert _state(claim_status="approved") == "done"
    assert _state(steps=[{"role_slug": "dev", "step_id": "S1", "status": "approved"}]) == "done"


def test_busy_elsewhere_released_return_pending_idle_todo():
    elsewhere = {"alive": True, "status": "busy", "current_task_id": "T2", "queue": []}
    assert _state(runtime=elsewhere) == "not_involved"
    assert _state(claim_status="released") == "released"
    assert _state(return_pending=True) == "return_pending"
    assert _state(has_context=True) == "idle"
    assert _state(claim_status="pending") == "todo"


def test_slice_filter_only_counts_selected_slice():
    steps = [
        {"role_slug": "dev", "step_id": "S1.a.execute", "status": "running",
         "slice_key": "a"},
        {"role_slug": "dev", "step_id": "S1.b.execute", "status": "pending",
         "slice_key": "b"},
    ]
    # slice a 有 running 步骤但无 runtime 忙碌证据 → 不误判 working；
    # slice b 的 pending 只在选中 b 时影响状态。
    assert _state(steps=steps, slice_key="a") == "not_involved"
    assert _state(steps=steps, slice_key="b") == "todo"


class _Mgr:
    enabled = True

    def __init__(self, status):
        self._status = status

    def status(self, role):
        return self._status


def test_task_role_states_loader_uses_runtime_and_claim(tmp_path):
    from anvil.db import connect
    from anvil.services import role_state as role_state_service
    from anvil.services import tasks as tasks_service

    conn = connect(str(tmp_path / "s.db"))
    try:
        task = tasks_service.create(
            conn, title="T", assignee="dev", assignees=["dev"])
        conn.execute(
            "UPDATE task_role_claims SET status='working' WHERE task_id=?",
            (task["id"],))
        conn.commit()
        mgr = _Mgr({
            "alive": True, "status": "busy", "current_task_id": task["id"],
            "current_run_id": None, "current_step_id": None,
            "context_task_id": task["id"], "context_slice_key": None,
            "context_key": task["id"], "queue": [],
        })
        rows = role_state_service.task_role_states(conn, task["id"], mgr)
        row = next(r for r in rows if r["role_slug"] == "dev")
        assert row["state"] == "working"
        assert row["working_here"] is True
    finally:
        conn.close()
