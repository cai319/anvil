"""Paired replay evaluation and statistics (v1.50 P2).

The evaluator is outside the evolving role: it reads candidate/baseline
versions, runs paired items, and writes only aggregate results to eval_runs.
Per-item data stays in the hidden suite and is never included in diagnosis.
"""
from __future__ import annotations

import json
import random
import sqlite3
from statistics import mean

from .. import config
from ..util import jdumps, new_id, now_iso
from . import eval_suite

MIN_PAIRS = 8
MIN_EFFECT = 0.05
MAX_COST_RATIO = 1.5


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def stratified_bootstrap(by_family: dict[str, list[float]], *,
                         B: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Return (mean, ci_low, ci_high) of paired deltas, stratified by family."""
    values = [v for group in by_family.values() for v in group]
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    families = list(by_family)
    samples: list[float] = []
    for _ in range(max(1, int(B))):
        resampled: list[float] = []
        for family in families:
            group = by_family[family]
            if not group:
                continue
            resampled.extend(rng.choice(group) for _ in group)
        samples.append(mean(resampled) if resampled else 0.0)
    return (mean(values), _percentile(samples, 0.025), _percentile(samples, 0.975))


def _replay_pair(item: dict, replay_fn, baseline_version: int,
                 candidate_version: int) -> tuple[dict, dict]:
    baseline = replay_fn(item, baseline_version)
    candidate = replay_fn(item, candidate_version)
    for side in (baseline, candidate):
        if not isinstance(side, dict) or "score" not in side:
            raise ValueError("replay_fn must return {score,...}")
    return baseline, candidate


def _split_report(deltas: list[float], by_family: dict[str, list[float]],
                  noise_floor: float, seed: int) -> dict:
    mean_delta, low, high = stratified_bootstrap(
        by_family, B=2000, seed=seed)
    return {
        "n": len(deltas),
        "delta": round(mean_delta, 4),
        "ci95": [round(low, 4), round(high, 4)],
        "noise_floor": round(noise_floor, 4),
    }


def _insert_run(conn: sqlite3.Connection, *, role_slug: str, candidate_id: str | None,
                baseline_version: int, candidate_version: int, suite_id: str,
                suite_digest: str, held_in: dict, held_out: dict,
                diagnosis: dict, integrity: dict, cost: dict,
                verdict: str, status: str = "done", error: str | None = None,
                baseline_working_set_hash: str | None = None,
                candidate_working_set_hash: str | None = None,
                run_id: str | None = None,
                request_json: dict | None = None) -> str:
    now = now_iso()
    if run_id:
        conn.execute(
            "UPDATE eval_runs SET candidate_id=?, baseline_version=?, "
            "candidate_version=?, baseline_working_set_hash=?, "
            "candidate_working_set_hash=?, suite_id=?, suite_digest=?, "
            "held_in_json=?, held_out_json=?, diagnosis_json=?, integrity_json=?, "
            "cost_json=?, verdict=?, status=?, error=?, "
            "started_at=COALESCE(started_at, ?), finished_at=?, request_json=? "
            "WHERE id=?",
            (candidate_id, baseline_version, candidate_version,
             baseline_working_set_hash, candidate_working_set_hash,
             suite_id, suite_digest, jdumps(held_in), jdumps(held_out),
             jdumps(diagnosis), jdumps(integrity), jdumps(cost), verdict, status,
             error, now, now, jdumps(request_json or {}), run_id))
        conn.commit()
        return run_id
    run_id = new_id()
    conn.execute(
        "INSERT INTO eval_runs (id, role_slug, candidate_id, baseline_version, "
        "candidate_version, baseline_working_set_hash, candidate_working_set_hash, "
        "suite_id, suite_digest, held_in_json, held_out_json, "
        "diagnosis_json, integrity_json, cost_json, request_json, verdict, "
        "status, error, created_at, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, role_slug, candidate_id, baseline_version, candidate_version,
         baseline_working_set_hash, candidate_working_set_hash,
         suite_id, suite_digest, jdumps(held_in), jdumps(held_out),
         jdumps(diagnosis), jdumps(integrity), jdumps(cost),
         jdumps(request_json or {}), verdict, status,
         error, now, now, now))
    conn.commit()
    return run_id


def enqueue(conn: sqlite3.Connection, *, role_slug: str, suite_id: str,
            candidate_version: int, baseline_version: int,
            candidate_id: str | None = None, request: dict | None = None) -> dict:
    """Create a queued eval row; the EvalWorker fills it in-place."""
    run_id = new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO eval_runs (id, role_slug, candidate_id, baseline_version, "
        "candidate_version, suite_id, suite_digest, verdict, status, "
        "request_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, role_slug, candidate_id, baseline_version, candidate_version,
         suite_id, "", "inconclusive", "queued", jdumps(request or {}), now))
    conn.commit()
    return get_run(conn, run_id)  # type: ignore[return-value]


def evaluate(conn: sqlite3.Connection, *, role_slug: str, suite_id: str,
             candidate_version: int, baseline_version: int,
             replay_fn, candidate_id: str | None = None,
             noise_floor: float = 0.10, minimum_effect: float = MIN_EFFECT,
             seed: int = 7, runtime_id: str | None = None,
             model_id: str | None = None,
             baseline_working_set_hash: str | None = None,
             candidate_working_set_hash: str | None = None,
             run_id: str | None = None,
             request_json: dict | None = None) -> dict:
    """Run paired replay and return verdict + diagnosis; write one eval_runs row."""
    suite = eval_suite.load_suite(suite_id)
    integrity = {
        "suite_digest": suite["digest"],
        "suite_id": suite_id,
        "runtime_id": runtime_id,
        "model_id": model_id,
        "noise_floor": round(float(noise_floor), 4),
        "judge": None,
        "baseline_working_set_hash": baseline_working_set_hash,
        "candidate_working_set_hash": candidate_working_set_hash,
    }
    if not suite.get("ready"):
        diagnosis = {
            "verdict": "inconclusive", "reason": "suite_not_ready",
            "splits": {}, "dimensions": [], "regressions": [],
            "integrity": integrity, "recommended_actions": [],
        }
        run_id = _insert_run(
            conn, role_slug=role_slug, candidate_id=candidate_id,
            baseline_version=baseline_version, candidate_version=candidate_version,
            suite_id=suite_id, suite_digest=suite["digest"], held_in={}, held_out={},
            diagnosis=diagnosis, integrity=integrity, cost={}, verdict="inconclusive",
            baseline_working_set_hash=baseline_working_set_hash,
            candidate_working_set_hash=candidate_working_set_hash,
            run_id=run_id, request_json=request_json)
        _attach_eval(conn, candidate_id, run_id, "inconclusive")
        return {"run_id": run_id, "verdict": "inconclusive",
                "reason": "suite_not_ready", "splits": {}, "dimensions": [],
                "diagnosis": diagnosis, "integrity": integrity, "cost": {}}

    per_split: dict[str, list[float]] = {"held_in": [], "held_out": []}
    by_family: dict[str, dict[str, list[float]]] = {"held_in": {}, "held_out": {}}
    baseline_tokens = 0
    candidate_tokens = 0
    hard_failure = False
    forbidden = False
    for item in suite["items"]:
        split = item["split"]
        baseline, candidate = _replay_pair(
            item, replay_fn, baseline_version, candidate_version)
        delta = float(candidate["score"]) - float(baseline["score"])
        per_split.setdefault(split, []).append(delta)
        by_family.setdefault(split, {}).setdefault(item["family"], []).append(delta)
        baseline_tokens += int(baseline.get("tokens") or 0)
        candidate_tokens += int(candidate.get("tokens") or 0)
        # A baseline hard failure is a legitimate paired regression signal
        # (baseline fails, candidate passes); only the candidate's hard/
        # forbidden failure invalidates the candidate.
        hard_failure = hard_failure or bool(candidate.get("hard"))
        forbidden = forbidden or bool(candidate.get("forbidden"))
    splits = {
        name: _split_report(values, by_family.get(name, {}), noise_floor, seed)
        for name, values in per_split.items()
    }
    cost = {
        "baseline_tokens": baseline_tokens,
        "candidate_tokens": candidate_tokens,
        "ratio": (
            round(candidate_tokens / baseline_tokens, 4)
            if baseline_tokens else (0.0 if candidate_tokens == 0 else 999.0)
        ),
    }
    reason = ""
    if hard_failure:
        verdict, reason = "reject", "hard verifier failure"
    elif forbidden:
        verdict, reason = "reject", "forbidden changes"
    elif cost["ratio"] > MAX_COST_RATIO:
        verdict, reason = "reject", "cost ratio exceeds limit"
    elif any(s["delta"] < 0 for s in splits.values()):
        verdict, reason = "reject", "paired regression"
    elif any(s["n"] < MIN_PAIRS for s in splits.values()):
        verdict, reason = "inconclusive", "insufficient pairs"
    else:
        improved = [
            s for s in splits.values()
            if s["delta"] >= minimum_effect and s["ci95"][0] > noise_floor
        ]
        if improved:
            verdict, reason = "promote", "held-in/held-out paired gain"
        else:
            verdict, reason = "inconclusive", "gain within noise/minimum effect"

    dimensions = []
    for name, report in splits.items():
        status = "improved" if report["delta"] >= minimum_effect else (
            "regressed" if report["delta"] < 0 else "stable")
        dimensions.append({
            "name": name,
            "value": report["delta"],
            "delta": report["delta"],
            "ci95": report["ci95"],
            "status": status,
            "confidence": 0.0,
            "evidence_clusters": [],
            "suggested_operators": [],
        })
    diagnosis = {
        "schema_version": 1,
        "diagnosis_id": new_id(),
        "role_slug": role_slug,
        "policy": {"base_version": baseline_version,
                   "candidate_version": candidate_version,
                   "candidate_id": candidate_id,
                   "baseline_working_set_hash": baseline_working_set_hash,
                   "candidate_working_set_hash": candidate_working_set_hash},
        "verdict": verdict,
        "reason": reason,
        "splits": splits,
        "dimensions": dimensions,
        "regressions": [
            {"dimension": name, "severity": "medium"}
            for name, s in splits.items() if s["delta"] < 0
        ],
        "clusters": [],
        "integrity": integrity,
        "recommended_actions": [],
    }
    run_id = _insert_run(
        conn, role_slug=role_slug, candidate_id=candidate_id,
        baseline_version=baseline_version, candidate_version=candidate_version,
        suite_id=suite_id, suite_digest=suite["digest"],
        held_in=splits.get("held_in", {}), held_out=splits.get("held_out", {}),
        diagnosis=diagnosis, integrity=integrity, cost=cost, verdict=verdict,
        baseline_working_set_hash=baseline_working_set_hash,
        candidate_working_set_hash=candidate_working_set_hash,
        run_id=run_id, request_json=request_json)
    _attach_eval(conn, candidate_id, run_id, verdict)
    return {
        "run_id": run_id,
        "verdict": verdict,
        "reason": reason,
        "splits": splits,
        "dimensions": dimensions,
        "diagnosis": diagnosis,
        "integrity": integrity,
        "cost": cost,
    }


def _attach_eval(conn: sqlite3.Connection, candidate_id: str | None,
                 run_id: str, verdict: str | None = None) -> None:
    if not candidate_id:
        return
    try:
        from . import proposals
        proposals.attach_eval(conn, candidate_id, run_id)
        candidate = proposals.get_candidate(conn, candidate_id)
        if candidate is not None and verdict:
            if verdict == "reject":
                target = "rejected"
            else:
                # promote and inconclusive both go to human review; the eval
                # report carries the verdict, and inconclusive never auto-applies.
                target = "pending"
            if candidate["status"] == "evaluating":
                proposals.transition_candidate(
                    conn, candidate_id, target, decided_by="rsi_evaluator",
                    decision_note=f"eval {run_id}: {verdict}")
    except Exception:  # noqa: BLE001 —— 外部 candidate_id 不必是 proposal
        pass


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("held_in_json", "held_out_json", "diagnosis_json",
                "integrity_json", "cost_json", "request_json"):
        d[key.removesuffix("_json")] = json.loads(d.pop(key) or "{}")
    return d


def list_runs(conn: sqlite3.Connection, role_slug: str | None = None,
              limit: int = 50) -> list[dict]:
    if role_slug:
        rows = conn.execute(
            "SELECT * FROM eval_runs WHERE role_slug=? ORDER BY created_at DESC LIMIT ?",
            (role_slug, max(1, min(int(limit), 200)))).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key in ("held_in_json", "held_out_json", "diagnosis_json",
                    "integrity_json", "cost_json", "request_json"):
            d[key.removesuffix("_json")] = json.loads(d.pop(key) or "{}")
        out.append(d)
    return out
