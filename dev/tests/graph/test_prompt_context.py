"""@ 反馈进入步骤/回复上下文：步骤 run 注入最近会话；回复 run 用 REPLY_PROTOCOL。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import memory
from anvil.util import now_iso

pytest.importorskip("anvil.graph")
from anvil.graph import nodes, render  # noqa: E402
from anvil.services import runs as runs_service  # noqa: E402
from anvil.services import tasks as tasks_service  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "p.db"))
    now = now_iso()
    c.execute("INSERT INTO tasks (id,title,description,status,repo,created_at,updated_at) "
              "VALUES ('T1','跳棋','实现跳棋','in_progress','openssl',?,?)", (now, now))
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
    assert nodes._protocol_for_run(conn, planner) == (
        render.PLANNING_PROTOCOL + "\n\n" + render.REVISION_PROTOCOL)

    step = runs_service.create_run(conn, "developer", "T1", prompt="步骤说明",
                                   step_id="S1", workflow_id="wf-1")
    assert nodes._protocol_for_run(conn, step) == render.STEP_PROTOCOL

    normal = runs_service.create_run(conn, "developer", "T1", prompt="普通任务")
    assert nodes._protocol_for_run(conn, normal) == render.PROTOCOL


def test_finding_run_uses_finding_protocol(conn):
    """[anvil:findings] run 注入问题板协议，不能退化成只讨论的 REPLY_PROTOCOL。"""
    run = runs_service.create_run(
        conn, "auditor", "T1",
        prompt="[anvil:findings]问题板需要复核：请补 PoC")
    prompt = nodes._prompt_for_run(conn, run, {"title": "审计", "description": "安全审计"})
    assert "问题板需要复核" in prompt
    assert "anvil finding add" in prompt
    assert "anvil finding complete" in prompt
    assert "只做讨论" not in prompt
    assert nodes._protocol_for_run(conn, run) == render.FINDING_PROTOCOL


def test_reviewer_step_gets_finding_protocol(conn):
    """启用问题板且执行者是 reviewer 时，步骤 run 同时注入填写协议。"""
    now = now_iso()
    conn.execute("UPDATE tasks SET issue_board_enabled=1 WHERE id='T1'")
    conn.execute(
        "INSERT INTO task_reviewers (task_id, role_slug, source, added_at) VALUES (?,?,?,?)",
        ("T1", "auditor", "human", now))
    conn.commit()
    reviewer = runs_service.create_run(conn, "auditor", "T1", prompt="步骤说明",
                                       step_id="S1", workflow_id="wf-1")
    developer = runs_service.create_run(conn, "developer", "T1", prompt="步骤说明",
                                        step_id="S2", workflow_id="wf-1")
    assert "anvil finding add" in nodes._prompt_for_run(
        conn, reviewer, tasks_service.get(conn, "T1"))
    assert "anvil finding add" not in nodes._prompt_for_run(
        conn, developer, tasks_service.get(conn, "T1"))
    assert nodes._protocol_for_run(conn, reviewer) == (
        render.STEP_PROTOCOL + "\n\n" + render.FINDING_PROTOCOL)


def test_step_prompt_includes_knowledge_hint(conn, tmp_path, monkeypatch):
    """warm session 的步骤 prompt 也要有知识库入口与相关标题。"""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn.execute("UPDATE tasks SET tags_json='[\"jwt\"]' WHERE id='T1'")
    conn.commit()
    memory.write_note(
        conn, "developer", "vulns/jwt.md",
        "JWT algorithm confusion pattern", kind="vulnerability_pattern",
        scope="role", tags=["jwt"], repo="openssl")

    run = runs_service.create_run(conn, "developer", "T1",
                                  prompt="## 工作流步骤 S3：实现 JWT 校验",
                                  step_id="S3", workflow_id="wf-1")
    prompt = nodes._prompt_for_run(conn, run, {"title": "跳棋", "description": "实现跳棋"})
    assert "## 可用知识（按需检索）" in prompt
    assert "漏洞模式 1" in prompt
    assert "JWT algorithm confusion" in prompt
    assert "anvil knowledge search" in prompt
    assert len(prompt) < 8000


def test_non_step_prompt_uses_compact_knowledge_hint(conn, tmp_path, monkeypatch):
    """普通/回复/规划 run 只给一行知识入口，避免与 CIC 的完整检索块重复。"""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    memory.write_note(
        conn, "developer", "vulns/jwt.md", "JWT compact hint pattern",
        kind="vulnerability_pattern", scope="role", tags=["jwt"])

    run = runs_service.create_run(conn, "developer", "T1", prompt="普通任务")
    prompt = nodes._prompt_for_run(conn, run, {"title": "跳棋", "description": "实现跳棋"})
    assert "知识库：" in prompt
    assert "## 可用知识" not in prompt
    hint_line = next(line for line in prompt.splitlines() if line.startswith("知识库："))
    assert len(hint_line) <= 320
    assert "anvil knowledge search" in hint_line


def test_context_for_task_excludes_snapshot_and_other_tasks(conn, tmp_path, monkeypatch):
    """投影只带可复用知识：任务快照/其它任务内容不进入 CLAUDE.md。"""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    memory.write_note(
        conn, "developer", "knowledge/T1/snap.md", "SNAPSHOTONLYUNIQUE jwt",
        kind="task_snapshot", scope="task", task_id="T1")
    memory.write_note(
        conn, "developer", "vulns/jwt.md", "ROLEONLYUNIQUE jwt pattern",
        kind="vulnerability_pattern", scope="role", tags=["jwt"])

    ctx = memory.context_for_task(
        conn, "developer", {"id": "T1", "title": "跳棋", "tags": ["jwt"], "repo": None})
    assert ctx["snapshot"] == ""
    assert ctx["notes"]
    joined = "\n".join(ctx["notes"])
    assert "ROLEONLYUNIQUE" in joined
    assert "SNAPSHOTONLYUNIQUE" not in joined
    assert len(joined) <= 4000


def test_project_instruction_uses_given_protocol(tmp_path):
    target = tmp_path / "CLAUDE.md"
    render.project_instruction(target, "", {"title": "t"}, protocol=render.STEP_PROTOCOL)
    text = target.read_text(encoding="utf-8")
    assert "anvil step submit" in text
    assert "第一步必须执行 `anvil task claim" not in text
