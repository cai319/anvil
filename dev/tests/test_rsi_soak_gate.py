"""P3.1 evaluation-gated promotion + SOAKING auto-rollback tests."""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.services import proposals, role_space, roles, rsi
from anvil.util import now_iso


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    monkeypatch.setattr(config, "ROLE_RSI_EVALUATION", True)
    monkeypatch.setattr(config, "ROLE_RSI_AUTO_ROLLBACK", True)


def _seed(conn):
    roles.create(conn, slug="rsi-role", display_name="RSI",
                 definition={"runtime": {"id": "claude-code"},
                             "rsi": {"enabled": True},
                             "responsibilities": ["backend"]})
    home = config.AGENTS_DIR / "rsi-role"
    (home / "instruction.md").write_text(
        "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
        "<!-- ANVIL:STRATEGY:BEGIN -->\n策略 v1\n<!-- ANVIL:STRATEGY:END -->\n",
        encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("准则\n", encoding="utf-8")


def _strategy_candidate(conn):
    edit = {
        "operator": "replace_strategy_block", "tier": "human_approval",
        "target": "instruction.strategy", "content_md": "策略 v2",
        "portrait_refs": ["responsibilities[0]"],
        "diagnosis_ref": "verification_discipline",
        "predicted_effect": {"dimension": "quality", "direction": "up",
                             "magnitude": 0.1},
    }
    return proposals.create_candidate(
        conn, "rsi-role", cycle_id="C-1", edits=[edit],
        tier="human_approval", base_version=1)


def _eval_run(conn, candidate_id: str, verdict: str = "promote") -> str:
    rid = f"E-{verdict}"
    conn.execute(
        "INSERT INTO eval_runs (id, role_slug, candidate_id, suite_id, "
        "suite_digest, verdict, status, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, "rsi-role", candidate_id, "s", "d", verdict, "done",
         now_iso(), now_iso()))
    conn.commit()
    return rid


def test_promotion_requires_promote_eval_for_strategy(tmp_path):
    conn = db.connect(str(tmp_path / "gate.db"))
    try:
        _seed(conn)
        cand = _strategy_candidate(conn)
        proposals.transition_candidate(conn, cand["id"], "pending")
        with pytest.raises(ValueError):
            rsi.promote_candidate(conn, cand["id"])
        rid = _eval_run(conn, cand["id"], "inconclusive")
        proposals.attach_eval(conn, cand["id"], rid)
        with pytest.raises(ValueError):
            rsi.promote_candidate(conn, cand["id"])
        rid = _eval_run(conn, cand["id"], "promote")
        proposals.attach_eval(conn, cand["id"], rid)
        version = rsi.promote_candidate(conn, cand["id"])
        assert "策略 v2" in (
            config.AGENTS_DIR / "rsi-role" / "instruction.md").read_text(
                encoding="utf-8")
        assert version["status"] == "active"
    finally:
        conn.close()


def test_soak_check_rolls_back_regressed_version(tmp_path):
    conn = db.connect(str(tmp_path / "soak.db"))
    try:
        _seed(conn)
        v1 = role_space.snapshot(
            conn, "rsi-role", kind="evolution", source="baseline",
            status="active")
        home = config.AGENTS_DIR / "rsi-role"
        (home / "PRINCIPLES.md").write_text("准则 v2\n", encoding="utf-8")
        v2 = role_space.snapshot(
            conn, "rsi-role", kind="evolution", source="rsi_promote",
            status="active", parent_version=v1["version"])
        # v1 had a done task; v2 active version only saw a closed task.
        for tid, status in (("T-V1", "done"), ("T-V2", "closed")):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?)", (tid, tid, status, now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, origin, "
            "role_space_version, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
            ("R-V1", "rsi-role", "T-V1", "completed", "task", v1["version"],
             now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, origin, "
            "role_space_version, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
            ("R-V2", "rsi-role", "T-V2", "completed", "task", v2["version"],
             now_iso(), now_iso()))
        conn.commit()
        result = rsi.soak_check(
            conn, "rsi-role", v2["version"],
            baseline_version=v1["version"], min_runs=1, noise=0.05)
        assert result["action"] == "rolled_back"
        active = conn.execute(
            "SELECT version FROM role_versions WHERE slug=? AND kind='evolution' "
            "AND status='active'", ("rsi-role",)).fetchone()
        assert active["version"] == v1["version"]
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rsi.auto_rollback'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
