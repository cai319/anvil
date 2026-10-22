"""角色感知规划的基础件：Roster、非破坏性绑定、规划标记、workflow supersede。"""
from __future__ import annotations

from anvil import config
from anvil.db import connect
from anvil.graph import nodes
from anvil.services import planning
from anvil.services import roles as roles_service
from anvil.services import tasks as tasks_service
from anvil.services import workflow as workflow_service
from anvil.util import now_iso


def test_add_assignees_is_non_destructive(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','t','todo',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO task_assignees (task_id,role_slug,joined_at) VALUES ('T1','dev',?)", (now,)
    )
    conn.execute(
        "INSERT INTO task_role_claims (task_id,role_slug,status,updated_at) "
        "VALUES ('T1','dev','working',?)", (now,)
    )
    conn.commit()

    tasks_service.add_assignees(conn, "T1", ["tester"])
    task = tasks_service.get(conn, "T1")
    assert task["assignees"] == ["dev", "tester"]
    claims = {c["role_slug"]: c["status"] for c in task["role_claims"]}
    assert claims["dev"] == "working"  # 已有 claim 不被清空
    assert claims["tester"] == "pending"


def test_build_roster_includes_capabilities_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn = connect(str(tmp_path / "t.db"))
    roles_service.create(conn, slug="dev", display_name="Dev", definition={
        "runtime": {"id": "claude-code"},
        "capabilities": {"python": 0.9},
        "responsibilities": ["实现"],
        "concurrency": {"max_parallel": 2},
        "workspace": {"repos": ["repo-a"]},
    })
    roster = planning.build_roster(conn, "T1", ["dev"])
    assert roster[0]["capabilities"]["python"] == 0.9
    assert roster[0]["max_parallel"] == 2
    assert roster[0]["repos"] == ["repo-a"]
    assert roster[0]["active_runs"] == 0


def test_plan_marker_avoids_reply_protocol(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    out = nodes._prompt_for_run(
        conn,
        {"role_slug": "cindy", "prompt": planning.PLAN_MARKER + "\n请基于 Roster 规划",
         "task_id": None},
        {"title": "t", "description": "d"},
    )
    assert "请基于 Roster 规划" in out
    assert "只做讨论与回复" not in out
    # planner 不应被通用 PROTOCOL 的「必须 claim」覆盖（PLANNING_PROTOCOL 明确禁止 claim）
    assert "第一步必须执行 `anvil task claim" not in out


def test_planning_protocol_uses_runtime_slicing():
    from anvil.graph.render import PLANNING_PROTOCOL

    assert "不要由你预先切片" in PLANNING_PROTOCOL
    assert "plan_slices" in PLANNING_PROTOCOL
    assert "anvil step plan" in PLANNING_PROTOCOL
    assert "loop" in PLANNING_PROTOCOL


def test_task_size_block_flags_audit_and_wide_scope():
    assert "plan_slices + loop" in planning._task_size_block(
        {"title": "大型项目代码安全审计", "scope_paths": []})
    assert "plan_slices + loop" in planning._task_size_block(
        {"title": "普通任务", "scope_paths": ["a/**", "b/**", "c/**"]})
    assert planning._task_size_block({"title": "普通任务"}) == ""


def test_new_workflow_supersedes_old_running(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','t','todo',?,?)",
        (now, now),
    )
    conn.commit()
    wf1 = workflow_service.create_workflow(
        conn, "T1", {"steps": [{"id": "S1", "title": "a", "role": "dev"}]})
    conn.execute("UPDATE workflow_steps SET status='running' WHERE workflow_id=?",
                 (wf1["id"],))
    conn.commit()

    workflow_service.create_workflow(
        conn, "T1", {"steps": [{"id": "S1", "title": "b", "role": "dev"}]})

    old = workflow_service.get_workflow(conn, wf1["id"])
    assert old["status"] == "failed"
    assert old["steps"][0]["status"] == "failed"
    assert old["steps"][0]["failure_reason"] == "superseded"
