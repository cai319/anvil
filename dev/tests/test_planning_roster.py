"""Planner Roster 渲染：workspace.repos 支持字符串与 {name, access} 对象。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import planning, tasks as tasks_service


def _role(conn, slug: str, repos):
    from anvil.services import roles as roles_service

    roles_service.create(conn, slug=slug, display_name=slug, definition={
        "runtime": {"id": "claude-code"},
        "workspace": {"repos": repos},
        "responsibilities": [f"{slug} duty"],
    })


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = connect(str(tmp_path / "plan.db"))
    yield c
    c.close()


def test_render_roster_accepts_repo_objects(conn):
    """真实角色配置里的 repos 是 [{name, access}]，不能再让 join 直接炸掉。"""
    _role(conn, "pm", [{"name": "gomoku", "access": "write"}])
    _role(conn, "designer", ["plain-repo"])
    task = tasks_service.create(conn, title="丢骰子", assignees=["pm", "designer"])

    roster = planning.build_roster(conn, task["id"], ["pm", "designer"])
    text = planning.render_roster(conn, task["id"], roster)

    assert "gomoku(write)" in text
    assert "plain-repo" in text


def test_start_planner_run_with_repo_objects_does_not_fail(conn, tmp_path):
    """角色建议批准后的自动重规划必须能真正创建 run（回归 Type 错误）。"""
    _role(conn, "pm", [{"name": "gomoku", "access": "write"}])
    task = tasks_service.create(conn, title="丢骰子", assignees=["pm"])

    run = planning.start_planner_run(
        conn, str(tmp_path / "plan.db"), None, task["id"], "cindy",
        assignees=["pm"], extra_constraint="human 已批准角色建议", replace_active=True,
    )

    assert run["role_slug"] == "cindy"
    assert run["task_id"] == task["id"]
    assert "gomoku(write)" in (run.get("prompt") or "")
    assert "[anvil:plan]" in (run.get("prompt") or "")
