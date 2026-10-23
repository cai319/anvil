"""@ 反馈进入步骤/回复上下文：步骤 run 注入最近会话；回复 run 用 REPLY_PROTOCOL。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.util import now_iso

pytest.importorskip("anvil.graph")
from anvil.graph import nodes, render  # noqa: E402
from anvil.services import runs as runs_service  # noqa: E402
from anvil.services import tasks as tasks_service  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "p.db"))
    now = now_iso()
    c.execute("INSERT INTO tasks (id,title,description,status,created_at,updated_at) "
              "VALUES ('T1','跳棋','实现跳棋','in_progress',?,?)", (now, now))
    c.commit()
    yield c
    c.close()


def test_step_prompt_includes_recent_mentions(conn):
    tasks_service.mention(conn, "T1", "designer", "developer", "接口字段少了 moveId，请确认")
    tasks_service.mention(conn, "T1", "developer", "designer", "已补上，见 v2 接口文档")
    run = runs_service.create_run(conn, "developer", "T1",
                                  prompt="## 工作流步骤 S3：实现引擎",
                                  step_id="S3", workflow_id="wf-1")
    task = {"title": "跳棋", "description": "实现跳棋"}
    prompt = nodes._prompt_for_run(conn, run, task)
    assert "接口字段少了 moveId" in prompt          # 未读反馈进入上下文
    assert "## 未读会话" in prompt
    assert "anvil inbox check" in prompt            # 拉模式指针
    assert "交叉确认" in prompt                     # 挑战式协作协议
    assert "anvil step submit" in prompt


def test_reply_prompt_uses_reply_protocol(conn):
    run = runs_service.create_run(
        conn, "developer", "T1",
        prompt="[anvil:reply]你在任务会话中被 pm @：\n验收口径有歧义，请确认")
    prompt = nodes._prompt_for_run(conn, run, {"title": "跳棋", "description": "实现跳棋"})
    assert "验收口径有歧义" in prompt
    assert "只做讨论" in prompt
    assert "不要 claim 或 submit **当前任务**" in prompt
    assert "另一个任务或子任务" in prompt             # 允许按消息指示 claim 子任务
    assert "anvil step submit" not in prompt


def test_protocols_include_attachment_budget():
    """角色协议必须要求自律：不携带大文件/大图，优先片段读取与路径引用。"""
    assert "附件预算" in render.PROTOCOL
    assert "附件预算" in render.STEP_PROTOCOL
    assert "offset+limit" in render.STEP_PROTOCOL
    assert "不要反复读取" in render.STEP_PROTOCOL


def test_step_prompt_uses_agent_inbox_and_stays_small(conn):
    for i in range(60):
        tasks_service.comment(conn, "T1", "pm", f"{i}:" + "长内容" * 100)
    run = runs_service.create_run(conn, "developer", "T1",
                                  prompt="## 工作流步骤 S3：实现", step_id="S3",
                                  workflow_id="wf-1")
    prompt = nodes._prompt_for_run(conn, run, {"title": "跳棋", "description": "实现"})
    assert "## 未读会话" in prompt and "anvil inbox check" in prompt
    assert len(prompt) < 6000  # 不把全量历史 push 进上下文


def test_projected_protocol_matches_run_kind(conn):
    planner = runs_service.create_run(conn, "cindy", "T1", prompt="[anvil:plan]\n请规划")
    assert nodes._protocol_for_run(conn, planner) == render.PLANNING_PROTOCOL

    step = runs_service.create_run(conn, "developer", "T1", prompt="步骤说明",
                                   step_id="S1", workflow_id="wf-1")
    assert nodes._protocol_for_run(conn, step) == render.STEP_PROTOCOL

    normal = runs_service.create_run(conn, "developer", "T1", prompt="普通任务")
    assert nodes._protocol_for_run(conn, normal) == render.PROTOCOL


def test_project_instruction_uses_given_protocol(tmp_path):
    target = tmp_path / "CLAUDE.md"
    render.project_instruction(target, "", {"title": "t"}, protocol=render.STEP_PROTOCOL)
    text = target.read_text(encoding="utf-8")
    assert "anvil step submit" in text
    assert "第一步必须执行 `anvil task claim" not in text
