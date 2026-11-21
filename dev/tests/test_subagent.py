"""运行时原生 subagent 的观测（不是 Anvil 任务）。"""
from __future__ import annotations

import pytest

from anvil.db import connect, open_conn
from anvil.services.daemon import DaemonManager
from anvil.util import ConflictError


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


def test_projection_protocol_injects_only_for_marked_step_and_capable_runtime(tmp_path):
    from anvil.graph import nodes, render
    from anvil.services import runtime as runtime_service
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "hint.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,description,status,created_at,updated_at) "
                 "VALUES ('T1','审计','有描述的审计任务','todo',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", {
        "steps": [{"id": "S1", "title": "复核", "role": "auditor",
                   "subagent_hint": True}]})
    runtime_service.register(
        conn, id="cap", display_name="cap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": True})
    runtime_service.register(
        conn, id="nocap", display_name="nocap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})

    run = {"role_slug": "auditor", "task_id": "T1", "workflow_id": wf["id"],
           "step_id": "S1", "prompt": "复核上一角色提出的多个 condition",
           "profile_snapshot": None}
    effective = {"projection": {"runtime": {"id": "cap"}}}
    protocol = nodes._projection_protocol(conn, run, effective)
    assert render.SUBAGENT_PROTOCOL in protocol
    assert "条件化只读调查" in protocol
    # 普通步骤不注入安全审计 checker 话术。
    assert render.SUBAGENT_AUDIT_PROTOCOL not in protocol

    # 问题板 reviewer 的 hint step 才追加审计 checker 协议。
    conn.execute("UPDATE tasks SET issue_board_enabled=1 WHERE id='T1'")
    conn.execute(
        "INSERT INTO task_reviewers (task_id, role_slug, source, added_at) "
        "VALUES ('T1','auditor','human',?)", (now_iso(),))
    conn.commit()
    reviewer_protocol = nodes._projection_protocol(conn, run, effective)
    assert render.SUBAGENT_AUDIT_PROTOCOL in reviewer_protocol
    assert "verdict" in reviewer_protocol

    # 未标记的 step 不注入。
    conn.execute("UPDATE workflow_steps SET subagent_hint=0 WHERE workflow_id=? AND step_id='S1'",
                 (wf["id"],))
    conn.commit()
    assert render.SUBAGENT_PROTOCOL not in nodes._projection_protocol(conn, run, effective)

    # 标记但当运行时能力不足时不注入。
    conn.execute("UPDATE workflow_steps SET subagent_hint=1 WHERE workflow_id=? AND step_id='S1'",
                 (wf["id"],))
    conn.commit()
    unsupported = {"projection": {"runtime": {"id": "nocap"}}}
    assert render.SUBAGENT_PROTOCOL not in nodes._projection_protocol(conn, run, unsupported)

    # 非 step run 不注入。
    reply_run = {**run, "step_id": None}
    assert render.SUBAGENT_PROTOCOL not in nodes._projection_protocol(conn, reply_run, effective)
    conn.close()


def test_subagent_protocol_text_is_condition_based():
    from anvil.graph import render

    text = render.SUBAGENT_PROTOCOL
    for needle in ("拆", "不拆", "单一明确问题", "conclusion", "confidence",
                   "evidence", "open_questions", "LSP", "tree-sitter", "grep",
                   "turn 级", "不会跨 turn"):
        assert needle in text, needle
    # 非审计步骤不被 checker 的 verdict/TP/FP 话术绑死。
    assert "verdict" not in text
    assert "TP" not in text

    audit = render.SUBAGENT_AUDIT_PROTOCOL
    for needle in ("condition_id", "verdict", "call_chain", "TP", "FP", "不投票"):
        assert needle in audit, needle
    assert "局部实现" not in text
    assert "并行实现" not in render.DELEGATION_PROTOCOL
    assert "subagent_hint" in render.PLANNING_PROTOCOL
    assert "verify/verify2/fix" in render.PLANNING_PROTOCOL
    assert "Roster" in render.REVISION_PROTOCOL
    assert "subagent_hint" in render.REVISION_PROTOCOL


def test_loop_phase_subagent_hint_propagates_to_generated_steps(tmp_path):
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "loop.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','切片','todo',?,?)", (now, now))
    conn.commit()
    loop = {
        "from": "S1",
        "execute": {"role": "dev"},
        "verify": {"role": "auditor", "subagent_hint": True},
        "fix": {"role": "dev"},
        "decide": {"role": "lead"},
    }
    generated = wf_service._dynamic_slice_steps(
        conn, "S1", loop, [{"id": "A", "title": "A", "execute_steps": [{}]}],
        existing_ids=set(), weight="standard")
    by_suffix = {g["id"].split(".")[-1]: g for g in generated}
    assert by_suffix["verify"]["subagent_hint"] == 1
    assert by_suffix["execute"]["subagent_hint"] == 0
    assert by_suffix["decide"]["subagent_hint"] == 0
    conn.close()


