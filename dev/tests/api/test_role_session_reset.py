"""角色级会话重置：清除 role_sessions，不要求有 run。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from anvil.services import runs as runs_service  # noqa: E402
from anvil.util import now_iso  # noqa: E402
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)


async def test_role_session_reset_clears_stored_session(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO role_sessions (role_slug, session_id, status, updated_at) "
            "VALUES (?,?,?,?)",
            ("dev", "sid-old", "dead", now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    r = await client.post(f"{API}/roles/dev/session-reset", headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["reset"] == "session"

    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT session_id, status FROM role_sessions WHERE role_slug='dev'"
        ).fetchone()
    finally:
        conn.close()
    assert row["session_id"] == ""
    assert row["status"] == "dead"


async def test_role_session_reset_requires_human(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())
    r = await client.post(f"{API}/roles/dev/session-reset", headers=auth_headers("pm"))
    assert r.status_code == 403


async def test_agent_self_reset_context_renews_run(client, app, monkeypatch):
    """角色自主 reset：清会话、run 回到 created、写审计。"""
    from anvil.services import daemon as daemon_service

    await client.post(f"{API}/tasks", json=task_payload(id="T-SELF"), headers=auth_headers())
    conn = open_conn(app.state.db_path)
    try:
        run = runs_service.create_run(conn, "qa-role", "T-SELF")
        runs_service.mark_spawned(conn, run["id"], "rs:qa-role")
        runs_service.set_status(conn, run["id"], "running")
    finally:
        conn.close()

    class FakeMgr:
        enabled = True

        def __init__(self):
            self.calls = []

        def status(self, role):
            return {"alive": True, "current_run_id": run["id"],
                    "current_task_id": "T-SELF", "queue_len": 0,
                    "context_tokens": 70000}

        def reset_session(self, role, cic=False):
            self.calls.append((role, cic))

    class FakeOrch:
        def __init__(self):
            self.calls = []

        def pump(self, db_path, run_id, event, payload):
            self.calls.append((run_id, event))

    fake = FakeMgr()
    orch = FakeOrch()
    monkeypatch.setattr(daemon_service, "get_manager", lambda: fake)
    app.state.orchestrator = orch

    r = await client.post(f"{API}/me/session/reset", headers=auth_headers("qa-role"),
                          json={"task_id": "T-SELF", "reason": "上下文过高"})
    assert r.status_code == 200, r.text
    assert fake.calls == [("qa-role", True)]
    assert orch.calls == [(run["id"], "wake")]
    assert r.json()["rewoken"] is True

    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute("SELECT status, native_session_id FROM runs WHERE id=?",
                           (run["id"],)).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit WHERE role_slug='qa-role' AND task_id='T-SELF' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["status"] == "created"
    assert row["native_session_id"] is None
    assert audit["action"] == "session.reset_context"
