"""独立派遣 / 跨切片临时处理后的自动回切。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import dispatch
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


def _seed(conn, tmp_path):
    now = now_iso()
    for slug in ("dev", "qa"):
        conn.execute(
            "INSERT INTO roles (slug,display_name,home_path,definition_json,"
            "created_at,updated_at,enabled) VALUES (?,?,?,'{}',?,?,1)",
            (slug, slug.upper(), str(tmp_path / "agents" / slug), now, now))
    conn.commit()


def _running_step(conn, task_id: str, step_id: str, run_id: str,
                  slice_key: str, role: str = "dev"):
    now = now_iso()
    wid = f"W-{task_id}"
    conn.execute(
        "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
        "VALUES (?,?,'{}','running',?,?)", (wid, task_id, now, now))
    conn.execute(
        "INSERT INTO workflow_steps "
        "(workflow_id,step_id,title,role_slug,status,slice_key,run_id,updated_at) "
        "VALUES (?,?,?,?,'running',?,?,?)",
        (wid, step_id, step_id, role, slice_key, run_id, now))
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,step_id,workflow_id,slice_key,"
        "started_at) VALUES (?,?,?,'suspended',?,?,?,?)",
        (run_id, role, task_id, step_id, wid, slice_key, now))
    conn.execute(
        "INSERT INTO role_task_sessions "
        "(role_slug,task_key,task_id,slice_key,session_id,status,created_at,updated_at) "
        "VALUES (?,?,?,?,'sid','idle',?,?)",
        (role, f"{task_id}::slice::{slice_key}", task_id, slice_key, now, now))
    conn.execute(
        "INSERT INTO role_sessions "
        "(role_slug,session_id,status,current_task_id,current_run_id,"
        " current_context_key,current_slice_key,updated_at) "
        "VALUES (?,?,'idle',?,?,?,?,?) ON CONFLICT(role_slug) DO UPDATE SET "
        "current_task_id=excluded.current_task_id, current_run_id=excluded.current_run_id, "
        "current_context_key=excluded.current_context_key, "
        "current_slice_key=excluded.current_slice_key, session_id='sid'",
        (role, "sid", task_id, run_id, f"{task_id}::slice::{slice_key}", slice_key, now))
    conn.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    from anvil.services import daemon as daemon_service
    monkeypatch.setattr(daemon_service, "get_manager", lambda: None)
    conn = connect(str(tmp_path / "t.db"))
    _seed(conn, tmp_path)
    yield conn
    conn.close()


def test_dispatch_done_prepares_return_and_resolves_once(env):
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    _running_step(conn, source["id"], "A.scope", "R-original", "A")
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="去处理另一个独立问题", requester="qa", source_slice_key="A")
    target_id = result["task"]["id"]
    # 模拟派遣任务完成。
    conn.execute("UPDATE tasks SET status='in_review' WHERE id=?", (target_id,))
    conn.commit()
    tasks_service.review(conn, target_id, True, "human")

    d = conn.execute(
        "SELECT * FROM task_dispatches WHERE target_task_id=?", (target_id,)).fetchone()
    assert d["status"] == "return_ready"
    run = conn.execute("SELECT wake_note FROM runs WHERE id='R-original'").fetchone()
    assert "派遣任务" in (run["wake_note"] or "")
    target = tasks_service.get(conn, target_id)
    assert target["dispatch_status"] == "return_ready"

    calls: list[str] = []

    def pump(run_id, event):
        calls.append(run_id)
        return True

    assert dispatch.resolve_dispatch_returns(conn, pump) == 1
    assert calls == ["R-original"]
    d2 = conn.execute(
        "SELECT status, returned_at, attempts FROM task_dispatches WHERE id=?",
        (d["id"],)).fetchone()
    assert d2["status"] == "returned"
    assert d2["returned_at"]
    assert d2["attempts"] == 1
    # 幂等：不会重复 resume。
    assert dispatch.resolve_dispatch_returns(conn, pump) == 0
    assert calls == ["R-original"]


def test_dispatch_done_does_not_return_when_original_submitted(env):
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    _running_step(conn, source["id"], "A.scope", "R-original", "A")
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="独立问题", requester="qa", source_slice_key="A")
    target_id = result["task"]["id"]
    conn.execute("UPDATE runs SET status='submitted' WHERE id='R-original'")
    conn.execute("UPDATE tasks SET status='in_review' WHERE id=?", (target_id,))
    conn.commit()
    tasks_service.review(conn, target_id, True, "human")

    d = conn.execute(
        "SELECT status FROM task_dispatches WHERE target_task_id=?", (target_id,)).fetchone()
    assert d["status"] == "no_return"
    assert tasks_service.get(conn, target_id)["dispatch_status"] == "no_return"


def test_context_return_resumes_original_slice_once(env):
    conn = env
    task = tasks_service.create(conn, title="B 片继续工作", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    # A 是临时切过去复核的 run，记录回切目标 B。
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,step_id,slice_key,started_at,"
        "context_return_context_key,context_return_run_id,context_return_after_turn) "
        "VALUES ('R-a','dev',?,'suspended','A.execute','A',?,?,?,1)",
        (task["id"], now, f"{task['id']}::slice::B", "R-b"))
    conn.commit()

    calls: list[str] = []

    def pump(run_id, event):
        calls.append(run_id)
        return True

    assert dispatch.resolve_context_returns(conn, pump) == 1
    assert calls == ["R-b"]
    b = conn.execute("SELECT wake_note FROM runs WHERE id='R-b'").fetchone()
    assert "临时" in (b["wake_note"] or "")
    a = conn.execute(
        "SELECT context_return_after_turn, context_returned_at FROM runs WHERE id='R-a'"
    ).fetchone()
    assert a["context_return_after_turn"] == 0
    assert a["context_returned_at"]
    assert dispatch.resolve_context_returns(conn, pump) == 0


def test_multiple_dispatches_share_one_return_wake_note(env):
    """同一 suspended run 的多个派遣结果应合并到一次回切，而不是互相覆盖。"""
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    _running_step(conn, source["id"], "A.scope", "R-original", "A")
    one = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="独立问题 1", requester="qa", source_slice_key="A")
    two = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="独立问题 2", requester="qa", source_slice_key="A")
    for tid in (one["task"]["id"], two["task"]["id"]):
        conn.execute("UPDATE tasks SET status='in_review' WHERE id=?", (tid,))
    conn.commit()
    tasks_service.review(conn, one["task"]["id"], True, "human")
    tasks_service.review(conn, two["task"]["id"], True, "human")

    note = conn.execute("SELECT wake_note FROM runs WHERE id='R-original'").fetchone()["wake_note"]
    assert one["task"]["id"] in note
    assert two["task"]["id"] in note

    calls: list[str] = []

    def pump(run_id, event):
        calls.append(run_id)
        return True

    assert dispatch.resolve_dispatch_returns(conn, pump) == 1
    assert calls == ["R-original"]
    rows = conn.execute(
        "SELECT status FROM task_dispatches WHERE target_task_id IN (?,?)",
        (one["task"]["id"], two["task"]["id"])).fetchall()
    assert [r["status"] for r in rows] == ["returned", "returned"]


def test_context_return_resumes_when_temporary_run_failed(env):
    """临时切换的 run 失败也应收尾回切，不能留下永久死标记。"""
    conn = env
    task = tasks_service.create(conn, title="B 片继续工作", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,step_id,slice_key,started_at,"
        "context_return_context_key,context_return_run_id,context_return_after_turn,"
        "failure_class) VALUES ('R-a','dev',?,'failed','A.execute','A',?,?,?,1,"
        "'agent_error')",
        (task["id"], now, f"{task['id']}::slice::B", "R-b"))
    conn.commit()

    calls: list[str] = []

    def pump(run_id, event):
        calls.append(run_id)
        return True

    assert dispatch.resolve_context_returns(conn, pump) == 1
    assert calls == ["R-b"]
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R-b'").fetchone()["wake_note"]
    assert "failed" in note and "agent_error" in note
    a = conn.execute(
        "SELECT context_return_after_turn FROM runs WHERE id='R-a'").fetchone()
    assert a["context_return_after_turn"] == 0


def test_context_return_submitted_temporary_run_clears_without_wait(env):
    conn = env
    task = tasks_service.create(conn, title="B 片", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,step_id,slice_key,started_at,"
        "context_return_context_key,context_return_run_id,context_return_after_turn) "
        "VALUES ('R-a','dev',?,'submitted','A.execute','A',?,?,?,1)",
        (task["id"], now, f"{task['id']}::slice::B", "R-b"))
    conn.commit()
    calls: list[str] = []
    assert dispatch.resolve_context_returns(conn, lambda rid, ev: calls.append(rid) or True) == 0
    assert calls == []
    a = conn.execute(
        "SELECT context_return_after_turn, context_returned_at FROM runs WHERE id='R-a'"
    ).fetchone()
    assert a["context_return_after_turn"] == 0
    assert a["context_returned_at"]


def test_multiple_context_returns_merge_into_one_pump(env):
    conn = env
    task = tasks_service.create(conn, title="B 片", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    now = now_iso()
    for rid in ("R-a1", "R-a2"):
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,step_id,slice_key,started_at,"
            "context_return_context_key,context_return_run_id,context_return_after_turn) "
            "VALUES (?,'dev',?,'suspended','A.execute','A',?,?,?,1)",
            (rid, task["id"], now, f"{task['id']}::slice::B", "R-b"))
    conn.commit()
    calls: list[str] = []
    assert dispatch.resolve_context_returns(
        conn, lambda rid, ev: calls.append(rid) or True) == 1
    assert calls == ["R-b"]
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R-b'").fetchone()["wake_note"]
    assert "R-a1" in note and "R-a2" in note
    rows = conn.execute(
        "SELECT context_return_after_turn FROM runs WHERE id IN ('R-a1','R-a2')"
    ).fetchall()
    assert [r["context_return_after_turn"] for r in rows] == [0, 0]


def test_context_return_pump_failure_does_not_duplicate_note(env, monkeypatch):
    from anvil import config

    conn = env
    monkeypatch.setattr(config, "CONTEXT_RETURN_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(config, "CONTEXT_RETURN_MAX_ATTEMPTS", 2)
    task = tasks_service.create(conn, title="B 片", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,step_id,slice_key,started_at,"
        "context_return_context_key,context_return_run_id,context_return_after_turn) "
        "VALUES ('R-a','dev',?,'suspended','A.execute','A',?,?,?,1)",
        (task["id"], now, f"{task['id']}::slice::B", "R-b"))
    conn.commit()
    assert dispatch.resolve_context_returns(conn, lambda rid, ev: False) == 0
    assert dispatch.resolve_context_returns(conn, lambda rid, ev: False) == 0
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R-b'").fetchone()["wake_note"]
    assert note.count("run R-a") == 1
    attempt = conn.execute(
        "SELECT context_return_attempts FROM runs WHERE id='R-a'").fetchone()[0]
    assert attempt >= 2


def test_context_return_missing_slot_does_not_pump(env):
    conn = env
    task = tasks_service.create(conn, title="B 片", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    conn.execute(
        "DELETE FROM role_task_sessions WHERE role_slug='dev' AND task_id=?", (task["id"],))
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,step_id,slice_key,started_at,"
        "context_return_context_key,context_return_run_id,context_return_after_turn) "
        "VALUES ('R-a','dev',?,'suspended','A.execute','A',?,?,?,1)",
        (task["id"], now, f"{task['id']}::slice::B", "R-b"))
    conn.commit()
    calls: list[str] = []
    assert dispatch.resolve_context_returns(conn, lambda rid, ev: calls.append(rid) or True) == 0
    assert calls == []


def test_dispatch_return_does_not_resume_human_wait(env):
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    _running_step(conn, source["id"], "A.scope", "R-original", "A")
    now = now_iso()
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?, 'dev', 'mention', ?, ?)",
        (source["id"], '{"target":"human","message":"请确认"}', now))
    conn.commit()
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="独立工作", requester="qa", source_slice_key="A")
    target_id = result["task"]["id"]
    conn.execute("UPDATE tasks SET status='in_review' WHERE id=?", (target_id,))
    conn.commit()
    tasks_service.review(conn, target_id, True, "human")
    row = conn.execute(
        "SELECT status FROM task_dispatches WHERE target_task_id=?", (target_id,)).fetchone()
    assert row["status"] == "no_return"
    note = conn.execute("SELECT wake_note FROM runs WHERE id='R-original'").fetchone()[0]
    assert not note


def test_record_context_return_recovers_idle_release_run(env):
    conn = env
    task = tasks_service.create(conn, title="B 片", assignee="qa")
    _running_step(conn, task["id"], "B.execute", "R-b", "B")
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,started_at) "
        "VALUES ('R-a','dev',?, 'running', ?)", (task["id"], now))
    conn.commit()
    dispatch.record_context_return(
        conn, switched_run_id="R-a",
        return_context_key=f"{task['id']}::slice::B", return_run_id=None)
    row = conn.execute(
        "SELECT context_return_run_id FROM runs WHERE id='R-a'").fetchone()
    assert row[0] == "R-b"


def test_dispatch_completion_notification_is_idempotent(env):
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    _running_step(conn, source["id"], "A.scope", "R-original", "A")
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="独立工作", requester="qa", source_slice_key="A")
    target_id = result["task"]["id"]
    tasks_service.claim(conn, target_id, "dev")
    tasks_service.submit(conn, target_id, summary="完成", role="dev")
    conn.execute("UPDATE task_dispatches SET status='return_ready' WHERE target_task_id=?",
                 (target_id,))
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM activities WHERE task_id=? AND kind='mention'",
        (source["id"],)).fetchone()["n"]
    dispatch.on_task_done(conn, target_id)
    count2 = conn.execute(
        "SELECT COUNT(*) AS n FROM activities WHERE task_id=? AND kind='mention'",
        (source["id"],)).fetchone()["n"]
    assert count2 == count