def _loop_definition(**hints):
    """最小 plan_slices + loop 定义；hints 以 phase=bool 覆盖 subagent_hint。"""
    loop = {
        "from": "P1",
        "execute": {"role": "developer"},
        "verify": {"role": "tester"},
        "fix": {"role": "developer"},
        "verify2": {"role": "tester"},
        "decide": {"role": "pm"},
    }
    for phase, value in hints.items():
        loop[phase]["subagent_hint"] = value
    return {
        "steps": [
            {"id": "P1", "title": "切片", "role": "pm", "kind": "plan_slices",
             "instructions": "盘点并切片", "acceptance": ["可独立闭环"]},
            {"id": "F1", "title": "报告", "role": "pm", "depends_on": ["P1"],
             "instructions": "汇总", "acceptance": ["报告完整"]},
        ],
        "loop": loop,
    }


def test_agent_loop_draft_requires_explicit_subagent_decision(tmp_path):
    """Cindy 漏写 subagent_hint 时草案必须被拒绝，而不是静默按 false 落库。"""
    from anvil.services import board as board_service
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "require-hint.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','审计','todo',?,?)", (now, now))
    conn.commit()

    with pytest.raises(ConflictError, match="subagent_hint"):
        board_service.create_draft(
            conn, goal="审计", tree=_loop_definition(), created_by="agent:cindy",
            source_task="T1", kind="workflow")

    # 显式写 false 也算完成决策，不会被拒绝。
    draft = board_service.create_draft(
        conn, goal="审计",
        tree=_loop_definition(execute=False, verify=False, fix=False,
                              verify2=False, decide=False),
        created_by="agent:cindy", source_task="T1", kind="workflow")
    assert draft["status"] == "pending"
    assert "subagent_hint" in draft["tree"]["loop"]["verify"]
    conn.close()


def test_runtime_slice_plan_cannot_set_subagent_hint(tmp_path):
    """运行期切片计划不能覆盖 Cindy 的提示编排。"""
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "slice-hint.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','切片','todo',?,?)", (now, now))
    conn.commit()
    loop = {
        "from": "S1",
        "execute": {"role": "dev", "subagent_hint": False},
        "verify": {"role": "auditor", "subagent_hint": True},
        "decide": {"role": "lead", "subagent_hint": False},
    }
    with pytest.raises(ConflictError, match="subagent_hint"):
        wf_service._dynamic_slice_steps(  # noqa: SLF001
            conn, "S1", loop,
            [{"id": "A", "title": "A", "subagent_hint": True}],
            existing_ids=set(), weight="standard")
    conn.close()


def test_non_boolean_subagent_hint_is_rejected(tmp_path):
    """`\"false\"` 这类 JSON 字符串不能按 truthy 当成 true。"""
    from anvil.services import board as board_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "bool-hint.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','审计','todo',?,?)", (now, now))
    conn.commit()
    tree = _loop_definition(execute=False, verify=False, fix=False,
                            verify2=False, decide=False)
    tree["loop"]["verify"]["subagent_hint"] = "false"
    with pytest.raises(ConflictError, match="boolean"):
        board_service.create_draft(
            conn, goal="审计", tree=tree, created_by="agent:cindy",
            source_task="T1", kind="workflow")
    conn.close()


def test_human_loop_draft_may_omit_subagent_decision(tmp_path):
    """人工草案保持兼容：不强制显式 hint（缺省按 false 处理）。"""
    from anvil.services import board as board_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "human-hint.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','审计','todo',?,?)", (now, now))
    conn.commit()
    draft = board_service.create_draft(
        conn, goal="审计", tree=_loop_definition(), created_by="human",
        source_task="T1", kind="workflow")
    assert draft["status"] == "pending"
    conn.close()


