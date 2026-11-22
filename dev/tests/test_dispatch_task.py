"""派遣子任务：来源 task 下的非阻塞 child、共享 workspace、幂等与深度保护。"""
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


def test_dispatch_creates_non_blocking_child_task(env):
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"],
        repo="original-repo")
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
    assert task["parent_id"] == source["id"]
    assert task["parent_step_id"] is None
    assert task["type"] == "dispatch"
    assert task["assignee"] == "dev"
    assert task["source_task_id"] == source["id"]
    assert task["source_slice_key"] == "A"
    assert task["requested_by"] == "qa"
    assert task["dispatch_status"] == "open"
    # 继承来源任务的 repo/workspace；但对话上下文独立。
    assert task["repo"] == "original-repo"
    assert task["depends_on"] == []
    assert run["task_id"] == task["id"]
    assert run["slice_key"] is None
    assert not (run.get("worktree_path") and __import__("pathlib").Path(run["worktree_path"]).exists())
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
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"])
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
    source = tasks_service.create(
        conn, title="来源", assignee="qa", assignees=["qa", "dev"])
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
    source = tasks_service.create(
        conn, title="来源", assignee="qa", assignees=["qa", "dev"])
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


def test_dispatch_workspace_is_shared_with_source(env):
    from anvil.services import task_workspace

    conn = env
    source = tasks_service.create(
        conn, title="共享 workspace", assignee="qa", assignees=["qa", "dev"])
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="共享空间的小切片", requester="qa")
    target = result["task"]
    src_ws = task_workspace.resolve_for_task(conn, source["id"], create=True)
    child_ws = task_workspace.resolve_for_task(conn, target["id"], create=True)
    assert src_ws and child_ws == src_ws
    assert result["run"]["worktree_path"] and not __import__("pathlib").Path(
        result["run"]["worktree_path"]).exists()


def test_dispatch_submit_marks_done_without_review(env):
    conn = env
    source = tasks_service.create(
        conn, title="来源", assignee="qa", assignees=["qa", "dev"])
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="完成后直接 done", requester="qa")
    target = result["task"]
    tasks_service.claim(conn, target["id"], "dev")
    submitted = tasks_service.submit(
        conn, target["id"], summary="派遣结果", role="dev")
    assert submitted["status"] == "done"
    assert submitted["assignee"] == "dev"


def test_parent_submit_is_not_blocked_by_dispatch_child(env):
    conn = env
    source = tasks_service.create(conn, title="父任务", assignee="qa")
    now = now_iso()
    # dev 作为参与者但不参与父任务团队提交。
    conn.execute(
        "INSERT INTO task_role_claims (task_id, role_slug, status, updated_at) "
        "VALUES (?, 'dev', 'pending', ?)", (source["id"], now))
    conn.commit()
    dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="不阻塞父任务", requester="qa")
    tasks_service.claim(conn, source["id"], "qa")
    submitted = tasks_service.submit(
        conn, source["id"], summary="父任务按自己的节奏提交", role="qa")
    assert submitted["status"] == "in_review"


def test_dispatch_creation_rolls_back_on_run_failure(env, monkeypatch):
    from anvil.services import runs as runs_service

    conn = env
    source = tasks_service.create(
        conn, title="来源", assignee="qa", assignees=["qa", "dev"])
    monkeypatch.setattr(runs_service, "create_run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        dispatch.create_dispatch(
            conn, source_task_id=source["id"], target_role="dev",
            message="失败不应留下 task", requester="qa")
    conn.rollback()
    child = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE parent_id=? AND type='dispatch'",
        (source["id"],)).fetchone()["n"]
    assert child == 0


def test_dispatch_target_must_be_source_participant(env):
    conn = env
    source = tasks_service.create(conn, title="来源", assignee="qa")
    with pytest.raises(dispatch.DispatchError) as exc:
        dispatch.create_dispatch(
            conn, source_task_id=source["id"], target_role="dev",
            message="非参与者不能进入共享 workspace", requester="qa")
    assert "participant" in str(exc.value)


