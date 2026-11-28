"""P0-B：工作区 brief.json 不自动导入；human 查看后导入仍需结构化确认。"""
from __future__ import annotations

import json

import pytest

from anvil.db import connect
from anvil.services import clarification, roles, task_workspace, tasks
from anvil.util import ConflictError, now_iso


def _brief(**overrides) -> dict:
    brief = {
        "goal": "实现登录",
        "true_intent": "让用户能安全登录",
        "non_goals": ["不做注册"],
        "constraints": ["沿用现有会话表"],
        "acceptance": ["登录成功返回 token"],
        "assumptions": ["已有用户表"],
        "open_questions": [],
        "weight": "standard",
        "weight_rationale": "中等风险",
        "plan_shape": "auto",
        "plan_shape_rationale": "按 scope 判断",
        "risks": ["会话固定"],
    }
    brief.update(overrides)
    return brief


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "brief.db"))
    roles.ensure_onboarding(c)
    now = now_iso()
    c.execute(
        "INSERT INTO tasks (id,title,type,status,created_at,updated_at,tags_json,planner) "
        "VALUES ('T1','任务','feature','todo',?,?,'[]','cindy')", (now, now))
    c.commit()
    return c


def _write_workspace_brief(task_id: str, brief: dict, rel: str = "brief.json") -> None:
    ws = task_workspace.path_for(task_id)
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")


def test_workspace_brief_is_not_auto_imported(conn):
    clarification.start(conn, "T1")
    _write_workspace_brief("T1", _brief())
    state = clarification.get_state(conn, "T1")
    assert state["brief"] is None
    assert state["brief_version"] == 0
    assert clarification.versions(conn, "T1") == []


def test_preview_reads_without_persisting(conn):
    clarification.start(conn, "T1")
    _write_workspace_brief("T1", _brief())
    preview = clarification.preview_workspace_brief(conn, "T1")
    assert preview["exists"] is True
    assert preview["relative_path"] == "brief.json"
    assert preview["brief"]["goal"] == "实现登录"
    assert clarification.get_state(conn, "T1")["brief"] is None


def test_preview_supports_anvil_projection(conn):
    _write_workspace_brief("T1", _brief(), rel=".anvil/intent-brief.json")
    preview = clarification.preview_workspace_brief(conn, "T1")
    assert preview["relative_path"] == ".anvil/intent-brief.json"


def test_import_requires_human_and_review(conn):
    clarification.start(conn, "T1")
    _write_workspace_brief("T1", _brief())
    with pytest.raises(ConflictError):
        clarification.import_workspace_brief(
            conn, "T1", reviewed=True, actor="cindy")
    with pytest.raises(ValueError):
        clarification.import_workspace_brief(
            conn, "T1", reviewed=False, actor="human")


def test_import_saves_draft_but_does_not_confirm(conn):
    clarification.start(conn, "T1")
    _write_workspace_brief("T1", _brief())
    preview = clarification.preview_workspace_brief(conn, "T1")
    result = clarification.import_workspace_brief(
        conn, "T1", brief=preview["brief"], reviewed=True, actor="human")
    assert result["imported"] is True
    assert result["requires_confirmation"] is True
    state = result["clarification"]
    assert state["status"] == "in_progress"
    assert state["brief_version"] == 1
    assert state["brief"].get("confirmed_by") is None
    assert [r["kind"] for r in clarification.versions(conn, "T1")] == ["draft"]

    # 仍必须走结构化确认。
    confirmed = clarification.confirm(conn, "T1", actor="human")
    assert confirmed["status"] == "confirmed"
    assert confirmed["brief"]["confirmed_by"] == "human"


def test_import_rejects_invalid_structure(conn):
    clarification.start(conn, "T1")
    with pytest.raises(ValueError):
        clarification.import_workspace_brief(
            conn, "T1", brief={"unknown_field": 1}, reviewed=True, actor="human")


def test_import_requires_in_progress_clarification(conn):
    _write_workspace_brief("T1", _brief())
    with pytest.raises(ConflictError):
        clarification.import_workspace_brief(
            conn, "T1", reviewed=True, actor="human")