def test_supports_subagents_prefers_effective_snapshot(tmp_path):
    from anvil.graph import nodes
    from anvil.services import runtime as runtime_service

    conn = connect(str(tmp_path / "cap.db"))
    runtime_service.register(
        conn, id="cap", display_name="cap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})
    run = {"role_slug": "dev", "profile_snapshot": None}
    effective = {"projection": {
        "runtime": {"id": "cap"},
        "runtime_capabilities": {"supports_subagents": True},
    }}
    assert nodes._supports_subagents(conn, run, effective) is True
    conn.close()


def test_subagent_protocol_survives_task_description_in_projection(tmp_path):
    """协议走 ## 协议 段，不依赖会被 task.description 覆盖的 prompt 正文。"""
    from anvil.graph import render

    target = tmp_path / "CLAUDE.md"
    render.project_instruction(
        target,
        "",
        {"title": "审计", "description": "任务有描述，不能吞掉协议"},
        prompt="普通任务正文",
        protocol=render.SUBAGENT_PROTOCOL,
    )
    text = target.read_text(encoding="utf-8")
    assert "## 协议" in text
    assert "条件化只读调查" in text
    assert "conclusion" in text


def _seed_run(conn, *, run_id="R1", status="running", session="sess-1"):
    from anvil.util import now_iso
    now = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','t','in_progress',?,?)", (now, now))
    conn.execute(
        "INSERT OR REPLACE INTO runs (id,role_slug,task_id,status,native_session_id,"
        "slice_key,step_id,started_at) VALUES (?,?,?,?,?,'A','S1',?)",
        (run_id, "dev", "T1", status, session, now))
    conn.commit()


def _tool_use(tool_id="tu-1", name="Task"):
    return {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": tool_id, "name": name,
        "input": {"subagent_type": "Explore", "description": "搜索调用点",
                  "prompt": "查找所有调用点"},
    }]}}


def _tool_result(tool_id="tu-1", text="找到 3 处调用"):
    return {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": tool_id,
        "content": [{"type": "text", "text": text}],
    }]}}


def test_subagent_records_full_runtime_attribution(tmp_path):
    conn, mgr = _manager(tmp_path)
    _seed_run(conn)
    role = mgr._role("dev")
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    role.current_step_id = "S1"
    role.proc_generation = 3
    role.session_id = "sess-1"

    mgr._track_subagent(role, _tool_use())  # noqa: SLF001

    row = open_conn(str(tmp_path / "sub.db")).execute(
        "SELECT * FROM run_subagents WHERE run_id='R1'").fetchone()
    assert row["subagent_type"] == "Explore"
    assert row["proc_generation"] == 3
    assert row["native_session_id"] == "sess-1"
    assert row["slice_key"] == "A"
    assert row["step_id"] == "S1"
    conn.close()


