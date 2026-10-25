"""Review packets and immutable decision records."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil.db import connect
from anvil.services import inbox
from anvil.services import reviews
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso


def _task(conn, tid: str = "T1") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES (?,?, 'todo',?,?)",
        (tid, "t", now, now))
    conn.commit()


def test_gate_submit_creates_packet_and_records_decision(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "设计评审", "role": "dev", "gate": True,
         "reviewer": "qa", "acceptance": ["覆盖接口契约"]}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf = wf_service.submit_step(conn, wf["id"], "S1", "dev", "设计完成")
    packet = reviews.latest_packet(conn, wf["id"], "S1")
    assert packet is not None
    assert "设计评审" in packet["question"]
    assert packet["authority"] == "human"
    assert packet["evidence"]
    wf = wf_service.review_step(conn, wf["id"], "S1", True, "通过")
    decisions = reviews.decisions_for_step(conn, wf["id"], "S1")
    assert decisions[-1]["decision"] == "approve"
    assert decisions[-1]["decider_type"] == "human"
    assert decisions[-1]["packet_id"] == packet["id"]
    assert conn.execute(
        "SELECT 1 FROM activities WHERE task_id=? AND kind='decision' LIMIT 1", ("T1",)
    ).fetchone()


def test_decision_step_records_reject(tmp_path):
    conn = connect(str(tmp_path / "d.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"},
        {"id": "D1", "title": "验收决策", "role": "lead", "kind": "decision",
         "depends_on": ["S1"], "on_reject": ["S1"]},
    ]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf = wf_service.submit_step(conn, wf["id"], "S1", "dev", "实现完成")
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    wf = wf_service.decide_step(conn, wf["id"], "D1", "lead", False, "验收不通过")
    packet = reviews.latest_packet(conn, wf["id"], "D1")
    assert packet is not None
    assert packet["authority"] == "ai"
    decisions = reviews.decisions_for_step(conn, wf["id"], "D1")
    assert decisions[-1]["decision"] == "reject"
    assert decisions[-1]["rework_targets"] == ["S1"]


def test_gate_reject_can_widen_rework_scope(tmp_path):
    conn = connect(str(tmp_path / "r.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S0", "title": "实现", "role": "dev"},
        {"id": "S1", "title": "评审", "role": "qa", "gate": True,
         "reviewer": "lead", "depends_on": ["S0"]},
    ]})
    started: list[str] = []

    def starter(role, *args):
        started.append(role)
        return {"id": f"r_{role}"}

    wf_service.advance(conn, wf["id"], starter=starter)
    wf_service.submit_step(conn, wf["id"], "S0", "dev", "完成")
    wf_service.advance(conn, wf["id"], starter=starter)
    wf_service.submit_step(conn, wf["id"], "S1", "qa", "评审完成")
    with pytest.raises(ConflictError):
        wf_service.review_step(
            conn, wf["id"], "S1", False, "重做", rework_targets=["missing"])
    wf = wf_service.review_step(
        conn, wf["id"], "S1", False, "实现需要返工", rework_targets=["S0"])
    by_id = {s["step_id"]: s for s in wf["steps"]}
    assert by_id["S0"]["status"] == "pending"
    assert by_id["S1"]["status"] == "pending"
    decision = reviews.decisions_for_step(conn, wf["id"], "S1")[-1]
    assert set(decision["rework_targets"]) == {"S0", "S1"}


def test_evidence_normalization_rejects_unsafe_paths():
    assert reviews.normalize_evidence(["tests passed"])[0]["kind"] == "note"
    with pytest.raises(reviews.EvidenceError):
        reviews.normalize_evidence([{"kind": "file", "ref": "/etc/passwd"}])
    with pytest.raises(reviews.EvidenceError):
        reviews.normalize_evidence([{"kind": "file", "ref": "../../secret"}])
    with pytest.raises(reviews.EvidenceError):
        reviews.normalize_evidence([{"kind": "bogus", "ref": "x"}])


def test_evidence_required_blocks_submit(tmp_path):
    conn = connect(str(tmp_path / "e.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev", "evidence_required": True},
    ]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    with pytest.raises(ConflictError):
        wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成")
    wf = wf_service.submit_step(
        conn, wf["id"], "S1", "dev", "完成",
        evidence=[{"kind": "test", "ref": "pytest tests/test_x.py"}])
    assert wf["steps"][0]["evidence"][0]["kind"] == "test"


def test_human_authority_decision_skips_ai_run_and_opens_packet(tmp_path):
    conn = connect(str(tmp_path / "h.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"},
        {"id": "D1", "title": "人工验收", "role": "lead", "kind": "decision",
         "decision_authority": "human", "depends_on": ["S1"], "on_reject": ["S1"]},
    ]})
    started: list[str] = []

    def starter(role, *args):
        started.append(role)
        return {"id": f"r_{role}"}

    wf_service.advance(conn, wf["id"], starter=starter)
    wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成")
    wf_service.advance(conn, wf["id"], starter=starter)
    assert started == ["dev"]
    wf = wf_service.get_workflow(conn, wf["id"])
    d1 = next(s for s in wf["steps"] if s["step_id"] == "D1")
    assert d1["status"] == "submitted"
    assert d1["run_id"] is None
    packet = reviews.latest_packet(conn, wf["id"], "D1")
    assert packet["status"] == "pending_human"
    items = inbox.collect(conn)["items"]
    assert any(i["kind"] == "decision" and i["meta"]["step_id"] == "D1" for i in items)
    wf = wf_service.decide_step(conn, wf["id"], "D1", "human", True, "同意")
    assert next(s for s in wf["steps"] if s["step_id"] == "D1")["status"] == "approved"
    assert wf["status"] == "done"


def test_joint_authority_ai_recommends_then_human_approves(tmp_path):
    conn = connect(str(tmp_path / "j.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"},
        {"id": "D1", "title": "联合验收", "role": "lead", "kind": "decision",
         "decision_authority": "joint", "depends_on": ["S1"], "on_reject": ["S1"]},
    ]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成")
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    wf = wf_service.decide_step(conn, wf["id"], "D1", "lead", True, "建议通过")
    d1 = next(s for s in wf["steps"] if s["step_id"] == "D1")
    assert d1["status"] == "submitted"
    packet = reviews.latest_packet(conn, wf["id"], "D1")
    assert packet["status"] == "pending_human"
    assert packet["recommendation"]["decision"] == "approve"
    assert reviews.decisions_for_step(conn, wf["id"], "D1")[-1]["decision"] == "recommend_approve"
    wf = wf_service.decide_step(
        conn, wf["id"], "D1", "human", True, "同意 AI 建议")
    assert next(s for s in wf["steps"] if s["step_id"] == "D1")["status"] == "approved"


def test_stale_evidence_blocks_ai_approve_and_escalates(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    _task(conn)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"},
        {"id": "D1", "title": "验收", "role": "lead", "kind": "decision",
         "depends_on": ["S1"], "on_reject": ["S1"]},
    ]})
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.submit_step(
        conn, wf["id"], "S1", "dev", "完成",
        evidence=[{"kind": "test", "ref": "pytest", "captured_at": old}])
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    with pytest.raises(ConflictError) as exc:
        wf_service.decide_step(conn, wf["id"], "D1", "lead", True, "通过")
    assert "evidence_stale" in str(exc.value)
    wf = wf_service.escalate_step(
        conn, wf["id"], "D1", "lead", "证据已过期，请人工确认",
        triggers=["evidence_stale"], proposed_decision="approve")
    d1 = next(s for s in wf["steps"] if s["step_id"] == "D1")
    assert d1["status"] == "submitted"
    packet = reviews.latest_packet(conn, wf["id"], "D1")
    assert packet["status"] == "pending_human"
    assert packet["recommendation"]["escalated"] is True
    wf = wf_service.decide_step(
        conn, wf["id"], "D1", "human", True, "接受风险",
        override_reason="人工确认过期证据仍可用")
    assert next(s for s in wf["steps"] if s["step_id"] == "D1")["status"] == "approved"
