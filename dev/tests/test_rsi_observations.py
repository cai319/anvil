"""P1 RSI observation/trigger tests (spec RSI-OBS-001..005)."""
from __future__ import annotations

import json

import pytest

from anvil import config, db
from anvil.services import role_state, rsi, tasks as tasks_service
from anvil.util import now_iso


@pytest.fixture(autouse=True)
def _rsi_enabled(monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)


def _role(conn, slug: str) -> None:
    conn.execute(
        "INSERT INTO roles (slug, display_name, home_path, definition_json, "
        "created_at, updated_at, enabled) VALUES (?,?,?,?,?,?,1)",
        (slug, slug, f"/tmp/{slug}",
         '{"rsi": {"enabled": true}}', now_iso(), now_iso()))


def _seed(conn, task_id: str = "T-RSI") -> None:
    for slug in ("role-a", "role-b", "role-c", "role-d", "role-e"):
        _role(conn, slug)
    conn.execute(
        "INSERT INTO tasks (id, title, type, status, assignee, planner, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, "RSI 观测", "feature", "done", "role-a", "role-e",
         now_iso(), now_iso()))
    conn.execute(
        "INSERT INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
        (task_id, "role-a", now_iso()))
    conn.execute(
        "INSERT INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
        (task_id, "role-b", now_iso()))
    conn.execute(
        "INSERT INTO task_role_claims (task_id, role_slug, status, updated_at) "
        "VALUES (?,?,?,?)", (task_id, "role-c", "approved", now_iso()))
    conn.execute(
        "INSERT INTO workflows (id, task_id, definition_json, status, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?)",
        ("W-RSI", task_id, "{}", "done", now_iso(), now_iso()))
    conn.execute(
        "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, "
        "updated_at) VALUES (?,?,?,?,?)",
        ("W-RSI", "S1", "步骤", "role-d", now_iso()))
    conn.commit()


def _count(conn, task_id: str = "T-RSI") -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='rsi.observation' "
        "AND task_id=?", (task_id,)).fetchone()[0]


def test_done_creates_idempotent_observation_for_all_participants(tmp_path):
    conn = db.connect(str(tmp_path / "obs.db"))
    try:
        _seed(conn)
        first = rsi.observe_task_done(
            conn, "T-RSI", source="review", completion_key="review:1")
        assert {x["role"] for x in first} == {
            "role-a", "role-b", "role-c", "role-d", "role-e"}
        assert all(x["created"] is True for x in first)
        assert _count(conn) == 5
        again = rsi.observe_task_done(
            conn, "T-RSI", source="review", completion_key="review:1")
        assert all(x["created"] is False for x in again)
        assert _count(conn) == 5
        # A reopened task that completes again gets a new observation set.
        rsi.observe_task_done(
            conn, "T-RSI", source="review", completion_key="review:2")
        assert _count(conn) == 10
        # seed receipt is idempotent as well.
        seed_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rsi.harvest_seed'"
        ).fetchone()[0]
        assert seed_count >= 5
    finally:
        conn.close()


def test_participants_exclude_system_and_unknown_roles(tmp_path):
    conn = db.connect(str(tmp_path / "participants.db"))
    try:
        _seed(conn)
        conn.execute(
            "INSERT INTO task_role_claims (task_id, role_slug, status, updated_at) "
            "VALUES (?,?,?,?)", ("T-RSI", "human", "pending", now_iso()))
        conn.execute(
            "INSERT INTO task_role_claims (task_id, role_slug, status, updated_at) "
            "VALUES (?,?,?,?)", ("T-RSI", "ghost", "pending", now_iso()))
        conn.commit()
        roles = rsi.participants(conn, "T-RSI")
        assert "human" not in roles
        assert "ghost" not in roles
        assert "role-a" in roles
    finally:
        conn.close()


