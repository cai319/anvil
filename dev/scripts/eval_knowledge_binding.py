#!/usr/bin/env python3
"""离线评估任务 → 知识 repo 路由（只读，不写库）。

用法（dev 根目录）：
    .venv/bin/python scripts/eval_knowledge_binding.py
    .venv/bin/python scripts/eval_knowledge_binding.py --db data/anvil.db --json
    .venv/bin/python scripts/eval_knowledge_binding.py --no-gate

指标：precision@1、precision@3、recall@3、auto-apply precision。
门禁（默认）：precision@1 ≥ 0.80，auto-apply precision ≥ 0.90；不达标时先切
`ANVIL_KNOWLEDGE_BINDING_MODE=suggest` 校准，再回到 auto。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEV / "backend"))

from anvil import config  # noqa: E402
from anvil.db import connect, open_conn  # noqa: E402
from anvil.services import repo_catalog  # noqa: E402


def _auto_selection(candidates: list[dict], has_tags: bool) -> list[str]:
    high = float(getattr(config, "KNOWLEDGE_BINDING_HIGH", 0.62))
    suggest_at = float(getattr(config, "KNOWLEDGE_BINDING_SUGGEST", 0.38))
    gap = float(getattr(config, "KNOWLEDGE_BINDING_GAP", 0.15))
    max_repos = max(1, int(getattr(config, "KNOWLEDGE_BINDING_MAX_REPOS", 3)))
    if not candidates:
        return []
    top = candidates[0]["score"]
    second = candidates[1]["score"] if len(candidates) > 1 else 0.0
    chosen: list[str] = []
    for cand in candidates:
        score = float(cand.get("score") or 0)
        if score < suggest_at:
            continue
        evidence = cand.get("matched_tags") or (cand.get("components") or {}).get("lexical", 0) >= 0.25
        if not evidence:
            continue
        if has_tags:
            if score >= high and (not chosen or top - score <= gap):
                chosen.append(cand["repo"])
        else:
            components = cand.get("components") or {}
            if ((components.get("lexical") or 0) >= 0.65
                    and (components.get("prior") or 0) >= 0.40
                    and top - second >= 0.20 and score == top):
                chosen.append(cand["repo"])
        if len(chosen) >= max_repos:
            break
    return chosen


def _ratio(hit: int, total: int) -> float:
    return round(hit / total, 4) if total else 1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(config.SQLITE_PATH))
    parser.add_argument("--cases", default=str(
        DEV / "tests" / "fixtures" / "knowledge_binding_cases.json"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    db_path = Path(args.db)
    conn = open_conn(args.db) if db_path.exists() else connect(args.db)
    details = []
    p1_hits = p3_hits = recall_hits = recall_total = 0
    auto_total = auto_hits = 0
    needs_cindy_total = needs_cindy_ok = 0
    try:
        for case in cases:
            result = repo_catalog.suggest(
                conn,
                title=case.get("title") or "",
                description=case.get("description") or "",
                tags=case.get("tags") or [],
                exec_repo=case.get("repo"),
                workspace_id=case.get("workspace_id"),
                limit=5,
                use_cache=False,
            )
            candidates = result.get("candidates") or []
            expected = list(dict.fromkeys(case.get("expected") or []))
            predicted = [c["repo"] for c in candidates]
            top1 = predicted[:1]
            top3 = predicted[:3]
            p1 = 1 if top1 and top1[0] in expected else 0
            p3 = len(set(top3) & set(expected)) / len(top3) if top3 else 0.0
            recall = len(set(top3) & set(expected)) / len(expected) if expected else 1.0
            auto = _auto_selection(candidates, bool(case.get("tags")))
            auto_precision = (
                len(set(auto) & set(expected)) / len(auto) if auto else 0.0)
            if auto:
                auto_total += 1
                auto_hits += 1 if auto_precision >= 0.999 else 0
            if expected:
                p1_hits += p1
                p3_hits += p3
                recall_hits += recall
                recall_total += 1
            if case.get("expect_needs_cindy"):
                needs_cindy_total += 1
                needs_cindy_ok += 1 if not auto else 0
            details.append({
                "id": case.get("id"), "expected": expected,
                "top1": top1, "top3": top3, "predicted": predicted,
                "auto_applied": auto, "auto_precision": round(auto_precision, 4),
                "precision@1": p1, "precision@3": round(p3, 4),
                "recall@3": round(recall, 4),
            })
    finally:
        conn.close()

    metrics = {
        "cases": len(details),
        "cases_with_expected": recall_total,
        "precision@1": _ratio(p1_hits, recall_total),
        "precision@3": round(p3_hits / recall_total, 4) if recall_total else 1.0,
        "recall@3": _ratio(recall_hits, recall_total),
        "auto_apply_cases": auto_total,
        "auto_apply_precision": _ratio(auto_hits, auto_total),
        "needs_cindy_cases": needs_cindy_total,
        "needs_cindy_ok": _ratio(needs_cindy_ok, needs_cindy_total),
    }
    gate_ok = (metrics["precision@1"] >= 0.80
               and metrics["auto_apply_precision"] >= 0.90
               and metrics["needs_cindy_ok"] >= 0.90)
    out = {"db": args.db, "metrics": metrics, "gate_ok": gate_ok, "details": details}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"db: {args.db}")
        for key, value in metrics.items():
            print(f"{key:>22}: {value}")
        print(f"{'gate':>22}: {'PASS' if gate_ok else 'FAIL'}")
        for item in details:
            print(f"- {item['id']}: top1={item['top1']} auto={item['auto_applied']} "
                  f"expected={item['expected']}")
    return 0 if (gate_ok or args.no_gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
