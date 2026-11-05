#!/usr/bin/env python3
"""用人工标注 JSONL 评估确定性知识质量门。

用法：
    python3 dev/scripts/knowledge_quality_eval.py cases.jsonl [--show]

每行：
    {"title": "...", "tldr": "...", "body_md": "...", "fields": {...},
     "kind": "insight", "expected_valid": true}

输出：TP/FP/TN/FN、precision、recall、漏放/误拦样本。
这是规则门基线；LLM 清洗效果应另用同一标注集对比，不得直接据此自动批准。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from anvil.services import knowledge_card  # noqa: E402


def evaluate(row: dict) -> dict:
    return knowledge_card.evaluate_quality(
        str(row.get("kind") or "insight"),
        str(row.get("title") or ""),
        str(row.get("tldr") or ""),
        str(row.get("body_md") or ""),
        row.get("fields") or {},
        authority=str(row.get("authority") or "reference"),
        source_task_id=row.get("source_task_id"),
        source_step_id=row.get("source_step_id"),
        source_role=row.get("source_role"),
        review_at=row.get("review_at"),
        expires_at=row.get("expires_at"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", help="标注 JSONL 路径")
    parser.add_argument("--show", action="store_true", help="列出误判样本")
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.cases).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    tp = fp = tn = fn = 0
    false_positive, false_negative = [], []
    for index, row in enumerate(rows, 1):
        expected = bool(row.get("expected_valid"))
        quality = evaluate(row)
        predicted = not quality["blocking"]
        if predicted and expected:
            tp += 1
        elif predicted and not expected:
            fp += 1
            false_positive.append((index, row.get("title"), quality["blocking"]))
        elif not predicted and not expected:
            tn += 1
        else:
            fn += 1
            false_negative.append((index, row.get("title"), quality["blocking"]))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    print(json.dumps({
        "cases": len(rows), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": round(precision, 4), "recall": round(recall, 4),
        "gate_version": 1,
    }, ensure_ascii=False, indent=2))
    if args.show:
        for label, items in (("误放", false_positive), ("误拦", false_negative)):
            for index, title, blocking in items:
                print(f"{label} #{index} {title}: {blocking}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
