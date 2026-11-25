"""P2 paired replay statistics tests (spec RSI-EVAL-003..010)."""
from __future__ import annotations

import json

import pytest

from anvil import config, db
from anvil.services import eval_suite, role_eval


def _item(i: int, split: str, family: str) -> dict:
    return {
        "schema_version": 1, "item_id": f"R-{split}-{i}", "suite_id": "s",
        "split": split, "family": family,
        "prompt": {"task_title": f"T{i}"},
        "workspace": {"kind": "git_snapshot"},
        "verifier": {"kind": "deterministic",
                     "checks": [{"id": "c1", "cmd": "true", "hard": True}],
                     "forbidden_changes": [], "timeout_seconds": 60,
                     "token_cap": 1000},
        "rubric": None, "provenance": {"original_outcome": "done"},
    }


def _suite(tmp_path, monkeypatch, n: int = 8) -> None:
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    eval_suite.create_suite("s")
    for i in range(n):
        eval_suite.add_item("s", _item(i, "held_in", f"a;{i}"))
    for i in range(n):
        eval_suite.add_item("s", _item(100 + i, "held_out", f"b;{i}"))
    eval_suite.freeze("s")


def _replay(delta: float, *, tokens: int = 100, hard: bool = False,
            forbidden: bool = False):
    def fn(item: dict, version: int) -> dict:
        base = 0.5
        return {"score": base + (delta if version == 2 else 0.0),
                "tokens": tokens if version == 2 else 100,
                "hard": hard, "forbidden": forbidden}
    return fn


def _eval(conn, replay, **kw):
    return role_eval.evaluate(
        conn, role_slug="qa-role", suite_id="s",
        candidate_version=2, baseline_version=1,
        candidate_id="P-1", replay_fn=replay,
        noise_floor=0.02, minimum_effect=0.05, seed=7, **kw)


def test_promote_when_paired_gain_exceeds_noise(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        report = _eval(conn, _replay(0.10))
        assert report["verdict"] == "promote"
        assert report["splits"]["held_in"]["delta"] == pytest.approx(0.10)
        assert report["splits"]["held_out"]["delta"] == pytest.approx(0.10)
        assert report["splits"]["held_in"]["ci95"][0] > 0.02
        assert report["integrity"]["suite_digest"]
        assert "R-held_in-0" not in json.dumps(report)
        row = conn.execute("SELECT * FROM eval_runs WHERE id=?",
                           (report["run_id"],)).fetchone()
        assert row["verdict"] == "promote"
        assert row["status"] == "done"
        assert row["held_in_json"]
    finally:
        conn.close()


def test_reject_when_candidate_regresses(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        report = _eval(conn, _replay(-0.20))
        assert report["verdict"] == "reject"
    finally:
        conn.close()


def test_inconclusive_when_delta_is_within_noise(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        report = _eval(conn, _replay(0.0))
        assert report["verdict"] == "inconclusive"
    finally:
        conn.close()


def test_cost_gate_rejects_expensive_candidate(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        report = _eval(conn, _replay(0.20, tokens=200))
        assert report["verdict"] == "reject"
        assert "cost" in report["reason"].lower()
    finally:
        conn.close()


def test_baseline_hard_failure_is_not_candidate_failure(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)

    def replay(item: dict, version: int) -> dict:
        return {"score": 0.2 if version == 1 else 0.8, "tokens": 100,
                "hard": version == 1, "forbidden": False}

    conn = db.connect(str(tmp_path / "baseline-hard.db"))
    try:
        report = _eval(conn, replay)
        assert report["verdict"] == "promote"
    finally:
        conn.close()


def test_hard_or_forbidden_failure_rejects(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        assert _eval(conn, _replay(0.20, hard=True))["verdict"] == "reject"
        assert _eval(conn, _replay(0.20, forbidden=True))["verdict"] == "reject"
    finally:
        conn.close()


def test_diagnosis_records_working_set_context(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        report = role_eval.evaluate(
            conn, role_slug="qa-role", suite_id="s",
            candidate_version=2, baseline_version=1, candidate_id="P-1",
            replay_fn=_replay(0.10), noise_floor=0.02, minimum_effect=0.05,
            seed=7, baseline_working_set_hash="sha256:w1",
            candidate_working_set_hash="sha256:w2")
        assert report["diagnosis"]["policy"]["baseline_working_set_hash"] == "sha256:w1"
        assert report["diagnosis"]["policy"]["candidate_working_set_hash"] == "sha256:w2"
        row = conn.execute("SELECT * FROM eval_runs WHERE id=?",
                           (report["run_id"],)).fetchone()
        assert row["baseline_working_set_hash"] == "sha256:w1"
        assert row["candidate_working_set_hash"] == "sha256:w2"
    finally:
        conn.close()


def test_diagnosis_contains_direction_but_no_item_ids(tmp_path, monkeypatch):
    _suite(tmp_path, monkeypatch)
    conn = db.connect(str(tmp_path / "eval.db"))
    try:
        report = _eval(conn, _replay(0.10))
        diag = report["diagnosis"]
        assert diag["verdict"] == "promote"
        assert diag["splits"]["held_in"]["n"] == 8
        assert diag["dimensions"]
        assert diag["integrity"]["suite_digest"] == report["integrity"]["suite_digest"]
        text = json.dumps(diag, ensure_ascii=False)
        assert "R-held" not in text
        assert "T0" not in text
    finally:
        conn.close()
