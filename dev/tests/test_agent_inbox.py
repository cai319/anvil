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


def test_slice_scoped_inbox_does_not_leak_or_advance_other_slice(conn):
    tasks_service.comment(conn, "T1", "pm", "A-only", slice_key="A")
    tasks_service.comment(conn, "T1", "pm", "B-only", slice_key="B")
    tasks_service.comment(conn, "T1", "pm", "task-wide")

    a_items = agent_inbox.items(conn, "developer", "T1", slice_key="A")
    a_texts = [i["text"] for i in a_items]
    assert "A-only" in a_texts
    assert "task-wide" in a_texts
    assert "B-only" not in a_texts

    b_before = agent_inbox.items(conn, "developer", "T1", slice_key="B")
    assert "B-only" in [i["text"] for i in b_before]
    agent_inbox.commit(conn, "developer", "T1", a_items[-1]["id"], slice_key="A")

    # A 的 cursor 不影响 B；task-wide 是独立 cursor。
    assert agent_inbox.unread_count(conn, "developer", "T1", slice_key="A") == 0
    assert agent_inbox.unread_count(conn, "developer", "T1", slice_key="B") == 2
    assert agent_inbox.unread_count(conn, "developer", "T1") == 1
