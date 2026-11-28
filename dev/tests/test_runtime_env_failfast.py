"""P0-B：工具级 EPERM/ENOENT 启动失败 → runtime_env_error（不是 suspend）。"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from anvil.db import connect, open_conn
from anvil.services import runs as runs_service
from anvil.services import runtime_errors
from anvil.services import sandbox as sandbox_service
from anvil.services import workflow as workflow_service
from anvil.util import now_iso

from tests.graph.conftest import make_env


def _user_tool_result(text: str, *, is_error: bool = True) -> dict:
    return {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result", "is_error": is_error,
            "content": [{"type": "text", "text": text}],
        }]},
    }


def test_classifies_eperm_mkdir_private_tmp_as_runtime_env_error():
    event = _user_tool_result(
        "EPERM: operation not permitted, mkdir '/private/tmp/claude-501/"
        "-Users-ikaruga-cctest-ws'")
    reason = runtime_errors.runtime_env_error_from_event(event)
    assert reason
    assert "EPERM" in reason


def test_classifies_sandbox_deny_as_runtime_env_error():
    event = _user_tool_result(
        "Sandbox: claude.exe deny(1) file-write-create /private/tmp/claude-501/x")
    assert runtime_errors.runtime_env_error_from_event(event)


def test_ignores_business_tool_errors():
    event = _user_tool_result("1 failed, 2 passed - AssertionError: expected 3")
    assert runtime_errors.runtime_env_error_from_event(event) is None
    # 非错误 tool_result 也不算
    ok = _user_tool_result("EPERM: operation not permitted, mkdir /private/tmp/claude-1",
                           is_error=False)
    assert runtime_errors.runtime_env_error_from_event(ok) is None


def _mk_run(env, *, status="running", suspend_kind=None):
    conn = open_conn(env["db_path"])
    try:
        rid = f"R-{time.time_ns()}"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO runs (id, role_slug, status, started_at, suspend_kind) "
            "VALUES (?,?,?,?,?)", (rid, "dev", status, now, suspend_kind))
        conn.commit()
        return rid
    finally:
        conn.close()


def _get(env, rid):
    conn = open_conn(env["db_path"])
    try:
        return runs_service.get_run(conn, rid)
    finally:
        conn.close()


def test_process_exit_fails_run_with_visible_reason(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    rid = _mk_run(env)
    env["orch"].pump(env["db_path"], rid, "process_exit", {
        "exit_code": 0,
        "failure_class": "runtime_env_error",
        "failure_message": "EPERM: operation not permitted, mkdir '/private/tmp/claude-501'",
    })
    run = _get(env, rid)
    assert run["status"] == "failed"
    assert run["failure_class"] == "runtime_env_error"
    assert "EPERM" in run["failure_message"]
    conn = open_conn(env["db_path"])
    try:
        body = conn.execute(
            "SELECT payload_json FROM activities WHERE run_id=? AND kind='comment' "
            "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()["payload_json"]
    finally:
        conn.close()
    assert "运行时环境错误" in body
    assert "self-check --force" in body


def test_runtime_env_error_fails_even_with_zero_exit_code(tmp_path, monkeypatch):
    """E3/E4 现场：CLI 退出码 0，但 Bash 工具没执行——必须失败而不是 abandoned。"""
    env = make_env(tmp_path, monkeypatch)
    rid = _mk_run(env)
    env["orch"].pump(env["db_path"], rid, "process_exit", {
        "exit_code": 0, "failure_class": "runtime_env_error",
        "failure_message": "silent degradation",
    })
    assert _get(env, rid)["status"] == "failed"


def test_spawn_refuses_and_fails_run_before_starting_process(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sandbox_service, "runtime_dispatch_guard",
        lambda *a, **kw: (False, "运行时 dev 不可用：EPERM mkdir claude-501"))
    conn = open_conn(env["db_path"])
    run = runs_service.create_run(conn, "dev", task_id=None)
    conn.close()
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    r = _get(env, run["id"])
    assert r["status"] == "failed"
    assert r["failure_class"] == "runtime_env_error"
    assert "EPERM" in r["failure_message"]
    assert env["fake"].spawned == []


def test_advance_refuses_to_start_step_when_runtime_unavailable(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "wf.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.commit()
    monkeypatch.setattr(
        sandbox_service, "runtime_dispatch_guard",
        lambda *a, **kw: (False, "运行时 dev 不可用：EPERM mkdir claude-501"))
    wf = workflow_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"}]})
    started: list = []
    result = workflow_service.advance(
        conn, wf["id"], starter=lambda *a: started.append(a) or {"id": "run-x"})
    assert result["started"] == []
    assert started == []
    step = conn.execute(
        "SELECT status, failure_reason FROM workflow_steps WHERE workflow_id=?",
        (wf["id"],)).fetchone()
    assert step["status"] == "pending"
    assert "EPERM" in step["failure_reason"]
    conn.close()


def test_suspend_kind_separates_waiting_human_from_environment(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    try:
        waiting = _mk_run(env, status="suspended", suspend_kind="waiting_human")
        broken = _mk_run(env, status="suspended", suspend_kind="environment")
        assert runs_service.suspended_auto_recoverable(
            conn, {"task_id": None, "role_slug": "dev",
                   "suspend_kind": "waiting_human"}) is False
        assert runs_service.suspended_auto_recoverable(
            conn, {"task_id": None, "role_slug": "dev",
                   "suspend_kind": "environment"}) is True
        assert waiting != broken
    finally:
        conn.close()
