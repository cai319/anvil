"""P0 proposal field allowlist tests (spec RSI-SEC-002).

Proposal approval must not become a backdoor for charter/runtime fields.
"""
from __future__ import annotations

import pytest

from anvil import db
from anvil.services import memory, roles


def _seed(conn) -> None:
    roles.create(
        conn,
        slug="qa-role",
        display_name="QA",
        definition={
            "runtime": {"id": "claude-code"},
            "responsibilities": ["testing"],
            "capabilities": {"python": 0.5},
        },
    )


@pytest.mark.parametrize("field", [
    "description", "runtime", "permissions", "workspace", "concurrency",
])
def test_forbidden_proposal_fields_are_rejected(tmp_path, field):
    conn = db.connect(str(tmp_path / "forbidden.db"))
    try:
        _seed(conn)
        with pytest.raises(ValueError):
            memory.create_proposal(conn, "qa-role", field, {"x": 1}, "r", "k")
        with pytest.raises(ValueError):
            memory.create_proposal(conn, "qa-role", f"portrait:{field}", {"x": 1}, "r", "k")
    finally:
        conn.close()


def test_allowed_proposal_field_is_applied(tmp_path):
    conn = db.connect(str(tmp_path / "allowed.db"))
    try:
        _seed(conn)
        p = memory.create_proposal(
            conn, "qa-role", "responsibilities", ["testing", "review"], "r", "k")
        approved = memory.approve_proposal(conn, p["id"])
        assert approved["status"] == "approved"
        role = roles.get(conn, "qa-role")
        assert role["definition"]["responsibilities"] == ["testing", "review"]
    finally:
        conn.close()
