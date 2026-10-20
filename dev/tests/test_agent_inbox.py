"""Agent Inbox 拉模式：check 不推进 cursor、commit 前进、at-least-once、排除自己消息。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import agent_inbox
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "ib.db"))
    now = now_iso()
    c.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','t','in_progress',?,?)",
              (now, now))
    c.commit()
    yield c
    c.close()


def test_check_commit_and_self_exclusion(conn):
    tasks_service.comment(conn, "T1", "pm", "PRD 已更新")
    tasks_service.comment(conn, "T1", "developer", "我自己的进展")   # 自己发的不进 inbox
    tasks_service.mention(conn, "T1", "tester", "developer", "请确认接口")

    items = agent_inbox.items(conn, "developer", "T1")
    texts = [i["text"] for i in items]
    assert any("PRD 已更新" in t for t in texts)
    assert any("请确认接口" in t for t in texts)
    assert not any("我自己的进展" in t for t in texts)

    # check 不推进 cursor：再次读取仍返回同一批（at-least-once）
    assert len(agent_inbox.items(conn, "developer", "T1")) == 2
    up_to = items[-1]["id"]
    agent_inbox.commit(conn, "developer", "T1", up_to)
    assert agent_inbox.unread_count(conn, "developer", "T1") == 0
    assert agent_inbox.items(conn, "developer", "T1") == []

    # 新消息继续可见
    tasks_service.comment(conn, "T1", "pm", "补充一条")
    assert agent_inbox.unread_count(conn, "developer", "T1") == 1
