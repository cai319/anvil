"""Workflow 执行引擎：依赖推进、同角色串行、gate 人工审核、全部完成进入评审。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import board as board_service
from anvil.services import tasks as tasks_service
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso

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


def test_reviewer_must_differ_from_implementer(conn):
    """builder 永不是 verifier：reviewer 不得等于执行角色。"""
    from anvil.util import ConflictError
    bad = {"steps": [{"id": "S1", "title": "x", "role": "dev", "reviewer": "dev"}]}
    with pytest.raises(ConflictError):
        wf_service.create_workflow(conn, "T1", bad)
    good = {"steps": [{"id": "S1", "title": "x", "role": "dev", "reviewer": "qa"}]}
    wf = wf_service.create_workflow(conn, "T1", good)
    assert wf["steps"][0]["reviewer"] == "qa"


def test_contract_flows_to_downstream_prompt(conn):
    definition = {
        "verification": [{"cell": "功能路径", "method": "E2E", "verifier_role": "qa"}],
        "steps": [
            {"id": "S1", "title": "接口", "role": "dev",
             "contract": "Facts live apart from whims; API v1 字段不可删除"},
            {"id": "S2", "title": "实现", "role": "dev", "depends_on": ["S1"],
             "contract": "所有金额用 Decimal"},
        ],
    }
    wf = wf_service.create_workflow(conn, "T1", definition)
    assert wf["definition"]["verification"][0]["cell"] == "功能路径"
    s2 = next(s for s in wf["steps"] if s["step_id"] == "S2")
    assert s2["contract"] == "所有金额用 Decimal"
    prompt = wf_service.step_prompt(wf, s2, {"title": "t", "description": "d"})
    assert "上游契约" in prompt and "API v1 字段不可删除" in prompt
    assert "所有金额用 Decimal" in prompt


def test_fail_step_auto_requeues_without_human(conn):
    """技术性失败先自动退避重试，不 @human（人类可能不在场）。"""
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.fail_step(conn, wf["id"], "S1", "platform_timeout", "environment")
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"] if s["step_id"] == "S1")
    assert step["status"] == "pending"
    assert step["failure_reason"] == "environment"
    assert step["recovery_count"] == 1
    assert step["next_retry_at"]
    assert conn.execute(
        "SELECT 1 FROM activities WHERE task_id='T1' AND kind='mention'").fetchone() is None


def test_fail_step_ends_bound_run_so_advance_can_redispatch(conn):
    """失败重排必须结束关联 run：残留 running 会占住角色，advance 永远不重派。"""
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, step_id, workflow_id, started_at) "
        "VALUES ('r1','dev','T1','running','S1',?,?)", (wf["id"], now_iso()))
    conn.commit()

    wf_service.fail_step(conn, wf["id"], "S1", "lease_expired", "environment")
    run = conn.execute("SELECT status, ended_at FROM runs WHERE id='r1'").fetchone()
    assert run["status"] == "failed"
    assert run["ended_at"]

    # 退避窗口结束后，同一角色必须能被重新派发（旧 run 不再是活跃 run）。
    conn.execute(
        "UPDATE workflow_steps SET next_retry_at='2000-01-01T00:00:00+00:00' "
        "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
    conn.commit()
    started = []
    wf_service.advance(conn, wf["id"],
                       starter=lambda *a: started.append(a) or {"id": "r2"})
    assert len(started) == 1
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"]
                if s["step_id"] == "S1")
    assert step["status"] == "running"
    assert step["run_id"] == "r2"


def test_fail_step_exhausted_marks_failed_without_human(conn, monkeypatch):
    """恢复预算耗尽后判失败，但只留非阻塞 comment，不产生待回复的 @human。"""
    monkeypatch.setattr(config, "AUTO_RECOVER_MAX_ATTEMPTS", 1)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    conn.execute(
        "UPDATE workflow_steps SET recovery_count=1 WHERE workflow_id=? AND step_id='S1'",
        (wf["id"],))
    conn.commit()
    wf_service.fail_step(conn, wf["id"], "S1", "platform_timeout", "environment")
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"] if s["step_id"] == "S1")
    assert step["status"] == "failed"
    assert conn.execute(
        "SELECT 1 FROM activities WHERE task_id='T1' AND kind='mention'").fetchone() is None
    row = conn.execute(
        "SELECT payload_json FROM activities WHERE task_id='T1' AND kind='comment' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and "step_failed" in row["payload_json"]


def test_advance_respects_retry_backoff(conn):
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute(
        "UPDATE workflow_steps SET next_retry_at='2999-01-01T00:00:00+00:00' "
        "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
    conn.commit()
    started = []
    wf_service.advance(conn, wf["id"], starter=lambda *a: started.append(a) or {"id": "r1"})
    assert started == []
    conn.execute(
        "UPDATE workflow_steps SET next_retry_at='2000-01-01T00:00:00+00:00' "
        "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
    conn.commit()
    wf_service.advance(conn, wf["id"], starter=lambda *a: started.append(a) or {"id": "r1"})
    assert len(started) == 1


def test_reopen_resumes_running_workflow_without_replan(conn):
    """人工终止后重开：接回运行中的执行，任务回到 in_progress。"""
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    tasks_service.cancel(conn, "T1")
    reopened = tasks_service.reopen(conn, "T1")
    assert reopened["status"] == "in_progress"
    current = wf_service.get_workflow(conn, wf["id"])
    assert current and current["status"] == "running"


def test_reopen_done_workflow_goes_to_review(conn):
    """终止期间工作流已完成：重开进入最终评审，不重新规划。"""
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute("UPDATE workflows SET status='done' WHERE id=?", (wf["id"],))
    conn.commit()
    tasks_service.cancel(conn, "T1")
    reopened = tasks_service.reopen(conn, "T1")
    assert reopened["status"] == "in_review"


def _insert_role(conn, slug: str, kind: str = "member") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO roles (slug, display_name, home_path, definition_json, "
        "created_at, updated_at, enabled) VALUES (?,?,?,?,?,?,1)",
        (slug, slug, os.path.join(tempfile.gettempdir(), slug), json.dumps({"kind": kind}), now, now))
    conn.commit()


def test_final_review_notification_uses_real_sender(conn):
    """@human 的最终评审通知由 planner/Cindy 发出，human 才能 @ 回去答复。"""
    _insert_role(conn, "cindy", "onboarding")
    _insert_role(conn, "dev")
    conn.execute("UPDATE tasks SET planner='cindy' WHERE id='T1'")
    wf = wf_service.create_workflow(conn, "T1", {
        "steps": [{"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=? AND step_id='S1'",
        (wf["id"],))
    conn.commit()
    wf_service._finalize_if_done(conn, wf["id"])
    row = conn.execute(
        "SELECT role_slug, payload_json FROM activities "
        "WHERE task_id='T1' AND kind='mention' ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["role_slug"] == "cindy"
    assert '"target": "human"' in row["payload_json"]


def test_notification_sender_prefers_assignee_creator(conn):
    from anvil.services import roles as roles_service

    _insert_role(conn, "cindy", "onboarding")
    _insert_role(conn, "dev")
    conn.execute(
        "INSERT INTO task_assignees (task_id, role_slug, joined_at) VALUES ('T1','dev',?)",
        (now_iso(),))
    conn.commit()
    assert roles_service.notification_sender(conn, "T1", "agent:dev") == "dev"
    # 未绑定/未知的 creator 回退到 Cindy
    assert roles_service.notification_sender(conn, "T1", "orchestrator") == "cindy"


def test_retry_prompt_includes_recovery_reason(conn):
    """自动恢复重跑时，步骤提示要带上次失败原因，避免 agent 重复踩坑。"""
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute(
        "UPDATE workflow_steps SET recovery_count=2, failure_reason='platform_timeout' "
        "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
    conn.commit()
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"]
                if s["step_id"] == "S1")
    prompt = wf_service.step_prompt(wf_service.get_workflow(conn, wf["id"]), step,
                                    {"title": "t", "description": "d"})
    assert "自动恢复" in prompt and "platform_timeout" in prompt


def test_human_retry_resets_recovery_budget(conn):
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    conn.execute(
        "UPDATE workflow_steps SET recovery_count=5, next_retry_at='2999-01-01T00:00:00+00:00' "
        "WHERE workflow_id=? AND step_id='S1'", (wf["id"],))
    conn.commit()
    wf_service.retry_step(conn, wf["id"], "S1")
    step = next(s for s in wf_service.get_workflow(conn, wf["id"])["steps"] if s["step_id"] == "S1")
    assert step["status"] == "pending"
    assert step["recovery_count"] == 0
    assert step["next_retry_at"] is None


# ---- 切片闭环：scope → execute[...] → verify → fix → verify2 → decide ----

SLICED = {
    "feedback_rules": ["审计风险升级 @human"],
    "slices": [
        {
            "id": "A",
            "title": "认证模块",
            "execute_role": "auditor",
            "verify_role": "reviewer",
            "fix_role": "developer",
            "decision_role": "lead",
            "execute_steps": [
                {"id": "static", "title": "静态审计", "acceptance": ["覆盖入口"]},
                {"id": "poc", "title": "PoC 复现", "acceptance": ["可复现"]},
            ],
            "verify": {"acceptance": ["独立复现"]},
        },
        {
            "id": "B",
            "title": "支付模块",
            "execute_role": "auditor",
            "verify_role": "reviewer",
            "fix_role": "developer",
            "decision_role": "lead",
        },
    ],
}


def _step(wf: dict, sid: str) -> dict:
    return next(s for s in wf["steps"] if s["step_id"] == sid)


def _finish(conn, wf: dict, sid: str, summary: str = "done") -> dict:
    step = _step(wf_service.get_workflow(conn, wf["id"]), sid)
    return wf_service.submit_step(conn, wf["id"], sid, step["role_slug"], summary)


def test_slices_expand_to_per_slice_loop(conn):
    wf = wf_service.create_workflow(conn, "T1", SLICED)
    assert [s["step_id"] for s in wf["steps"]] == [
        "A.scope", "A.static", "A.poc", "A.verify", "A.fix", "A.verify2", "A.decide",
        "B.scope", "B.execute", "B.verify", "B.fix", "B.verify2", "B.decide",
    ]
    assert _step(wf, "A.poc")["depends_on"] == ["A.static"]
    assert _step(wf, "A.verify")["depends_on"] == ["A.static", "A.poc"]
    assert _step(wf, "A.decide")["kind"] == "decision"
    assert _step(wf, "A.decide")["role_slug"] == "lead"
    assert _step(wf, "A.decide")["on_reject"] == ["A.fix", "A.verify2"]
    assert _step(wf, "A.decide")["max_rework"] == 2
    # 下一片只在上一片决策之后开工
    assert _step(wf, "B.scope")["depends_on"] == ["A.decide"]


def test_decision_reject_loops_fix_and_verify2_then_unlocks_next_slice(conn):
    wf = wf_service.create_workflow(conn, "T1", SLICED)
    assert _advance(conn, wf) == ["A.scope"]
    started: list[str] = []
    for sid in ("A.scope", "A.static", "A.poc", "A.verify", "A.fix", "A.verify2"):
        wf = _finish(conn, wf, sid)
        started = _advance(conn, wf)
    assert started == ["A.decide"]

    # 决策打回：fix/verify2/decide 回到 pending，计数 +1
    wf = wf_service.decide_step(conn, wf["id"], "A.decide", "lead", False, "证据不足")
    assert _step(wf, "A.fix")["status"] == "pending"
    assert _step(wf, "A.verify2")["status"] == "pending"
    assert _step(wf, "A.decide")["status"] == "pending"
    assert _step(wf, "A.decide")["rework_count"] == 1

    # 返工闭环重跑 fix → verify2 → decide
    assert _advance(conn, wf) == ["A.fix"]
    wf = _finish(conn, wf, "A.fix")
    assert _advance(conn, wf) == ["A.verify2"]
    wf = _finish(conn, wf, "A.verify2")
    assert _advance(conn, wf) == ["A.decide"]

    # 通过后下一片才解锁
    wf = wf_service.decide_step(conn, wf["id"], "A.decide", "lead", True, "通过")
    assert _advance(conn, wf) == ["B.scope"]
    assert _step(wf_service.get_workflow(conn, wf["id"]), "B.scope")["status"] == "running"


def test_step_transitions_publish_activities(conn):
    wf = wf_service.create_workflow(conn, "T1", SLICED)
    _advance(conn, wf)
    payloads = [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT payload_json FROM activities WHERE task_id='T1' AND kind='step' ORDER BY id"
    ).fetchall()]
    assert any(p["step"] == "A.scope" and p["status"] == "running" for p in payloads)

    wf = _finish(conn, wf, "A.scope", "范围确认")
    payloads = [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT payload_json FROM activities WHERE task_id='T1' AND kind='step' ORDER BY id"
    ).fetchall()]
    assert any(p["step"] == "A.scope" and p["status"] == "approved"
               and p.get("summary") == "范围确认" for p in payloads)


def test_large_flat_workflow_without_decision_is_rejected(conn):
    steps = [
        {"id": f"S{i}", "title": f"step {i}",
         "role": ("pm", "developer", "tester")[i % 3],
         "depends_on": [f"S{i - 1}"] if i > 1 else []}
        for i in range(1, 9)
    ]
    with pytest.raises(ConflictError, match="flat serial chain"):
        wf_service.create_workflow(conn, "T1", {"steps": steps})


def test_delete_task_cascades_workflow(conn):
    wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 1
    tasks_service.delete(conn, "T1", force=True)
    assert tasks_service.get(conn, "T1") is None
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM workflow_steps").fetchone()["n"] == 0


def test_startup_cleanup_removes_orphan_workflow(conn):
    """历史 delete 遗留的 workflow 在启动清理时被移除。"""
    from anvil.main import _cleanup_orphans

    wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "x", "role": "dev"}]})
    # 模拟旧版本：直接删 task，未级联 workflow
    conn.execute("DELETE FROM tasks WHERE id='T1'")
    conn.commit()
    _cleanup_orphans(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM workflow_steps").fetchone()["n"] == 0


DYNAMIC_WF = {
    "feedback_rules": ["P0 立即 @human"],
    "steps": [
        {"id": "P1", "title": "项目盘点与切片", "role": "lead", "kind": "plan_slices",
         "instructions": "盘点项目并切片", "acceptance": ["切片可独立闭环"]},
        {"id": "F1", "title": "最终报告", "role": "lead", "depends_on": ["P1"],
         "instructions": "汇总所有切片结论", "acceptance": ["报告完整"]},
    ],
    "loop": {
        "from": "P1",
        "execute": {"role": "auditor", "instructions": "执行审计"},
        "verify": {"role": "reviewer", "instructions": "独立复核"},
        "fix": {"role": "developer", "instructions": "修复与补证"},
        "verify2": {"role": "reviewer", "instructions": "复验"},
        "decide": {"role": "lead", "instructions": "切片决策", "max_rework": 2},
    },
}

DYNAMIC_PLAN = {
    "rationale": "按模块切片",
    "slices": [
        {"id": "auth", "title": "认证模块", "scope_paths": ["src/auth/**"],
         "acceptance": ["认证入口覆盖"],
         "execute_steps": [
             {"id": "static", "title": "静态审计", "acceptance": ["覆盖入口"]},
             {"id": "poc", "title": "PoC 复现", "acceptance": ["可复现"]},
         ]},
        {"id": "pay", "title": "支付模块", "scope_paths": ["src/pay/**"]},
    ],
}


def test_dynamic_slice_plan_expands_loops_and_rewires_final_step(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    assert _advance(conn, wf) == ["P1"]  # 先跑切片计划

    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
    by_id = {s["step_id"]: s for s in wf["steps"]}
    # 静态步骤先落库，切片步骤在计划提交后追加（rowid 顺序）
    assert [s["step_id"] for s in wf["steps"]] == [
        "P1", "F1",
        "P1.auth.static", "P1.auth.poc", "P1.auth.verify", "P1.auth.fix",
        "P1.auth.verify2", "P1.auth.decide",
        "P1.pay.execute", "P1.pay.verify", "P1.pay.fix",
        "P1.pay.verify2", "P1.pay.decide",
    ]
    assert by_id["P1"]["status"] == "approved"
    assert "2 片" in by_id["P1"]["summary"]
    assert by_id["P1.auth.verify"]["depends_on"] == ["P1.auth.static", "P1.auth.poc"]
    assert by_id["P1.auth.decide"]["kind"] == "decision"
    assert by_id["P1.auth.decide"]["on_reject"] == ["P1.auth.fix", "P1.auth.verify2"]
    assert by_id["P1.auth.decide"]["source_step_id"] == "P1"
    assert by_id["P1.auth.decide"]["slice_key"] == "auth"
    # 下一片在上一片 decide 之后；最终步骤改挂到最后一片 decide
    assert by_id["P1.pay.execute"]["depends_on"] == ["P1.auth.decide"]
    assert by_id["F1"]["depends_on"] == ["P1.pay.decide"]
    # 决策 prompt 汇总整片（按 slice_key 而非 plan 步骤前缀）
    prompt = wf_service.step_prompt(
        wf, by_id["P1.auth.decide"], {"title": "审计", "description": "大型项目"})
    assert "所属切片 auth" in prompt
    assert "P1.auth.static" in prompt and "P1.auth.verify2" in prompt
    assert "P1.pay" not in prompt
    assert _advance(conn, wf) == ["P1.auth.static"]


def test_dynamic_slice_loop_reject_then_next_slice(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
    assert _advance(conn, wf) == ["P1.auth.static"]

    for sid in ("P1.auth.static", "P1.auth.poc", "P1.auth.verify",
                "P1.auth.fix", "P1.auth.verify2"):
        wf = _finish(conn, wf, sid)
        _advance(conn, wf)
    assert _step(wf_service.get_workflow(conn, wf["id"]), "P1.auth.decide")["status"] == "running"

    wf = wf_service.decide_step(conn, wf["id"], "P1.auth.decide", "lead", False, "证据不足")
    assert _step(wf, "P1.auth.fix")["status"] == "pending"
    assert _step(wf, "P1.auth.verify2")["status"] == "pending"
    assert _step(wf, "P1.auth.decide")["rework_count"] == 1

    assert _advance(conn, wf) == ["P1.auth.fix"]
    wf = _finish(conn, wf, "P1.auth.fix")
    assert _advance(conn, wf) == ["P1.auth.verify2"]
    wf = _finish(conn, wf, "P1.auth.verify2")
    assert _advance(conn, wf) == ["P1.auth.decide"]
    wf = wf_service.decide_step(conn, wf["id"], "P1.auth.decide", "lead", True, "通过")
    assert _advance(conn, wf) == ["P1.pay.execute"]
    # 最终报告仍要等最后一片决策通过
    assert _step(wf_service.get_workflow(conn, wf["id"]), "F1")["status"] == "pending"


def test_slice_plan_blocked_while_workflow_revision_pending(conn):
    """Cindy 的新 workflow 待审时，旧 loop 下不得继续展开切片。"""
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    board_service.create_draft(
        conn, goal="修订 loop", source_task="T1", kind="workflow",
        tree={"steps": [{"id": "S1", "title": "新结构", "role": "lead"}]})
    with pytest.raises(ConflictError, match="revision is pending"):
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
    assert _step(wf_service.get_workflow(conn, wf["id"]), "P1")["status"] == "running"


def test_dynamic_slice_plan_is_idempotent(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    first = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
    second = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
    assert len(first["steps"]) == len(second["steps"])


def test_dynamic_plan_with_zero_slices_unblocks_final_step(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    wf = wf_service.apply_slice_plan(
        conn, wf["id"], "P1", "lead", {"slices": [], "rationale": "项目为空"})
    assert _step(wf, "P1")["status"] == "approved"
    assert _step(wf, "F1")["depends_on"] == []
    assert _advance(conn, wf) == ["F1"]


def test_dynamic_slice_plan_rejects_role_overrides(conn):
    """运行期切片计划不能重新分配角色；loop 角色只能由 Cindy 定义。"""
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    bad = {"slices": [{"id": "auth", "title": "认证", "execute_role": "auditor",
                       "verify_role": "auditor"}]}
    with pytest.raises(ConflictError, match="role fields are not allowed"):
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", bad)
    # execute_steps 也不能携带 role
    bad_step = {"slices": [{"id": "auth", "title": "认证", "execute_steps": [
        {"id": "static", "title": "静态审计", "role": "auditor"}]}]}
    with pytest.raises(ConflictError, match="must not specify a role"):
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", bad_step)
    # 校验失败不应把计划步骤标记完成，角色可修正后重试
    step = _step(wf_service.get_workflow(conn, wf["id"]), "P1")
    assert step["status"] == "running"


def test_dynamic_slice_plan_uses_loop_roles(conn):
    """切片只提供工作粒度，生成步骤的角色必须来自 loop 模板。"""
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
    by_id = {s["step_id"]: s for s in wf["steps"]}
    assert by_id["P1.auth.static"]["role_slug"] == "auditor"
    assert by_id["P1.auth.verify"]["role_slug"] == "reviewer"
    assert by_id["P1.auth.fix"]["role_slug"] == "developer"
    assert by_id["P1.auth.verify2"]["role_slug"] == "reviewer"
    assert by_id["P1.auth.decide"]["role_slug"] == "lead"


def test_dynamic_workflow_must_cover_all_bound_roles(conn):
    """loop workflow 必须让任务绑定的每个角色都出现在步骤/阶段中。"""
    task = tasks_service.create(conn, title="覆盖检查", assignees=["pm", "developer", "tester"])
    definition = {
        "steps": [
            {"id": "P1", "title": "盘点", "role": "pm", "kind": "plan_slices"},
            {"id": "F1", "title": "报告", "role": "pm", "depends_on": ["P1"]},
        ],
        "loop": {
            "from": "P1",
            "execute": {"role": "developer"},
            "verify": {"role": "pm"},
            "decide": {"role": "pm"},
        },
    }
    with pytest.raises(ConflictError, match="missing: tester"):
        wf_service.create_workflow(conn, task["id"], definition)


def test_plan_slices_prompt_uses_step_plan(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    prompt = wf_service.step_prompt(
        wf, _step(wf, "P1"), {"title": "审计", "description": "大型项目"})
    assert "切片计划职责" in prompt
    assert "anvil step plan" in prompt
    assert "execute → verify → fix → verify2 → decide" in prompt
    assert "不得指定角色" in prompt
    assert "--to cindy" in prompt


def test_dynamic_slice_plan_rejects_unknown_dependency(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    bad = {"slices": [{"id": "auth", "title": "认证", "execute_steps": [
        {"id": "static", "title": "静态审计", "depends_on": ["missing"]}]}]}
    with pytest.raises(ConflictError, match="unknown dependency"):
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", bad)
    assert _step(wf_service.get_workflow(conn, wf["id"]), "P1")["status"] == "running"


def test_plan_slices_requires_loop_template(conn):
    definition = {"steps": [{"id": "P1", "title": "切片", "role": "lead",
                             "kind": "plan_slices"}]}
    with pytest.raises(ConflictError, match="requires a top-level `loop`"):
        wf_service.create_workflow(conn, "T1", definition)


def test_plan_slices_step_cannot_submit(conn):
    wf = wf_service.create_workflow(conn, "T1", DYNAMIC_WF)
    _advance(conn, wf)
    with pytest.raises(ConflictError, match="use `anvil step plan`"):
        wf_service.submit_step(conn, wf["id"], "P1", "lead", "done")


def test_decision_prompt_lists_whole_slice_outputs(conn):
    wf = wf_service.create_workflow(conn, "T1", SLICED)
    for sid, summary in (("A.scope", "范围已确认"), ("A.static", "静态审计完成"),
                         ("A.poc", "PoC 已复现")):
        conn.execute(
            "UPDATE workflow_steps SET status='approved', summary=? "
            "WHERE workflow_id=? AND step_id=?", (summary, wf["id"], sid))
    conn.commit()
    wf = wf_service.get_workflow(conn, wf["id"])
    prompt = wf_service.step_prompt(wf, _step(wf, "A.decide"),
                                    {"title": "代码审计", "description": "审计大型项目"})
    for sid, summary in (("A.scope", "范围已确认"), ("A.static", "静态审计完成"),
                         ("A.poc", "PoC 已复现")):
        assert sid in prompt
        assert summary in prompt
    assert "anvil step decide" in prompt


def test_role_busy_on_another_task_blocks_new_step(conn):
    """角色在别的任务上仍有活跃 run 时，本任务的步骤保持 pending；空出后再派发。"""
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T2','另一个任务','todo',?,?)", (now, now))
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
        "VALUES ('R-OTHER', 'developer', 'T1', 'running', ?)", (now,))
    conn.commit()

    wf = wf_service.create_workflow(conn, "T2", {
        "steps": [{"id": "S1", "title": "实现", "role": "developer", "depends_on": []}]
    })
    assert _advance(conn, wf) == []  # developer 正在 T1 上工作

    conn.execute("UPDATE runs SET status='suspended' WHERE id='R-OTHER'")
    conn.commit()
    assert _advance(conn, wf) == ["S1"]  # 角色空出后本任务才能开始

def test_advance_is_serialized(conn):
    """scheduler 与 API 同时 advance 时，同一步骤只能被 starter 拉起一次。"""
    wf = wf_service.create_workflow(conn, "T1", {
        "steps": [{"id": "S1", "title": "基线", "role": "pm", "depends_on": []}]
    })
    started: list[str] = []
    barrier = threading.Barrier(2)

    def starter(role, task_id, prompt, step):
        started.append(step["step_id"])
        time.sleep(0.05)
        return {"id": "run-S1"}

    def run():
        barrier.wait()
        wf_service.advance(conn, wf["id"], starter=starter)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert started == ["S1"]