def test_failure_and_rework_observations_are_idempotent(tmp_path):
    conn = db.connect(str(tmp_path / "fail.db"))
    try:
        _seed(conn)
        a = rsi.observe_failure(
            conn, "T-RSI", role="role-a", step_id="S1", run_id="R-1",
            failure_class="test")
        assert a["created"] is True
        again = rsi.observe_failure(
            conn, "T-RSI", role="role-a", step_id="S1", run_id="R-1",
            failure_class="test")
        assert again["created"] is False
        b = rsi.observe_rework(
            conn, "T-RSI", role="role-a", step_id="S1", feedback="补测试")
        assert b["created"] is True
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE event_type='rsi.observation' "
            "ORDER BY id").fetchall()
        triggers = [json.loads(r["payload_json"]).get("trigger") for r in rows]
        assert "step_failure" in triggers
        assert "rework" in triggers
    finally:
        conn.close()


def test_due_trigger_respects_volume_cooldown_and_quota(tmp_path):
    conn = db.connect(str(tmp_path / "due.db"))
    try:
        _seed(conn)
        assert rsi.due(conn, "role-a")["due"] is False
        for i in range(3):
            rsi.observe_task_done(
                conn, "T-RSI", source="review", completion_key=f"review:{i}")
        assert rsi.due(conn, "role-a")["due"] is True
        rsi.mark_reflection_started(conn, "role-a", cycle_key="C-1")
        assert rsi.due(conn, "role-a")["due"] is False
        rsi.record_deferred(conn, "role-a", reason="busy")
        obs = rsi.list_observations(conn, "role-a", limit=10)
        assert len(obs) >= 3
    finally:
        conn.close()


def test_disabled_master_flag_is_zero_impact(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", False)
    conn = db.connect(str(tmp_path / "off.db"))
    try:
        _seed(conn)
        assert rsi.observe_task_done(
            conn, "T-RSI", source="review", completion_key="review:off") == []
        assert _count(conn) == 0
        assert rsi.due(conn, "role-a")["due"] is False
        assert rsi.preempt(conn, "role-a")["preempted"] == 0
        assert role_state.evolving(conn, "role-a")["evolving"] is False
    finally:
        conn.close()


def test_review_hook_creates_observations_for_participants(tmp_path):
    conn = db.connect(str(tmp_path / "review-hook.db"))
    try:
        _seed(conn)
        conn.execute("UPDATE tasks SET status='in_review' WHERE id='T-RSI'")
        conn.commit()
        tasks_service.review(conn, "T-RSI", True, role="human",
                             notify_rework=False, notify_parent=False)
        assert _count(conn) == 5
    finally:
        conn.close()


def test_evolving_state_derived_from_reflect_run(tmp_path):
    conn = db.connect(str(tmp_path / "evolving.db"))
    try:
        _seed(conn)
        assert role_state.evolving(conn, "role-a")["evolving"] is False
        conn.execute(
            "INSERT INTO runs (id, role_slug, status, origin, started_at) "
            "VALUES (?,?,?,?,?)",
            ("R-RSI-2", "role-a", "running", "rsi_reflect", now_iso()))
        conn.commit()
        state = role_state.evolving(conn, "role-a")
        assert state["evolving"] is True
        assert state["run_id"] == "R-RSI-2"
    finally:
        conn.close()


def test_preempt_marks_active_reflection_run(tmp_path):
    conn = db.connect(str(tmp_path / "preempt.db"))
    try:
        _seed(conn)
        conn.execute(
            "INSERT INTO runs (id, role_slug, status, origin, started_at) "
            "VALUES (?,?,?,?,?)",
            ("R-RSI", "role-a", "running", "rsi_reflect", now_iso()))
        conn.commit()
        result = rsi.preempt(conn, "role-a", reason="task wake")
        assert result["preempted"] == 1
        row = conn.execute("SELECT status FROM runs WHERE id='R-RSI'").fetchone()
        assert row["status"] == "abandoned"
        ev = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rsi.preempted'"
        ).fetchone()[0]
        assert ev == 1
    finally:
        conn.close()