def test_turn_end_marks_running_subagent_interrupted(tmp_path):
    conn, mgr = _manager(tmp_path)
    _seed_run(conn)
    role = mgr._role("dev")
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    mgr._track_subagent(role, _tool_use())  # noqa: SLF001

    mgr._finish_subagents(conn, role, "R1")  # noqa: SLF001

    row = conn.execute(
        "SELECT status, interrupt_reason FROM run_subagents WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "interrupted"
    assert row["interrupt_reason"] == "turn_ended"
    assert role.active_subagents == {}
    conn.close()


def test_late_tool_result_keeps_interrupted_but_records_result(tmp_path):
    conn, mgr = _manager(tmp_path)
    _seed_run(conn)
    role = mgr._role("dev")
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    mgr._track_subagent(role, _tool_use())  # noqa: SLF001
    mgr._finish_subagents(conn, role, "R1")  # noqa: SLF001

    mgr._track_subagent(role, _tool_result())  # noqa: SLF001 —— 内存已清，走晚到路径

    row = conn.execute(
        "SELECT status, interrupt_reason, result_summary FROM run_subagents "
        "WHERE run_id='R1'").fetchone()
    assert row["status"] == "interrupted"
    assert row["interrupt_reason"] == "turn_ended"
    assert "找到 3 处" in row["result_summary"]
    conn.close()


def test_late_tool_result_completes_still_running_row(tmp_path):
    conn, mgr = _manager(tmp_path)
    _seed_run(conn)
    role = mgr._role("dev")
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    mgr._track_subagent(role, _tool_use())  # noqa: SLF001
    role.active_subagents.clear()  # 模拟内存丢失但 turn 未结束

    mgr._track_subagent(role, _tool_result())  # noqa: SLF001

    row = conn.execute(
        "SELECT status, result_summary FROM run_subagents WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert "找到 3 处" in row["result_summary"]
    conn.close()


def test_repeated_tool_use_does_not_reset_terminal_state(tmp_path):
    conn, mgr = _manager(tmp_path)
    _seed_run(conn)
    role = mgr._role("dev")
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    mgr._track_subagent(role, _tool_use())  # noqa: SLF001
    mgr._track_subagent(role, _tool_result())  # noqa: SLF001
    ended = conn.execute(
        "SELECT ended_at FROM run_subagents WHERE run_id='R1'").fetchone()["ended_at"]

    mgr._track_subagent(role, _tool_use())  # noqa: SLF001 —— 重复事件

    row = conn.execute(
        "SELECT status, ended_at, result_summary FROM run_subagents WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert row["ended_at"] == ended
    assert "找到 3 处" in row["result_summary"]
    conn.close()


def test_daemon_restart_heals_running_subagents(tmp_path):
    conn, mgr = _manager(tmp_path)
    _seed_run(conn)
    role = mgr._role("dev")
    role.current_run_id = "R1"
    role.current_task_id = "T1"
    mgr._track_subagent(role, _tool_use())  # noqa: SLF001

    mgr._heal_subagents()  # noqa: SLF001 —— 进程重启后不存在存活子代理

    row = conn.execute(
        "SELECT status, interrupt_reason FROM run_subagents WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "interrupted"
    assert row["interrupt_reason"] == "daemon_restart"
    conn.close()


def test_subagent_state_is_not_read_by_scheduling_decisions():
    """运行时 subagent 只可观测：调度/工作流/租约/上下文清单不得读取其状态。"""
    import inspect
    from pathlib import Path
    from anvil import scheduler as scheduler_mod
    from anvil.services import context_manifest, leases, workflow

    decision_sources = [
        inspect.getsource(scheduler_mod.Scheduler._awaiting_external_work),
        inspect.getsource(scheduler_mod.Scheduler._nudge_stalled),
        inspect.getsource(scheduler_mod.Scheduler._advance_workflows),
        Path(inspect.getsourcefile(workflow)).read_text(encoding="utf-8"),
        Path(inspect.getsourcefile(leases)).read_text(encoding="utf-8"),
        Path(inspect.getsourcefile(context_manifest)).read_text(encoding="utf-8"),
    ]
    for source in decision_sources:
        assert "run_subagents" not in source
        assert "active_subagents" not in source


def _insert_role_with_runtime(conn, slug: str, runtime_id: str) -> None:
    from anvil.util import now_iso
    now = now_iso()
    conn.execute(
        "INSERT OR REPLACE INTO roles (slug,display_name,home_path,definition_json,"
        "created_at,updated_at,enabled) VALUES (?,?,?,?,?,?,1)",
        (slug, slug, f"/tmp/{slug}",
         __import__("json").dumps({"runtime": {"id": runtime_id}}), now, now))
    conn.commit()


def test_unsupported_runtime_downgrades_flat_step_hint_with_audit(tmp_path):
    from anvil.services import runtime as runtime_service
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "downgrade-flat.db"))
    now = now_iso()
    runtime_service.register(
        conn, id="nocap", display_name="nocap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})
    _insert_role_with_runtime(conn, "dev", "nocap")
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.commit()

    wf = wf_service.create_workflow(conn, "T1", {"steps": [{
        "id": "S1", "title": "x", "role": "dev", "subagent_hint": True}]},
        enforce_role_coverage=False)
    step = conn.execute(
        "SELECT subagent_hint FROM workflow_steps WHERE workflow_id=? AND step_id='S1'",
        (wf["id"],)).fetchone()
    assert step["subagent_hint"] == 0
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='subagent_hint.downgraded' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert event is not None and "nocap" in event["payload_json"]
    conn.close()


def test_unsupported_runtime_downgrades_loop_phases(tmp_path):
    from anvil.services import runtime as runtime_service
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "downgrade-loop.db"))
    now = now_iso()
    runtime_service.register(
        conn, id="nocap", display_name="nocap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})
    for slug in ("dev", "auditor", "lead"):
        _insert_role_with_runtime(conn, slug, "nocap")
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','切片','todo',?,?)", (now, now))
    conn.commit()

    definition = {
        "steps": [{"id": "P1", "title": "切片", "role": "lead",
                   "kind": "plan_slices", "instructions": "切",
                   "acceptance": ["可闭环"]}],
        "loop": {
            "from": "P1",
            "execute": {"role": "dev", "subagent_hint": True},
            "verify": {"role": "auditor", "subagent_hint": True},
            "decide": {"role": "lead", "subagent_hint": True},
        },
    }
    wf = wf_service.create_workflow(
        conn, "T1", definition, enforce_role_coverage=False)
    stored_loop = wf["definition"]["loop"]
    for phase in ("execute", "verify", "decide"):
        assert stored_loop[phase]["subagent_hint"] is False, phase

    generated = wf_service._dynamic_slice_steps(
        conn, "P1", stored_loop, [{"id": "A", "title": "A"}],
        existing_ids=set(), weight="standard")
    assert all(g["subagent_hint"] == 0 for g in generated)

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM events "
        "WHERE event_type='subagent_hint.downgraded'").fetchone()["n"]
    assert count >= 3
    conn.close()


def test_supported_runtime_keeps_hint(tmp_path):
    from anvil.services import runtime as runtime_service
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "keep-hint.db"))
    now = now_iso()
    runtime_service.register(
        conn, id="cap", display_name="cap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": True})
    _insert_role_with_runtime(conn, "dev", "cap")
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.commit()

    wf = wf_service.create_workflow(conn, "T1", {"steps": [{
        "id": "S1", "title": "x", "role": "dev", "subagent_hint": True}]},
        enforce_role_coverage=False)
    step = conn.execute(
        "SELECT subagent_hint FROM workflow_steps WHERE workflow_id=? AND step_id='S1'",
        (wf["id"],)).fetchone()
    assert step["subagent_hint"] == 1
    assert conn.execute(
        "SELECT 1 FROM events WHERE event_type='subagent_hint.downgraded'"
    ).fetchone() is None
    conn.close()


