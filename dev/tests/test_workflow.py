"""Workflow 执行引擎：依赖推进、同角色串行、gate 人工审核、全部完成进入评审。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import workflow as wf_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")

WF = {
    "feedback_rules": ["阻塞 @pm"],
    "steps": [
        {"id": "S1", "title": "需求基线", "role": "pm", "depends_on": [], "gate": True,
         "acceptance": ["PRD"]},
        {"id": "S2", "title": "实现", "role": "developer", "depends_on": ["S1"]},
        {"id": "S3", "title": "测试用例", "role": "tester", "depends_on": ["S1"]},
        {"id": "S4", "title": "回归", "role": "developer", "depends_on": ["S2", "S3"]},
    ],
}


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "wf.db"))
    now = now_iso()
    c.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','跳棋','todo',?,?)",
              (now, now))
    c.commit()
    yield c
    c.close()


def _advance(conn, wf):
    started = []

    def starter(role, task_id, prompt, step):
        started.append(step["step_id"])
        return {"id": f"run-{step['step_id']}"}

    wf_service.advance(conn, wf["id"], starter=starter)
    return started


def test_dependency_progression_and_gate(conn):
    wf = wf_service.create_workflow(conn, "T1", WF)
    assert conn.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()["status"] == "in_progress"

    assert _advance(conn, wf) == ["S1"]          # 只有无依赖的 S1
    wf = wf_service.submit_step(conn, wf["id"], "S1", "pm", "PRD 完成")
    step1 = next(s for s in wf["steps"] if s["step_id"] == "S1")
    assert step1["status"] == "submitted"        # gate 步骤等待人工

    assert _advance(conn, wf) == []              # gate 未通过，下游不动
    wf = wf_service.review_step(conn, wf["id"], "S1", approve=True)
    assert _advance(conn, wf) == ["S2", "S3"]    # 不同角色可并行
    wf = wf_service.submit_step(conn, wf["id"], "S2", "developer", "实现完成")
    step2 = next(s for s in wf["steps"] if s["step_id"] == "S2")
    assert step2["status"] == "approved"         # 非 gate 自动通过
    assert _advance(conn, wf) == []              # S3 未完成，S4 依赖未满足
    wf = wf_service.submit_step(conn, wf["id"], "S3", "tester", "用例完成")
    assert _advance(conn, wf) == ["S4"]
    wf = wf_service.submit_step(conn, wf["id"], "S4", "developer", "回归完成")
    assert wf["status"] == "done"
    assert conn.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()["status"] == "in_review"
    # 完成时 @human 通知
    assert conn.execute(
        "SELECT 1 FROM activities WHERE task_id='T1' AND kind='mention'").fetchone()


def test_same_role_steps_are_serialized(conn):
    definition = {
        "steps": [
            {"id": "S1", "title": "A", "role": "developer", "depends_on": []},
            {"id": "S2", "title": "B", "role": "developer", "depends_on": []},
        ]
    }
    wf = wf_service.create_workflow(conn, "T1", definition)
    started = _advance(conn, wf)
    assert started == ["S1"]  # 同一角色一次只跑一个步骤
    wf_service.submit_step(conn, wf["id"], "S1", "developer", "done")
    assert _advance(conn, wf) == ["S2"]


def test_gate_reject_resets_step(conn):
    wf = wf_service.create_workflow(conn, "T1", WF)
    _advance(conn, wf)
    wf_service.submit_step(conn, wf["id"], "S1", "pm", "v1")
    wf = wf_service.review_step(conn, wf["id"], "S1", approve=False, feedback="补充验收标准")
    step1 = next(s for s in wf["steps"] if s["step_id"] == "S1")
    assert step1["status"] == "pending"
    assert _advance(conn, wf) == ["S1"]  # 重新调度
