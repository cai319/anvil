"""plan_slices 可选人工门与 Brief 覆盖矩阵（issue #8 / v1.62）。"""
from __future__ import annotations

import json

import pytest

from anvil.db import connect
from anvil.services import inbox
from anvil.services import reviews
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso

LOOP = {
    "from": "P1",
    "execute": {"role": "auditor"},
    "verify": {"role": "reviewer"},
    "fix": {"role": "developer"},
    "verify2": {"role": "reviewer"},
    "decide": {"role": "lead"},
}

WF_GATE = {
    "steps": [
        {"id": "P1", "title": "切片计划", "role": "lead", "kind": "plan_slices",
         "gate": True},
        {"id": "F1", "title": "最终报告", "role": "lead", "depends_on": ["P1"]},
    ],
    "loop": LOOP,
}


def _task(conn, tid: str = "T1") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES (?,?, 'todo',?,?)",
        (tid, "t", now, now))
    conn.commit()


def _brief(conn, acceptance=None, non_goals=None, tid: str = "T1") -> None:
    brief = {"goal": "g", "true_intent": "t",
             "acceptance": list(acceptance or []),
             "non_goals": list(non_goals or [])}
    conn.execute(
        "UPDATE tasks SET clarification_status='confirmed', intent_brief_json=? "
        "WHERE id=?", (json.dumps(brief), tid))
    conn.commit()


def _start(conn, wf=WF_GATE):
    wf = wf_service.create_workflow(conn, "T1", wf)
    _advance(conn, wf)
    return wf


def _advance(conn, wf, tag: str = "r"):
    started = []

    def starter(role, task_id, prompt, step):
        started.append(step["step_id"])
        return {"id": f"{tag}-{step['step_id']}"}

    wf_service.advance(conn, wf["id"], starter=starter)
    return started


def _step(wf, sid):
    return next(s for s in wf["steps"] if s["step_id"] == sid)


def _by_id(wf):
    return {s["step_id"]: s for s in wf["steps"]}


