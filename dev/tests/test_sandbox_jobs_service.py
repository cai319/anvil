"""PoC 作业服务：创建/列表/取消/领取与状态守卫（M2a）。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import findings as findings_service
from anvil.services import inbox as inbox_service
from anvil.services import sandbox, tasks as tasks_service


def _raw(**over) -> dict:
    raw = {
        "network": "none",
        "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
        "artifacts": [{"path": "out/**"}],
    }
    raw.update(over)
    return raw


def _conn(tmp_path):
    return connect(str(tmp_path / "t.db"))


def test_create_list_get_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "docker ready"))
    conn = _conn(tmp_path)
    try:
        job = sandbox.create_job(
            conn, spec_raw=_raw(), actor_role="human", actor_is_human=True)
        assert job["status"] == "queued"
        assert job["profile"] == "safe"
        assert job["approval_status"] == "not_required"
        assert sandbox.list_jobs(conn)[0]["id"] == job["id"]
        view = sandbox.job_view(conn, job["id"])
        assert view["phases"] == [] and view["artifacts"] == []
        canceled = sandbox.cancel_job(conn, job["id"])
        assert canceled["status"] == "canceled"
        assert sandbox.claim_next_job(conn) is None
    finally:
        conn.close()


def test_agent_relaxed_requires_human(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    conn = _conn(tmp_path)
    try:
        with pytest.raises(PermissionError):
            sandbox.create_job(
                conn, spec_raw=_raw(network="bridge"),
                actor_role="pm", actor_is_human=False)
        assert sandbox.list_jobs(conn) == []
    finally:
        conn.close()


def test_strict_unavailable_blocks(tmp_path, monkeypatch):
    from anvil.sandbox.errors import SandboxUnavailable

    monkeypatch.setattr(sandbox, "strict_available", lambda: (False, "no docker"))
    conn = _conn(tmp_path)
    try:
        with pytest.raises(SandboxUnavailable):
            sandbox.create_job(
                conn, spec_raw=_raw(), actor_role="human", actor_is_human=True)
    finally:
        conn.close()


def test_claim_sets_native_job_and_cancel_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    conn = _conn(tmp_path)
    try:
        job = sandbox.create_job(
            conn, spec_raw=_raw(), actor_role="human", actor_is_human=True)
        claimed = sandbox.claim_next_job(conn)
        assert claimed["status"] == "running"
        assert claimed["native_job_id"].startswith("anvil-poc-")
        assert sandbox.claim_next_job(conn) is None
    finally:
        conn.close()


def test_agent_safe_queued_relaxed_waiting_and_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    conn = _conn(tmp_path)
    try:
        tasks_service.create(conn, id="T1", title="t", assignees=["pm"])
        safe = sandbox.create_job(
            conn, spec_raw=_raw(task_id="T1"),
            actor_role="pm", actor_is_human=False)
        assert safe["status"] == "queued"
        assert safe["created_by_type"] == "role"

        relaxed = sandbox.create_job(
            conn, spec_raw=_raw(task_id="T1", network="bridge"),
            actor_role="pm", actor_is_human=False)
        assert relaxed["status"] == "waiting_approval"
        assert relaxed["approval_status"] == "pending"

        approved = sandbox.approve_job(conn, relaxed["id"], actor_role="human")
        assert approved["status"] == "queued"
        assert approved["approval_status"] == "approved"

        rejected = sandbox.create_job(
            conn, spec_raw=_raw(task_id="T1", network="bridge"),
            actor_role="pm", actor_is_human=False)
        out = sandbox.reject_job(conn, rejected["id"], actor_role="human",
                                 note="too risky")
        assert out["status"] == "rejected"

        with pytest.raises(PermissionError):
            sandbox.create_job(
                conn, spec_raw=_raw(), actor_role="pm", actor_is_human=False)
    finally:
        conn.close()


def test_quota_counts_running_and_queued(tmp_path):
    from anvil.sandbox.errors import QuotaExceeded

    conn = _conn(tmp_path)
    try:
        for i in range(2):
            conn.execute(
                "INSERT INTO sandbox_jobs (id, kind, task_id, created_by, spec_json, "
                "spec_hash, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"run{i}", "poc", "T1", "pm", "{}", "h", "running",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit()
        with pytest.raises(QuotaExceeded):
            sandbox._check_quota(conn, task_id="T1", actor_role="pm")
    finally:
        conn.close()


def test_verification_for_finding(tmp_path):
    conn = _conn(tmp_path)
    try:
        def add(jid: str, status: str):
            conn.execute(
                "INSERT INTO sandbox_jobs (id, kind, finding_id, created_by, spec_json, "
                "spec_hash, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (jid, "poc", "F1", "pm", "{}", "h", status,
                 f"2026-01-01T00:00:0{jid[-1]}Z", "2026-01-01T00:00:00Z"))
        add("j1", "failed")
        add("j2", "succeeded")
        conn.commit()
        marks = findings_service.verification_for(conn, ["F1"])
        assert marks["F1"]["status"] == "verified"
        assert marks["F1"]["job_id"] == "j2"

        add("j3", "queued")
        conn.commit()
        # succeeded 优先级高于 pending
        assert findings_service.verification_for(conn, ["F1"])["F1"]["status"] == "verified"

        conn.execute("DELETE FROM sandbox_jobs")
        add("j4", "failed")
        conn.commit()
        assert findings_service.verification_for(conn, ["F1"])["F1"]["status"] == "failed"
        conn.execute("DELETE FROM sandbox_jobs")
        conn.commit()
        assert findings_service.verification_for(conn, ["F1"]) == {}
    finally:
        conn.close()


def test_inbox_lists_poc_approval(tmp_path):
    conn = _conn(tmp_path)
    try:
        conn.execute(
            "INSERT INTO sandbox_jobs (id, kind, task_id, finding_id, created_by, "
            "spec_json, spec_hash, status, approval_status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("poc_1", "poc", None, "F1", "pm", "{}", "h",
             "waiting_approval", "pending",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit()
        data = inbox_service.collect(conn)
        kinds = [item["kind"] for item in data["items"]]
        assert "poc_job" in kinds
    finally:
        conn.close()
