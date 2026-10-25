"""Workflow weight resolution and human override semantics."""
from __future__ import annotations

import json

import pytest

from anvil.db import connect
from anvil.services import weights
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso


def _task(conn, tid: str = "T1", **fields) -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at,tags_json) "
        "VALUES (?,?, 'todo',?,?,?)",
        (tid, fields.pop("title", "任务"), now, now,
         json.dumps(fields.pop("tags", []))))
    for key, value in fields.items():
        conn.execute(f"UPDATE tasks SET {key}=? WHERE id=?", (value, tid))
    conn.commit()


def test_heuristic_infers_strict_for_security_task(tmp_path):
    conn = connect(str(tmp_path / "w.db"))
    _task(conn, tags=["security"], description="权限与支付改造")
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"}]})
    assert wf["weight"] == "strict"
    assert wf["weight_source"] == "heuristic"
    assert wf["steps"][0]["evidence_required"] is True


def test_cindy_weight_wins_over_heuristic(tmp_path):
    conn = connect(str(tmp_path / "w.db"))
    _task(conn, tags=["security"])
    wf = wf_service.create_workflow(conn, "T1", {
        "weight": "light", "weight_rationale": "小范围可逆改动",
        "steps": [{"id": "S1", "title": "实现", "role": "dev"}]})
    assert wf["weight"] == "light"
    assert wf["weight_source"] == "cindy"
    assert wf["weight_reason"] == "小范围可逆改动"
    assert wf["steps"][0]["evidence_required"] is False


def test_human_override_takes_precedence_and_requires_restart(tmp_path):
    conn = connect(str(tmp_path / "w.db"))
    _task(conn, tags=["security"])
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"}]})
    assert wf["weight"] == "strict"
    result = weights.set_task_weight(conn, "T1", "light", "只是文档修订")
    assert result["requires_restart"] is True
    # Running execution is unchanged; the override applies to the next execution.
    assert wf_service.get_workflow(conn, wf["id"])["weight"] == "strict"
    wf_service._terminate_running_workflow(conn, wf["id"], "人工重启", now_iso())
    conn.commit()
    nxt = wf_service.create_workflow(
        conn, "T1", {"steps": [{"id": "S1", "title": "实现", "role": "dev"}]},
        restart_running=False)
    assert nxt["weight"] == "light"
    assert nxt["weight_source"] == "human"
    assert nxt["weight_reason"] == "只是文档修订"


def test_invalid_weight_rejected(tmp_path):
    conn = connect(str(tmp_path / "w.db"))
    _task(conn)
    with pytest.raises(ConflictError):
        weights.set_task_weight(conn, "T1", "heavy")
