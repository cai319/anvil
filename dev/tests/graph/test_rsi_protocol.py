"""P3 RSI reflection run/protocol tests (spec RSI-CAND-001/002)."""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.graph import nodes, render
from anvil.services import roles, rsi


@pytest.fixture(autouse=True)
def _rsi_enabled(monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)


def test_rsi_reflect_prompt_kind_and_protocol(tmp_path):
    conn = db.connect(str(tmp_path / "proto.db"))
    try:
        run = {"prompt": "[anvil:rsi-reflect] cycle=x", "task_id": None}
        assert nodes._run_prompt_kind(conn, run) == "rsi_reflect"
        assert nodes._protocols_for_run(conn, run, None, "rsi_reflect") == \
            render.RSI_REFLECT_PROTOCOL
        assert "不可信数据" in render.RSI_REFLECT_PROTOCOL
    finally:
        conn.close()


def test_start_reflection_creates_no_task_run(tmp_path):
    conn = db.connect(str(tmp_path / "run.db"))
    try:
        roles.create(conn, slug="qa-role", display_name="QA",
                     definition={"runtime": {"id": "claude-code"},
                                 "rsi": {"enabled": True}})
        run = rsi.start_reflection(conn, "qa-role", prompt_body="证据包",
                                   cycle_key="C-1")
        assert run is not None
        assert run["task_id"] is None
        assert run["origin"] == "rsi_reflect"
        assert run["prompt"].startswith("[anvil:rsi-reflect]")
        assert nodes._run_prompt_kind(conn, run) == "rsi_reflect"
        started = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rsi.reflection_started' "
            "AND actor='qa-role'").fetchone()[0]
        assert started == 1
    finally:
        conn.close()
