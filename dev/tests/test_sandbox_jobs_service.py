"""PoC 作业服务：创建/列表/取消/领取与状态守卫（M2a）。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import sandbox


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