def test_planner_roster_exposes_subagent_capability(tmp_path):
    from anvil.services import planning
    from anvil.services import runtime as runtime_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "roster.db"))
    now = now_iso()
    runtime_service.register(
        conn, id="nocap", display_name="nocap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})
    _insert_role_with_runtime(conn, "dev", "nocap")
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.execute("INSERT INTO task_assignees (task_id, role_slug, joined_at) "
                 "VALUES ('T1','dev',?)", (now,))
    conn.commit()

    roster = planning.build_roster(conn, "T1", ["dev"])
    assert roster[0]["runtime"]["supports_subagents"] is False
    text = planning.render_roster(conn, "T1", roster)
    assert "不支持 subagent" in text
    conn.close()


def test_revision_steps_downgrade_unsupported_hint(tmp_path):
    from anvil.services import runtime as runtime_service
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "revision-hint.db"))
    now = now_iso()
    runtime_service.register(
        conn, id="nocap", display_name="nocap", executable_path="/bin/true",
        status="ready", capabilities={"supports_subagents": False})
    _insert_role_with_runtime(conn, "dev", "nocap")
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", {"steps": [{
        "id": "S1", "title": "x", "role": "dev"}]},
        enforce_role_coverage=False)

    wf_service._insert_revision_steps(conn, wf["id"], [{
        "id": "R1", "title": "新增", "role": "dev", "subagent_hint": True,
        "depends_on": [], "acceptance": [], "kind": "task",
    }], now)
    conn.commit()

    row = conn.execute(
        "SELECT subagent_hint FROM workflow_steps WHERE workflow_id=? AND step_id='R1'",
        (wf["id"],)).fetchone()
    assert row["subagent_hint"] == 0
    assert conn.execute(
        "SELECT 1 FROM events WHERE event_type='subagent_hint.downgraded' "
        "AND workflow_id=?", (wf["id"],)).fetchone() is not None
    conn.close()


def test_hint_true_with_runtime_drift_audits_skip_once(tmp_path):
    """执行侧 runtime 不再支持时不注入协议，并写一次性 subagent_hint.skipped。"""
    from anvil.graph import nodes, render
    from anvil.services import workflow as wf_service
    from anvil.util import now_iso

    conn = connect(str(tmp_path / "drift.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", {"steps": [{
        "id": "S1", "title": "x", "role": "dev", "subagent_hint": True}]},
        enforce_role_coverage=False)
    run = {"id": "R1", "role_slug": "dev", "task_id": "T1",
           "workflow_id": wf["id"], "step_id": "S1"}
    effective = {"projection": {"runtime": {"id": "nocap"}}}

    protocol = nodes._projection_protocol(conn, run, effective)
    assert render.SUBAGENT_PROTOCOL not in protocol
    nodes._projection_protocol(conn, run, effective)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM events "
        "WHERE event_type='subagent_hint.skipped'").fetchone()["n"]
    assert count == 1
    conn.close()
