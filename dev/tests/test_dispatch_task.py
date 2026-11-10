"""独立派遣任务：顶层任务、软关联、新 run、幂等与深度保护。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import dispatch
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    from anvil.services import daemon as daemon_service
    monkeypatch.setattr(daemon_service, "get_manager", lambda: None)
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    for slug in ("dev", "qa"):
        conn.execute(
            "INSERT INTO roles (slug,display_name,home_path,definition_json,"
            "created_at,updated_at,enabled) VALUES (?,?,?,'{}',?,?,1)",
            (slug, slug.upper(), str(data / "agents" / slug), now, now))
    conn.commit()
    yield conn
    conn.close()


def test_dispatch_creates_independent_top_level_task(env):
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", repo="original-repo")
    now = now_iso()
    conn.execute(
        "INSERT INTO role_sessions "
        "(role_slug,session_id,status,current_task_id,current_context_key,"
        " current_slice_key,updated_at) VALUES ('dev','sid','busy',?,?,?,?)",
        (source["id"], f"{source['id']}::slice::A", "A", now))
    conn.commit()
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="修复另一个领域的缓存问题", requester="qa",
        source_slice_key="A", acceptance=["问题复现", "修复完成"],
        scope_paths=["src/cache"], reason_code="explicit")
    task = result["task"]
    run = result["run"]
    record = result["dispatch"]

    assert task["id"] != source["id"]
    assert task["parent_id"] is None
    assert task["parent_step_id"] is None
    assert task["type"] == "dispatched"
    assert task["assignee"] == "dev"
    assert task["source_task_id"] == source["id"]
    assert task["source_slice_key"] == "A"
    assert task["requested_by"] == "qa"
    assert task["dispatch_status"] == "open"
    # 不继承来源任务的 repo/workspace。
    assert task["repo"] is None
    assert task["depends_on"] == []
    assert run["task_id"] == task["id"]
    assert run["slice_key"] is None
    assert run["origin"] == "dispatch"
    assert run["dispatch_id"] == record["id"]
    assert record["status"] == "open"
    assert record["return_context_key"] is not None

    acts = conn.execute(
        "SELECT kind, payload_json FROM activities WHERE task_id=? "
        "AND kind='dispatch'", (source["id"],)).fetchall()
    assert len(acts) == 1

    # 来源任务不被阻塞：仍可正常提交/推进（无 parent_step 绑定）。
    assert tasks_service.get(conn, source["id"])["status"] == "todo"


def test_dispatch_is_idempotent_for_same_request(env):
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    first = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="同一个请求", requester="qa")
    second = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="同一个请求", requester="qa")
    assert second["reused"] is True
    assert second["task"]["id"] == first["task"]["id"]
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE source_task_id=?",
        (source["id"],)).fetchone()["n"]
    assert count == 1


def test_dispatch_rejects_terminal_source_and_non_participant(env):
    conn = env
    source = tasks_service.create(conn, title="来源任务", assignee="qa")
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (source["id"],))
    conn.commit()
    with pytest.raises(dispatch.DispatchError):
        dispatch.create_dispatch(
            conn, source_task_id=source["id"], target_role="dev",
            message="x", requester="qa")

    source2 = tasks_service.create(conn, title="来源任务2", assignee="qa")
    with pytest.raises(dispatch.DispatchError):
        dispatch.create_dispatch(
            conn, source_task_id=source2["id"], target_role="dev",
            message="x", requester="dev")


def test_dispatch_depth_limit(env, monkeypatch):
    conn = env
    monkeypatch.setattr(config, "DISPATCH_MAX_DEPTH", 1)
    source = tasks_service.create(conn, title="来源", assignee="qa")
    first = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="第一层", requester="qa")
    with pytest.raises(dispatch.DispatchError):
        dispatch.create_dispatch(
            conn, source_task_id=first["task"]["id"], target_role="dev",
            message="第二层", requester="human")


def test_dispatch_captures_return_point_after_idle_release(env):
    """进程空闲释放清空 current_* 后，仍能从最近槽/suspended run 找回返回点。"""
    from anvil.services import runs as runs_service

    conn = env
    source = tasks_service.create(conn, title="来源", assignee="qa")
    now = now_iso()
    conn.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (source["id"],))
    conn.execute(
        "INSERT INTO role_task_sessions "
        "(role_slug,task_key,task_id,slice_key,session_id,status,created_at,updated_at,"
        " last_active_at) VALUES ('dev',?,?,'A','sid','idle',?,?,?)",
        (f"{source['id']}::slice::A", source["id"], now, now, now))
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,slice_key,step_id,started_at) "
        "VALUES ('R-return','dev',?,'suspended','A','A.scope',?)",
        (source["id"], now))
    conn.commit()

    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="独立工作", requester="qa", source_slice_key="A")
    record = result["dispatch"]
    assert record["return_context_key"] == f"{source['id']}::slice::A"
    assert record["return_run_id"] == "R-return"
    assert record["return_step_id"] == "A.scope"
