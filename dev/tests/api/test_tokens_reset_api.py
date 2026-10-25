"""Global token counter reset (display baseline, non-destructive)."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn
from anvil.util import now_iso
from tests.conftest import API, auth_headers, human_headers


def _add_usage(app, *, input_tokens: int, output_tokens: int,
               cache_read: int = 0, role: str = "qa-role") -> None:
    conn = open_conn(app.state.db_path)
    conn.execute(
        "INSERT INTO token_usage (role_slug, task_id, run_id, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
        (role, None, None, input_tokens, output_tokens, cache_read, 0, now_iso()))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_token_counter_reset_is_two_scope(client, app):
    _add_usage(app, input_tokens=100, output_tokens=20, cache_read=30)
    before = await client.get(f"{API}/tokens", headers=human_headers())
    assert before.status_code == 200, before.text
    assert before.json()["total_tokens"] == 150

    denied = await client.post(f"{API}/tokens/reset", headers=auth_headers("pm"))
    assert denied.status_code == 403

    reset = await client.post(f"{API}/tokens/reset", headers=human_headers())
    assert reset.status_code == 200, reset.text
    assert reset.json()["reset_at"]

    after = await client.get(f"{API}/tokens", headers=human_headers())
    assert after.json()["total_tokens"] == 0
    assert after.json()["by_role"] == []

    # New usage after the baseline is counted; the old rows remain for task metrics.
    _add_usage(app, input_tokens=5, output_tokens=7)
    later = await client.get(f"{API}/tokens", headers=human_headers())
    assert later.json()["total_tokens"] == 12

    conn = open_conn(app.state.db_path)
    try:
        audit = conn.execute(
            "SELECT action, payload_json FROM audit WHERE action='tokens.reset'").fetchone()
        assert audit is not None and "reset_at" in audit["payload_json"]
        # Role-scoped summaries remain all-time and are not affected by the display reset.
        role = await client.get(f"{API}/tokens?scope=qa-role", headers=human_headers())
        assert role.json()["total_tokens"] == 162
    finally:
        conn.close()
