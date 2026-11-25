"""RSI reflection evidence bundle and output ingestion (spec RSI-CAND-001/RSI-OBS-004)."""
from __future__ import annotations

import json

from anvil import config, db
from anvil.services import rsi, roles
from anvil.util import now_iso


def _seed(conn) -> None:
    roles.create(conn, slug="rsi-role", display_name="RSI",
                 definition={"runtime": {"id": "claude-code"},
                             "rsi": {"enabled": True},
                             "responsibilities": ["backend"]})


def _observation(conn, *, role="rsi-role", task_id="T-1", trigger="task_done",
                 **payload) -> None:
    body = {"role": role, "task_id": task_id, "trigger": trigger,
            "task_title": "修复验收", **payload}
    conn.execute(
        "INSERT INTO events (ts, event_type, actor_type, actor, task_id, payload_json) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), "rsi.observation", "system", role, task_id, json.dumps(body)))
    conn.commit()


def _reflection_run(conn, tmp_path, text: str, *, status="completed",
                    role="rsi-role") -> str:
    log = tmp_path / f"{role}.log"
    log.write_text(text, encoding="utf-8")
    run_id = "R-RSI-1"
    conn.execute(
        "INSERT INTO runs (id, role_slug, status, log_path, prompt, origin, "
        "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, role, status, str(log),
         "[anvil:rsi-reflect] cycle=C-1", "rsi_reflect", now_iso(), now_iso()))
    conn.commit()
    return run_id


def _result(payload: dict) -> str:
    return ("# run\n[assistant] thinking\n[result] usage={}\n"
            + json.dumps(payload, ensure_ascii=False) + "\n")


def test_build_reflection_prompt_uses_redacted_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    conn = db.connect(str(tmp_path / "prompt.db"))
    try:
        _seed(conn)
        _observation(conn, feedback="token=SECRET-123 at /Users/alice/private")
        prompt = rsi.build_reflection_prompt(conn, "rsi-role", cycle_key="C-1")
        assert "rsi-role" in prompt
        assert "SECRET-123" not in prompt
        assert "/Users/alice" not in prompt
        assert "R-HELDOUT-1" not in prompt
        assert "append_principles" not in prompt or "append_principle" in prompt
        assert "schema_version" in prompt
        assert "rsi.reflect" in prompt or "rsi-reflect" in prompt
    finally:
        conn.close()


def test_reap_reflection_ingests_outputs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    monkeypatch.setattr(config, "ROLE_RSI_AUTONOMOUS_KNOWLEDGE", True)
    monkeypatch.setattr(config, "RUNTIME_PROPOSALS_DIR", tmp_path / "rip")
    conn = db.connect(str(tmp_path / "ingest.db"))
    try:
        _seed(conn)
        _observation(conn)
        payload = {
            "schema_version": 1,
            "cycle_id": "C-1",
            "role_slug": "rsi-role",
            "outcome": {"verdict": "success", "why": "ok"},
            "knowledge_items": [{
                "scope": "role", "kind": "personal_lesson",
                "title": "先跑验收", "tldr": "提交前跑验收命令",
                "fields": {"use_when": "提交前"},
            }],
            "edits": [{
                "operator": "append_principle", "tier": "autonomous",
                "target": "principles", "content_md": "提交前必须跑验收。",
                "portrait_refs": ["responsibilities[0]"],
                "diagnosis_ref": "verification_discipline",
                "predicted_effect": {"dimension": "verification_discipline"},
            }],
            "runtime_issues": [{
                "component_type": "skill", "component_key": "skill:verify",
                "symptom": "skill load failed", "severity": "major",
                "root_cause_hypothesis": "missing manifest",
                "proposed_change": "修复 SKILL.md manifest",
            }],
        }
        _reflection_run(conn, tmp_path, _result(payload))
        first = rsi.reap_reflections(conn)
        assert first[0]["outcome"] == "ingested"
        card = config.AGENTS_DIR / "rsi-role/notes/memory/cards"
        cards = list(card.glob("*.md"))
        assert len(cards) == 1
        assert "提交前跑验收命令" in cards[0].read_text(encoding="utf-8")
        cand = conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE kind='strategy'").fetchone()[0]
        assert cand == 1
        # A claimed autonomous tier cannot downgrade an approval-tier operator.
        row = conn.execute(
            "SELECT proposed_json FROM proposals WHERE kind='strategy'").fetchone()
        edits = json.loads(row[0])["edits"]
        assert edits[0]["tier"] == "human_approval"
        rip = conn.execute("SELECT COUNT(*) FROM runtime_proposals").fetchone()[0]
        assert rip == 1
        # Reap is idempotent for a given run.
        second = rsi.reap_reflections(conn)
        assert second == []
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rsi.reflection_ingested'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_reap_marks_bad_output_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    conn = db.connect(str(tmp_path / "bad.db"))
    try:
        _seed(conn)
        _reflection_run(conn, tmp_path, "[result] usage={}\nnot json\n")
        first = rsi.reap_reflections(conn)
        assert first[0]["outcome"] == "parse_failed"
        assert rsi.reap_reflections(conn) == []
    finally:
        conn.close()


def test_scheduler_reaps_completed_reflection(tmp_path, monkeypatch):
    from anvil.db import open_conn
    from anvil.scheduler import Scheduler
    from tests.graph.conftest import make_env

    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    monkeypatch.setattr(config, "ROLE_RSI_AUTONOMOUS_KNOWLEDGE", True)
    env = make_env(tmp_path, monkeypatch)
    conn = open_conn(env["db_path"])
    try:
        log = tmp_path / "done.log"
        log.write_text(_result({
            "schema_version": 1, "role_slug": "dev", "cycle_id": "C-1",
            "no_action_reason": "nothing to change",
        }), encoding="utf-8")
        conn.execute(
            "INSERT INTO runs (id, role_slug, status, log_path, origin, "
            "started_at, ended_at) VALUES ('R-DONE','dev','completed',?,"
            "'rsi_reflect',?,?)", (str(log), now_iso(), now_iso()))
        conn.commit()
    finally:
        conn.close()
    Scheduler(env["db_path"], env["orch"]).tick_once()
    conn = open_conn(env["db_path"])
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rsi.reflection_ingested'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()
