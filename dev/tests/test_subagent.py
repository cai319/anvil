"""运行时原生 subagent 的观测（不是 Anvil 任务）。"""
from __future__ import annotations

from anvil.db import connect, open_conn
from anvil.services.daemon import DaemonManager


def _manager(tmp_path):
    conn = connect(str(tmp_path / "sub.db"))
    mgr = DaemonManager(str(tmp_path / "sub.db"), enabled=False)
    return conn, mgr


def test_track_subagent_start_and_result(tmp_path):
    conn, mgr = _manager(tmp_path)
    role = mgr._role("dev")  # noqa: SLF001 —— 单测直接驱动事件解析
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    event = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "tu-1", "name": "Task",
            "input": {"subagent_type": "Explore", "description": "搜索调用点",
                      "prompt": "查找所有调用点"},
        }]},
    }
    mgr._track_subagent(role, event)  # noqa: SLF001
    assert role.active_subagents["tu-1"]["subagent_type"] == "Explore"
    assert mgr.status("dev")["active_subagents"][0]["description"] == "搜索调用点"
    row = open_conn(str(tmp_path / "sub.db")).execute(
        "SELECT status, description FROM run_subagents WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "running"

    result = {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "tu-1",
            "content": [{"type": "text", "text": "找到 3 处调用"}],
        }]},
    }
    mgr._track_subagent(role, result)  # noqa: SLF001
    assert "tu-1" not in role.active_subagents
    row = open_conn(str(tmp_path / "sub.db")).execute(
        "SELECT status, result_summary FROM run_subagents WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert "找到 3 处" in row["result_summary"]
    conn.close()


def test_subagent_protocol_is_injected_only_for_capable_runtime(tmp_path):
    from anvil.graph import nodes

    conn = connect(str(tmp_path / "sub.db"))
    from anvil.services import runtime as runtime_service

    runtime_service.register(
        conn, id="cap", display_name="cap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": True})
    runtime_service.register(
        conn, id="nocap", display_name="nocap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})
    run = {"profile_snapshot": None, "role_slug": "dev"}
    effective = {"projection": {"runtime": {"id": "cap"}}}
    assert nodes._supports_subagents(conn, run, effective) is True
    effective["projection"]["runtime"]["id"] = "nocap"
    assert nodes._supports_subagents(conn, run, effective) is False
    conn.close()
