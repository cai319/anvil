"""P1-0：agent 创建带 source_task 的草案；approve 落在父任务下并带 assignees/deps/验收/反馈路径。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import board as board_service

pytest.importorskip("anvil.graph")


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "b.db"))
    c.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES ('T1','五子棋','todo',?,?)",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    c.commit()
    yield c
    c.close()


TREE = {
    "id": "root", "title": "五子棋", "type": "epic", "leader": "pm",
    "feedback_rules": ["实现阻塞时 @pm", "需求歧义时 @pm，pm 决定是否升级 human"],
    "children": [
        {"id": "S1", "title": "设计 — 五子棋", "type": "design", "role": "designer",
         "acceptance": ["输出 docs/design.md"], "scope_paths": ["docs/**"]},
        {"id": "S2", "title": "实现 — 五子棋", "type": "implementation", "role": "developer",
         "depends_on": ["S1"], "acceptance": ["engine 测试通过"],
         "scope_paths": ["src/**", "tests/**"]},
    ],
}


def test_agent_draft_and_approve(conn):
    d = board_service.create_draft(conn, goal="五子棋 Web", tree=TREE,
                                   created_by="agent:pm", source_task="T1")
    assert d["source_task"] == "T1"
    assert d["status"] == "pending"
    out = board_service.approve(conn, [d["id"]])
    assert out["landed"] == [d["id"]]
    s2 = conn.execute("SELECT * FROM tasks WHERE id='S2'").fetchone()
    assert s2["parent_id"] == "T1"                      # 落在父任务下
    deps = [r["depends_on"] for r in conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id='S2'")]
    assert deps == ["S1"]
    roles = [r["role_slug"] for r in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id='S2'")]
    assert roles == ["developer"]
    assert "engine" in s2["acceptance_json"]
    assert "src/**" in s2["scope_paths_json"]
    assert "@pm" in (s2["description"] or "")            # 反馈路径并入描述


def test_list_drafts_filters_by_source_task(conn):
    board_service.create_draft(conn, goal="五子棋 Web", tree=TREE,
                               created_by="agent:pm", source_task="T1")
    board_service.decompose(conn, goal="另一个目标", created_by="human")
    only = board_service.list_drafts(conn, source_task="T1")
    assert len(only) == 1
    assert only[0]["source_task"] == "T1"
    assert len(board_service.list_drafts(conn)) == 2
