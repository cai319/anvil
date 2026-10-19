"""@ 反馈进入步骤/回复上下文：步骤 run 注入最近会话；回复 run 用 REPLY_PROTOCOL。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.util import now_iso

pytest.importorskip("anvil.graph")
from anvil.graph import nodes  # noqa: E402
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
    assert "接口字段少了 moveId" in prompt          # 最近会话被注入
    assert "## 任务会话" in prompt
    assert "交叉确认" in prompt                     # 挑战式协作协议
    assert "anvil step submit" in prompt


def test_reply_prompt_uses_reply_protocol(conn):
    run = runs_service.create_run(
        conn, "developer", "T1",
        prompt="[anvil:reply]你在任务会话中被 pm @：\n验收口径有歧义，请确认")
    prompt = nodes._prompt_for_run(conn, run, {"title": "跳棋", "description": "实现跳棋"})
    assert "验收口径有歧义" in prompt
    assert "只做讨论" in prompt
    assert "anvil task claim" not in prompt         # 不会让回复 run 去 claim 任务
    assert "anvil step submit" not in prompt
