"""P1: Cindy 规划前澄清的状态服务。

覆盖：触发条件、状态迁移、简报版本/投影、流程强度衔接、stale、确认权。
失败测试先行；实现见 ``anvil/services/clarification.py``。
"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import clarification, roles, task_workspace, tasks
from anvil.util import ConflictError, new_id, now_iso


def _task(conn, tid: str = "T1", **fields) -> dict:
    now = now_iso()
    planner = fields.pop("planner", "cindy")
    status = fields.pop("status", "todo")
    task_type = fields.pop("type", "feature")
    parent_id = fields.pop("parent_id", None)
    conn.execute(
        "INSERT INTO tasks (id,title,type,status,created_at,updated_at,tags_json,"
        "planner,parent_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, fields.pop("title", "任务"), task_type, status, now, now,
         json.dumps(fields.pop("tags", [])), planner, parent_id))
    for key, value in fields.items():
        col = f"{key}_json" if key in ("scope_paths", "acceptance") else key
        stored = json.dumps(value) if col.endswith("_json") else value
        conn.execute(f"UPDATE tasks SET {col}=? WHERE id=?", (stored, tid))
    conn.commit()
    return tasks.get(conn, tid)


def _workflow(conn, tid: str = "T1", status: str = "done") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id(), tid, "{}", status, now, now))
    conn.commit()


def _pending_draft(conn, tid: str = "T1", kind: str = "workflow") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO board_drafts (id,goal,tree_json,status,created_at,source_task,kind) "
        "VALUES (?,?,?,?,?,?,?)",
        (new_id(), "g", "{}", "pending", now, tid, kind))
    conn.commit()


def _run(conn, tid: str = "T1", *, status: str = "running",
         origin: str | None = "mention", step_id: str | None = None) -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,origin,step_id,started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (new_id(), "dev", tid, status, origin, step_id, now))
    conn.commit()


def _brief(**overrides) -> dict:
    brief = {
        "goal": "实现登录",
        "true_intent": "让用户能安全登录",
        "non_goals": ["不做注册"],
        "constraints": ["沿用现有会话表"],
        "acceptance": ["登录成功返回 token"],
        "assumptions": ["已有用户表"],
        "open_questions": [],
        "weight": "standard",
        "weight_rationale": "中等风险",
        "plan_shape": "auto",
        "plan_shape_rationale": "按 scope 判断",
        "risks": ["会话固定"],
    }
    brief.update(overrides)
    return brief


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "c.db"))
    roles.ensure_onboarding(c)
    return c


# ---- 触发条件 -------------------------------------------------------------


def test_start_from_none(conn):
    _task(conn)
    state = clarification.start(conn, "T1", actor="human", reason="先对齐需求")
    assert state["status"] == "in_progress"
    assert state["round"] == 0
    row = conn.execute(
        "SELECT clarification_status FROM tasks WHERE id='T1'").fetchone()
    assert row["clarification_status"] == "in_progress"
    events = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events WHERE task_id='T1'")]
    assert "clarification.started" in events


def test_start_rejects_non_cindy_planner(conn):
    _task(conn, planner="dev")
    assert clarification.can_clarify(conn, "T1", planner="dev") is False
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1", planner="dev")


def test_start_rejects_when_workflow_exists(conn):
    _task(conn)
    _workflow(conn)
    assert clarification.can_clarify(conn, "T1") is False
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


def test_start_rejects_pending_draft(conn):
    _task(conn)
    _pending_draft(conn)
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


def test_start_rejects_active_execution_run(conn):
    _task(conn)
    _run(conn, status="running", origin="mention")
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


def test_start_rejects_completed_execution_run(conn):
    _task(conn)
    _run(conn, status="completed", origin="mention")
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


def test_start_rejects_dispatch_child(conn):
    _task(conn, type="dispatch", parent_id="P1")
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


def test_start_rejects_non_todo_task(conn):
    _task(conn, status="in_progress")
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


def test_start_rejects_when_flag_off(conn, monkeypatch):
    _task(conn)
    monkeypatch.setattr(config, "CINDY_CLARIFICATION_ENABLED", False, raising=False)
    assert clarification.can_clarify(conn, "T1") is False
    with pytest.raises(ConflictError):
        clarification.start(conn, "T1")


# ---- 草稿与确认 -----------------------------------------------------------


def test_save_draft_records_version_and_projection(conn):
    _task(conn)
    clarification.start(conn, "T1")
    state = clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    assert state["brief"]["goal"] == "实现登录"
    assert state["brief_version"] == 1
    rows = clarification.versions(conn, "T1")
    assert [r["kind"] for r in rows] == ["draft"]
    path = task_workspace.path_for("T1") / ".anvil" / "intent-brief.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["goal"] == "实现登录"


def test_confirm_requires_ready_brief(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(goal=""), actor="cindy")
    with pytest.raises(ValueError):
        clarification.confirm(conn, "T1", actor="human")


def test_confirm_requires_open_question_ack(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(
        conn, "T1", _brief(open_questions=["是否支持 SSO"]), actor="cindy")
    with pytest.raises(ValueError):
        clarification.confirm(conn, "T1", actor="human")
    state = clarification.confirm(
        conn, "T1", actor="human", acknowledge_open_questions=True)
    assert state["status"] == "confirmed"


def test_confirm_sets_weight_version_and_projection(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    state = clarification.confirm(conn, "T1", actor="human")
    assert state["status"] == "confirmed"
    assert state["brief_version"] == 2
    task = tasks.get(conn, "T1")
    assert task["workflow_weight"] == "standard"
    assert task["workflow_weight_source"] == "human"
    assert task["workflow_weight_reason"].startswith("澄清阶段确认")
    assert task["clarification_basis_hash"]
    assert [r["kind"] for r in clarification.versions(conn, "T1")] == [
        "draft", "confirmed"]
    path = task_workspace.path_for("T1") / ".anvil" / "intent-brief.json"
    assert json.loads(path.read_text(encoding="utf-8"))["confirmed_by"] == "human"


def test_confirm_is_cas(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", actor="human")
    with pytest.raises(ConflictError):
        clarification.confirm(conn, "T1", actor="human")


def test_skip_records_state_without_brief(conn):
    _task(conn)
    clarification.start(conn, "T1")
    state = clarification.skip(conn, "T1", actor="human", reason="需求明确")
    assert state["status"] == "skipped"
    task = tasks.get(conn, "T1")
    assert task["clarification_basis_hash"]
    assert task["workflow_weight"] is None


def test_state_flags(conn):
    _task(conn)
    state = clarification.start(conn, "T1")
    assert state["status"] == "in_progress"
    assert state["can_skip"] is True
    assert state["can_confirm"] is False
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    state = clarification.get_state(conn, "T1")
    assert state["ready"] is True
    assert state["can_confirm"] is True
    clarification.confirm(conn, "T1", actor="human")
    state = clarification.get_state(conn, "T1")
    assert state["status"] == "confirmed"
    assert state["can_start"] is False
    assert state["can_edit"] is True


# ---- 编辑与 stale ---------------------------------------------------------


def test_update_brief_after_confirm(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", actor="human")
    state = clarification.update_brief(
        conn, "T1", _brief(goal="新目标", weight="light"), actor="human")
    assert state["status"] == "confirmed"
    assert state["brief"]["goal"] == "新目标"
    assert state["brief_version"] == 3
    task = tasks.get(conn, "T1")
    assert task["workflow_weight"] == "light"
    assert [r["kind"] for r in clarification.versions(conn, "T1")] == [
        "draft", "confirmed", "human_edit"]


def test_update_brief_blocked_by_active_planner_run(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", actor="human")
    _run(conn, status="running", origin=None)
    with pytest.raises(ConflictError):
        clarification.update_brief(conn, "T1", _brief(goal="新目标"), actor="human")


def test_mark_stale_after_task_change(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", actor="human")
    # tasks.update 的材料性变更钩子会自动标 stale。
    tasks.update(conn, "T1", description="新的描述")
    state = clarification.get_state(conn, "T1")
    assert state["status"] == "stale"
    events = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events WHERE task_id='T1'")]
    assert "clarification.stale" in events
    row = conn.execute(
        "SELECT payload_json FROM activities WHERE task_id='T1' AND kind='mention' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert json.loads(row["payload_json"])["target"] == "human"


def test_restart_from_skipped(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.skip(conn, "T1", actor="human")
    state = clarification.start(conn, "T1", actor="human", reason="重新澄清")
    assert state["status"] == "in_progress"
    assert state["round"] == 0


def test_mark_stale_ignores_tags(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", actor="human")
    tasks.update(conn, "T1", tags=["new-tag"])
    assert clarification.mark_stale(conn, "T1") is None
    state = clarification.get_state(conn, "T1")
    assert state["status"] == "confirmed"


def test_mark_stale_noop_when_workflow_exists(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", actor="human")
    _workflow(conn)
    tasks.update(conn, "T1", description="新的描述")
    assert clarification.mark_stale(conn, "T1") is None
    state = clarification.get_state(conn, "T1")
    assert state["status"] == "confirmed"


# ---- 门禁与 clarify run ---------------------------------------------------


def test_assert_workflow_allowed(conn):
    _task(conn)
    clarification.start(conn, "T1")
    with pytest.raises(ConflictError):
        clarification.assert_workflow_allowed(conn, "T1")
    clarification.skip(conn, "T1", actor="human")
    clarification.assert_workflow_allowed(conn, "T1")


def test_confirmed_weight_overrides_draft_tree(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(
        conn, "T1", weight="light", plan_shape="flat", actor="human")
    tree = {"weight": "strict", "steps": [
        {"id": "S1", "title": "实现", "role": "dev"}]}
    out = clarification.apply_confirmed_weight(conn, "T1", tree)
    assert out["weight"] == "light"
    assert out["weight_rationale"].startswith("澄清阶段确认")
    clarification.enforce_plan_shape(conn, "T1", tree)  # flat: 允许扁平
    with pytest.raises(ConflictError):
        clarification.enforce_plan_shape(
            conn, "T1", {**tree, "loop": {"execute": {"role": "dev"}}})


def test_plan_shape_slices_requires_loop(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", plan_shape="slices", actor="human")
    flat = {"steps": [{"id": "S1", "title": "实现", "role": "dev"}]}
    with pytest.raises(ConflictError):
        clarification.enforce_plan_shape(conn, "T1", flat)
    sliced = {
        "steps": [{"id": "P", "title": "切片计划", "kind": "plan_slices",
                   "role": "dev"}],
        "loop": {"execute": {"role": "dev"}},
    }
    clarification.enforce_plan_shape(conn, "T1", sliced)


def test_plan_shape_auto_allows_any(conn):
    _task(conn)
    clarification.start(conn, "T1")
    clarification.save_draft(conn, "T1", _brief(), actor="cindy")
    clarification.confirm(conn, "T1", plan_shape="auto", actor="human")
    clarification.enforce_plan_shape(
        conn, "T1", {"steps": [{"id": "S1", "title": "实现", "role": "dev"}]})
    clarification.enforce_plan_shape(conn, "T1", {
        "steps": [{"id": "P", "kind": "plan_slices", "role": "dev"}],
        "loop": {"execute": {"role": "dev"}}})


def test_weight_override_only_after_confirmation(conn):
    _task(conn)
    clarification.start(conn, "T1")
    tree = {"weight": "strict"}
    out = clarification.apply_confirmed_weight(conn, "T1", tree)
    assert out["weight"] == "strict"


def test_planning_conflict_during_clarification(conn):
    _task(conn)
    clarification.start(conn, "T1")
    task = tasks.get(conn, "T1")
    conflict = tasks.planning_conflict(conn, task)
    assert conflict and "clarified" in conflict


def test_start_clarify_run_prompt_and_round(conn, tmp_path):
    _task(conn)
    clarification.start(conn, "T1")
    run = clarification.start_clarify_run(
        conn, str(tmp_path / "c.db"), None, "T1", "cindy")
    assert run["origin"] == "clarify"
    assert run["prompt"].startswith("[anvil:clarify]")
    assert "规划前澄清" in run["prompt"]
    assert "[anvil:plan]" not in run["prompt"]
    assert tasks.get(conn, "T1")["clarification_round"] == 1

    run2 = clarification.start_clarify_run(
        conn, str(tmp_path / "c.db"), None, "T1", "cindy",
        human_message="补充：必须支持 SSO", replace_active=True)
    assert "human 本轮回复" in run2["prompt"]
    assert "补充：必须支持 SSO" in run2["prompt"]
    assert tasks.get(conn, "T1")["clarification_round"] == 2


def test_start_clarify_run_must_conclude(conn, tmp_path):
    _task(conn)
    clarification.start(conn, "T1")
    conn.execute("UPDATE tasks SET clarification_round=5 WHERE id='T1'")
    conn.commit()
    run = clarification.start_clarify_run(
        conn, str(tmp_path / "c.db"), None, "T1", "cindy")
    assert "must_conclude：true" in run["prompt"]
