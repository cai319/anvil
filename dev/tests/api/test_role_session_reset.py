"""角色级会话重置：清除 role_sessions，不要求有 run。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from anvil.util import now_iso  # noqa: E402
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload  # noqa: E402


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
    r = await client.post(f"{API}/roles/dev/session-reset", headers=auth_headers())
    assert r.status_code == 403
