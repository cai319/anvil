"""事件账本、run 围栏与 dispatch 治理的基础回归。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import governance
from anvil.services import ledger
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso


def _task(conn, tid: str = "T1") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES (?,?, 'todo',?,?)",
        (tid, "t", now, now),
    )
    conn.commit()


def test_emit_and_query_task_events(tmp_path):
    conn = connect(str(tmp_path / "e.db"))
    _task(conn)
    eid = ledger.emit(
        conn, "step.started", task_id="T1", workflow_id="W1", execution_no=1,
        step_id="S1", actor_type="role", actor="dev", payload={"x": 1},
        mirror_activity=True, activity_kind="step", activity_role="dev",
    )
    assert eid > 0
    events = ledger.list_task_events(conn, "T1")
    assert events["items"][0]["event_type"] == "step.started"
    assert events["items"][0]["payload"]["x"] == 1
    activity = conn.execute("SELECT * FROM activities WHERE event_id=?", (eid,)).fetchone()
    assert activity["workflow_id"] == "W1"
    assert activity["step_id"] == "S1"


def test_backfill_activities_and_audit_once(tmp_path):
    conn = connect(str(tmp_path / "b.db"))
    _task(conn)
    now = now_iso()
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES ('T1','dev','step','{\"status\":\"approved\",\"step\":\"S1\"}',?)", (now,))
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES ('human','T1','review','{\"approve\":true}',?)", (now,))
    conn.commit()
    added = ledger.backfill_once(conn)
    assert added == 2
    types = {e["event_type"] for e in ledger.list_task_events(conn, "T1")["items"]}
    assert types == {"step.approved", "audit.review"}
    assert ledger.backfill_once(conn) == 0


def test_stale_run_cannot_submit(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute(
        "UPDATE workflow_steps SET status='running', run_id='run-new' "
        "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
    conn.commit()
    with pytest.raises(ConflictError, match="stale_run"):
        wf_service.submit_step(conn, wf["id"], "S1", "dev", "old", run_id="run-old")
    wf_service.submit_step(conn, wf["id"], "S1", "dev", "new", run_id="run-new")
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"]
                if s["step_id"] == "S1")
    assert step["status"] == "approved"


def test_freeze_blocks_workflow_dispatch(tmp_path):
    conn = connect(str(tmp_path / "f.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    governance.set_freeze(conn, True)
    calls: list[str] = []
    result = wf_service.advance(conn, wf["id"], starter=lambda *a: calls.append("started") or {"id": "r1"})
    assert result["started"] == []
    assert calls == []
    assert conn.execute(
        "SELECT 1 FROM events WHERE event_type='dispatch.deferred'").fetchone() is not None