def test_migration_reparents_legacy_top_level_dispatch(env):
    from anvil import db as db_module

    conn = env
    source = tasks_service.create(conn, title="来源", assignee="qa")
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,type,status,source_task_id,created_at,updated_at) "
        "VALUES ('T-OLD','旧派遣','dispatched','in_progress',?,?,?)",
        (source["id"], now, now))
    conn.execute(
        "INSERT INTO task_dispatches "
        "(id,source_task_id,target_task_id,target_role,request_hash,reason_code,"
        " reason_json,status,created_at,updated_at) "
        "VALUES ('D-OLD',?,'T-OLD','dev','old','legacy','{}','open',?,?)",
        (source["id"], now, now))
    conn.execute("DELETE FROM settings WHERE key='schema.dispatch_child_v1'")
    conn.commit()
    db_module._migrate_dispatch_child_v1(conn)  # noqa: SLF001
    row = conn.execute(
        "SELECT parent_id, type FROM tasks WHERE id='T-OLD'").fetchone()
    assert row["parent_id"] == source["id"]
    assert row["type"] == "dispatch"


def test_dispatch_blocked_by_governance_gate(env):
    """派遣创建 run 前必须过治理闸门（冻结 / 并发 / token 预算）。"""
    from anvil.services import governance

    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"])
    governance.set_freeze(conn, True)
    with pytest.raises(dispatch.DispatchError, match="governance"):
        dispatch.create_dispatch(
            conn, source_task_id=source["id"], target_role="dev",
            message="冻结期间不应创建派遣", requester="qa")
    assert conn.execute(
        "SELECT 1 FROM tasks WHERE parent_id=? AND type='dispatch'",
        (source["id"],)).fetchone() is None


def test_dispatch_reuses_existing_before_governance_freeze(env):
    """幂等优先：frozen 时重复 dispatch 应返回已有记录，而不是治理错误。"""
    from anvil.services import governance

    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"])
    first = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="同一个请求", requester="qa")
    governance.set_freeze(conn, True)

    second = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="同一个请求", requester="qa")
    assert second["reused"] is True
    assert second["task"]["id"] == first["task"]["id"]


def test_dispatch_notification_failure_can_retry(env, monkeypatch):
    """mention 失败不能写 result_notified_at；scheduler 可补投。"""
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"])
    result = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="需要通知", requester="qa")
    target = result["task"]
    d = result["dispatch"]
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (target["id"],))
    conn.commit()

    original = dispatch.tasks_service.mention
    def boom(*args, **kwargs):
        raise RuntimeError("mention failed")
    monkeypatch.setattr(dispatch.tasks_service, "mention", boom)
    try:
        with pytest.raises(RuntimeError):
            dispatch._notify_dispatch_completed(
                conn, d, target["id"], "done", "no_return", None)
    finally:
        monkeypatch.setattr(dispatch.tasks_service, "mention", original)
    row = conn.execute(
        "SELECT result_notified_at FROM task_dispatches WHERE id=?",
        (d["id"],)).fetchone()
    assert row["result_notified_at"] is None

    assert dispatch.retry_completed_notification(conn, d) is True
    conn.commit()
    row = conn.execute(
        "SELECT result_notified_at FROM task_dispatches WHERE id=?",
        (d["id"],)).fetchone()
    assert row["result_notified_at"]


def test_task_dispatch_status_is_derived_from_dispatch_record(env):
    """task_dispatches.status 是唯一权威；tasks.dispatch_status 不得再作为镜像读取。"""
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"])
    target = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="派生状态", requester="qa")["task"]
    conn.execute(
        "UPDATE task_dispatches SET status='return_ready' WHERE target_task_id=?",
        (target["id"],))
    conn.commit()

    fresh = tasks_service.get(conn, target["id"])
    assert fresh["dispatch_status"] == "return_ready"


def test_task_dispatch_status_none_without_dispatch_record(env):
    conn = env
    task = tasks_service.create(conn, title="普通任务")
    # 模拟旧镜像列残留；没有 dispatch record 时必须以记录为准，返回 none。
    conn.execute("UPDATE tasks SET dispatch_status='open' WHERE id=?", (task["id"],))
    conn.commit()

    assert tasks_service.get(conn, task["id"])["dispatch_status"] == "none"


def test_list_all_derives_dispatch_status(env):
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="qa", assignees=["qa", "dev"])
    target = dispatch.create_dispatch(
        conn, source_task_id=source["id"], target_role="dev",
        message="列表派生", requester="qa")["task"]
    conn.execute(
        "UPDATE task_dispatches SET status='returned' WHERE target_task_id=?",
        (target["id"],))
    conn.commit()

    tasks = {t["id"]: t for t in tasks_service.list_all(conn)}
    assert tasks[target["id"]]["dispatch_status"] == "returned"
