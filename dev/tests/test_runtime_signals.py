"""P4 deterministic runtime detector tests."""
from __future__ import annotations

from anvil import config, db
from anvil.services import runtime_signals
from anvil.util import now_iso


def test_scan_unhealthy_runtime_and_failed_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_PROPOSALS_DIR", tmp_path / "rip")
    conn = db.connect(str(tmp_path / "sig.db"))
    try:
        conn.execute(
            "INSERT INTO runtimes (id, display_name, executable_path, status) "
            "VALUES (?,?,?,?)", ("rt-x", "Codex", "/missing/codex", "missing"))
        conn.execute(
            "INSERT INTO runs (id, role_slug, status, origin, failure_class, "
            "prompt, started_at) VALUES (?,?,?,?,?,?,?)",
            ("R-1", "qa-role", "failed", "task", "spawn_error", "hello",
             now_iso()))
        conn.commit()
        first = runtime_signals.scan(conn)
        assert len(first) == 2
        second = runtime_signals.scan(conn)
        assert all(x["created"] is False for x in second)
        proposals = conn.execute(
            "SELECT COUNT(*) FROM runtime_proposals").fetchone()[0]
        assert proposals == 2
    finally:
        conn.close()
