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
    for needle in ("拆", "不拆", "单一 condition", "verdict", "confidence",
                   "evidence", "call_chain", "open_questions", "条件逻辑",
                   "不投票", "LSP", "tree-sitter", "grep"):
        assert needle in text, needle
    assert "局部实现" not in text
    assert "并行实现" not in render.DELEGATION_PROTOCOL
    assert "subagent_hint" in render.PLANNING_PROTOCOL
    assert "verify/verify2/fix" in render.PLANNING_PROTOCOL


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
    assert "verdict" in text
