"""Step leases, fencing, expiry and path reservations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil.db import connect
from anvil.services import leases
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso


def _conn(tmp_path):
    conn = connect(str(tmp_path / "l.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','t','todo',?,?)",
        (now, now))
    conn.commit()
    return conn


def _wf(conn, starter_id="run-S1", **step):
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev", **step}]})
    wf_service.advance(
        conn, wf["id"],
        starter=lambda *a: {"id": starter_id})
    return wf_service.get_workflow(conn, wf["id"])


def test_advance_acquires_and_submit_releases_step_lease(tmp_path):
    conn = _conn(tmp_path)
    wf = _wf(conn)
    step = wf["steps"][0]
    assert step["lease_id"]
    assert step["fence_token"] == 1
    assert step["lease_holder"] == "dev"
    lease = leases.active(conn, "step", f"{wf['id']}:S1")
    assert lease and lease["run_id"] == "run-S1"
    wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成", run_id="run-S1")
    assert leases.active(conn, "step", f"{wf['id']}:S1") is None


def test_stale_or_expired_lease_rejects_submit(tmp_path):
    conn = _conn(tmp_path)
    wf = _wf(conn)
    step = wf["steps"][0]
    conn.execute("UPDATE leases SET fence=fence+1 WHERE id=?", (step["lease_id"],))
    conn.commit()
    with pytest.raises(ConflictError, match="lease_fenced"):
        wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成", run_id="run-S1")

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    conn.execute("UPDATE leases SET fence=?, expires_at=? WHERE id=?",
                 (step["fence_token"], past, step["lease_id"]))
    conn.commit()
    with pytest.raises(ConflictError, match="lease_expired"):
        wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成", run_id="run-S1")
    row = conn.execute("SELECT status FROM leases WHERE id=?", (step["lease_id"],)).fetchone()
    assert row["status"] == "expired"


def test_path_reservations_conflict_and_release(tmp_path):
    conn = _conn(tmp_path)
    first = leases.reserve_paths(conn, "T1", ["src/app.ts", "src/lib/"], holder="dev")
    assert len(first) == 2
    with pytest.raises(ConflictError, match="path_conflict"):
        leases.reserve_paths(conn, "T1", ["src/lib/a.ts"], holder="qa")
    with pytest.raises(ConflictError, match="path_conflict"):
        leases.reserve_paths(conn, "T1", ["src"], holder="qa")
    # Same holder can re-check without conflict; reservation itself is idempotent-ish.
    assert leases.check_paths(conn, "T1", ["src/app.ts"], holder="dev") == []
    assert leases.release_paths(conn, "T1", ["src/app.ts"], "dev") == 1
    assert leases.check_paths(conn, "T1", ["src/app.ts"], holder="qa") == []


def test_scope_violation_rejected_on_submit(tmp_path):
    conn = _conn(tmp_path, )
    wf = _wf(conn, scope_paths=["src/**"])
    with pytest.raises(ConflictError, match="scope_violation"):
        wf_service.submit_step(
            conn, wf["id"], "S1", "dev", "完成", run_id="run-S1",
            changed_paths=["docs/readme.md"])
    wf_service.submit_step(
        conn, wf["id"], "S1", "dev", "完成", run_id="run-S1",
        changed_paths=["src/app.ts"])


def test_lease_reap_and_fence_monotonic(tmp_path):
    conn = _conn(tmp_path)
    first = leases.acquire(conn, "path", "T1:a.ts", holder="dev")
    assert first["fence"] == 1
    leases.release(conn, first["id"])
    second = leases.acquire(conn, "path", "T1:a.ts", holder="qa")
    assert second["fence"] == 2
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    conn.execute("UPDATE leases SET expires_at=? WHERE id=?", (past, second["id"]))
    conn.commit()
    assert leases.reap_expired(conn) == 1
    again = leases.acquire(conn, "path", "T1:a.ts", holder="dev")
    assert again["fence"] == 3