def test_gated_plan_submit_stores_plan_and_packet_without_expanding(tmp_path):
    conn = connect(str(tmp_path / "g.db"))
    _task(conn)
    wf = _start(conn)
    plan = {"slices": [{"id": "auth", "title": "认证"}]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    ids = [s["step_id"] for s in wf["steps"]]
    # 未批准不产生任何可执行步骤
    assert ids == ["P1", "F1"]
    assert _step(wf, "P1")["status"] == "submitted"
    assert json.loads(_step(wf, "P1")["plan_json"])["slices"][0]["id"] == "auth"
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    assert packet is not None
    assert packet["authority"] == "human"
    assert packet["plan_review"]["slices"][0]["id"] == "auth"
    # 下游步骤仍被 block
    assert _step(wf, "F1")["status"] == "pending"
    assert _advance(conn, wf) == []


def test_plan_gate_approval_expands_and_rewires_downstream(tmp_path):
    conn = connect(str(tmp_path / "a.db"))
    _task(conn)
    wf = _start(conn)
    plan = {"slices": [
        {"id": "auth", "title": "认证",
         "execute_steps": [{"id": "static", "title": "静态审计"}]},
        {"id": "pay", "title": "支付"},
    ]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    wf = wf_service.review_step(conn, wf["id"], "P1", True, "计划可执行")
    by_id = _by_id(wf)
    assert by_id["P1"]["status"] == "approved"
    assert "P1.auth.static" in by_id
    assert by_id["P1.auth.decide"]["kind"] == "decision"
    assert by_id["P1.pay.execute"]["depends_on"] == ["P1.auth.decide"]
    # 下游静态步骤改挂到最后一片 decide
    assert by_id["F1"]["depends_on"] == ["P1.pay.decide"]
    assert _advance(conn, wf) == ["P1.auth.static"]


def test_plan_gate_blocks_approval_when_brief_acceptance_uncovered(tmp_path):
    conn = connect(str(tmp_path / "c.db"))
    _task(conn)
    _brief(conn, acceptance=["认证可用", "支付可用"])
    wf = _start(conn)
    plan = {"slices": [{"id": "auth", "title": "认证", "brief_refs": ["认证可用"],
                        "scope_paths": ["src/auth/**"]}]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    coverage = {c["acceptance"]: c["status"]
                for c in packet["plan_review"]["coverage"]}
    assert coverage["认证可用"] == "covered"
    assert coverage["支付可用"] == "uncovered"
    with pytest.raises(ConflictError) as exc:
        wf_service.review_step(conn, wf["id"], "P1", True, "通过")
    assert "plan_gate_blocked" in str(exc.value)
    assert "支付可用" in str(exc.value)
    assert _step(wf_service.get_workflow(conn, wf["id"]), "P1")["status"] == "submitted"


def test_plan_gate_allows_deferred_acceptance_with_reason(tmp_path):
    conn = connect(str(tmp_path / "d.db"))
    _task(conn)
    _brief(conn, acceptance=["认证可用", "支付可用"])
    wf = _start(conn)
    plan = {
        "slices": [{"id": "auth", "title": "认证", "brief_refs": ["认证可用"]}],
        "deferred": [{"acceptance": "支付可用", "reason": "支付模块下一期交付"}],
    }
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    coverage = {c["acceptance"]: c["status"]
                for c in packet["plan_review"]["coverage"]}
    assert coverage["支付可用"] == "deferred"
    wf = wf_service.review_step(conn, wf["id"], "P1", True, "允许延期")
    assert _step(wf, "P1")["status"] == "approved"


def test_plan_gate_blocks_non_goal_conflict_and_shows_it(tmp_path):
    conn = connect(str(tmp_path / "n.db"))
    _task(conn)
    _brief(conn, acceptance=["认证可用"], non_goals=["src/legacy/**"])
    wf = _start(conn)
    plan = {"slices": [{"id": "auth", "title": "认证", "brief_refs": ["认证可用"],
                        "scope_paths": ["src/legacy/auth/**"]}]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    conflicts = packet["plan_review"]["non_goal_conflicts"]
    assert conflicts and conflicts[0]["non_goal"] == "src/legacy/**"
    assert "src/legacy/auth/**" in conflicts[0]["scope_paths"]
    with pytest.raises(ConflictError) as exc:
        wf_service.review_step(conn, wf["id"], "P1", True, "通过")
    assert "plan_gate_blocked" in str(exc.value)
    assert "non_goal_conflict" in str(exc.value)


def test_non_goal_acknowledged_in_out_of_scope_is_not_a_conflict(tmp_path):
    conn = connect(str(tmp_path / "n2.db"))
    _task(conn)
    _brief(conn, acceptance=["认证可用"], non_goals=["src/legacy/**"])
    wf = _start(conn)
    plan = {"slices": [{
        "id": "auth", "title": "认证", "brief_refs": ["认证可用"],
        "scope_paths": ["src/auth/**"], "out_of_scope": ["src/legacy/**"]}]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    assert packet["plan_review"]["non_goal_conflicts"] == []
    wf = wf_service.review_step(conn, wf["id"], "P1", True, "通过")
    assert _step(wf, "P1")["status"] == "approved"


def test_enabling_slice_declared_at_approval_carries_green_tests_check(tmp_path):
    conn = connect(str(tmp_path / "e.db"))
    _task(conn)
    _brief(conn, acceptance=["认证可用"])
    wf = _start(conn)
    plan = {"slices": [
        {"id": "auth", "title": "认证", "brief_refs": ["认证可用"],
         "scope_paths": ["src/auth/**"]},
        {"id": "scaffold", "title": "启用脚手架", "enabling": True,
         "enables": ["auth"], "scope_paths": ["tests/**"],
         "execute_steps": [{"id": "add-support", "title": "补测试支持"}]},
    ]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    enabling = packet["plan_review"]["enabling"]
    assert enabling and enabling[0]["slice_id"] == "scaffold"
    assert enabling[0]["enables"] == ["auth"]
    wf = wf_service.review_step(conn, wf["id"], "P1", True, "通过")
    by_id = _by_id(wf)
    assert "现有测试保持通过" in by_id["P1.scaffold.add-support"]["acceptance"]
    assert "现有测试保持通过" in by_id["P1.scaffold.decide"]["acceptance"]


def test_enabling_slice_requires_existing_downstream_slice(tmp_path):
    conn = connect(str(tmp_path / "e2.db"))
    _task(conn)
    wf = _start(conn)
    plan = {"slices": [{"id": "scaffold", "title": "启用", "enabling": True,
                        "enables": ["missing"]}]}
    with pytest.raises(ConflictError) as exc:
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    assert "enabling slice" in str(exc.value)
    assert _step(wf_service.get_workflow(conn, wf["id"]), "P1")["status"] == "running"


def test_runtime_revision_cannot_add_enabling_slice(tmp_path):
    conn = connect(str(tmp_path / "r.db"))
    _task(conn)
    wf = _start(conn)
    plan = {"slices": [
        {"id": "auth", "title": "认证", "scope_paths": ["src/auth/**"]},
        {"id": "pay", "title": "支付", "scope_paths": ["src/pay/**"]},
    ]}
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", plan)
    wf = wf_service.review_step(conn, wf["id"], "P1", True, "通过")
    impact = wf_service.preview_revision(conn, wf["id"], {"add_slices": [{
        "id": "late", "title": "运行时启用", "enabling": True,
        "enables": ["auth"], "scope_paths": ["tests/**"]}]})
    assert impact["safe"] is False
    assert "enabling" in impact["summary"]


def test_plan_gate_rejects_ai_authority(tmp_path):
    conn = connect(str(tmp_path / "ai.db"))
    _task(conn)
    wf_def = {
        "steps": [
            {"id": "P1", "title": "切片计划", "role": "lead",
             "kind": "plan_slices", "gate": True, "decision_authority": "ai"},
            {"id": "F1", "title": "报告", "role": "lead", "depends_on": ["P1"]},
        ],
        "loop": LOOP,
    }
    with pytest.raises(ConflictError) as exc:
        wf_service.create_workflow(conn, "T1", wf_def)
    assert "ai authority" in str(exc.value)


def test_gated_plan_appears_in_human_inbox(tmp_path):
    conn = connect(str(tmp_path / "inbox.db"))
    _task(conn)
    wf = _start(conn)
    wf_service.apply_slice_plan(
        conn, wf["id"], "P1", "lead", {"slices": [{"id": "auth", "title": "认证"}]})
    items = inbox.collect(conn)["items"]
    assert any(
        item["kind"] == "gate" and item["meta"]["step_id"] == "P1"
        for item in items)


def test_packet_refresh_keeps_plan_review(tmp_path):
    conn = connect(str(tmp_path / "refresh.db"))
    _task(conn)
    _brief(conn, acceptance=["认证可用"])
    wf = _start(conn)
    wf_service.apply_slice_plan(
        conn, wf["id"], "P1", "lead",
        {"slices": [{"id": "auth", "title": "认证", "brief_refs": ["认证可用"]}]})
    step = _step(wf_service.get_workflow(conn, wf["id"]), "P1")
    refreshed = reviews.ensure_packet(conn, wf_service.get_workflow(conn, wf["id"]),
                                      step, created_by="human", force=True)
    assert refreshed["plan_review"]["coverage"][0]["status"] == "covered"


def test_rejected_plan_is_cleared_and_can_be_resubmitted(tmp_path):
    conn = connect(str(tmp_path / "rej.db"))
    _task(conn)
    wf = _start(conn)
    wf = wf_service.apply_slice_plan(
        conn, wf["id"], "P1", "lead", {"slices": [{"id": "auth", "title": "认证"}]})
    assert _step(wf, "P1")["status"] == "submitted"
    wf = wf_service.review_step(conn, wf["id"], "P1", False, "切片粒度太粗")
    p1 = _step(wf, "P1")
    assert p1["status"] == "pending"
    assert p1["plan_json"] is None
    packet = reviews.latest_packet(conn, wf["id"], "P1")
    assert packet["status"] in ("superseded", "decided")
    assert _advance(conn, wf) == ["P1"]
    wf = wf_service.apply_slice_plan(
        conn, wf["id"], "P1", "lead",
        {"slices": [{"id": "auth", "title": "认证"}, {"id": "pay", "title": "支付"}]})
    wf = wf_service.review_step(conn, wf["id"], "P1", True, "通过")
    assert _step(wf, "P1")["status"] == "approved"
    assert "P1.pay.decide" in _by_id(wf)
