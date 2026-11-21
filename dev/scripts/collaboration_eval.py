#!/usr/bin/env python3
"""协作方式判断评测（SDD/TDD）。

offline：只校验 fixtures 与提示词 contract，不调用模型，适合 CI。
model：逐场景调用可配置命令（例如 `claude -p`），解析 JSON，计算准确率。

示例：
    cd dev && python3 scripts/collaboration_eval.py --offline --json
    cd dev && python3 scripts/collaboration_eval.py \
        --command "claude -p" --out /tmp/collab-eval.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from anvil.graph import render  # noqa: E402
from anvil.services import workflow as workflow_service  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "collaboration_scenarios.json"
CRITICAL_IDS = {3, 4, 9, 13, 14, 15}
MODES = {"self", "subagent", "mention", "delegation", "dispatch", "cindy", "record"}


def load_scenarios() -> list[dict]:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios") or []
    if len(scenarios) < 15:
        raise SystemExit(f"fixtures must contain >= 15 scenarios, got {len(scenarios)}")
    return scenarios


def check_prompt_contract() -> list[str]:
    errors: list[str] = []
    collab = render.COLLABORATION_PROTOCOL
    for needle in (
        "先查 workflow", "已执行", "执行中", "尚未启动",
        "没有该 slice 的 run/上下文", "mention", "subagent", "委派", "派遣",
        "cindy", "blocking", "anvil task note", "不要唤醒", "同角色",
    ):
        if needle not in collab:
            errors.append(f"COLLABORATION_PROTOCOL missing: {needle}")
    if "目标角色 ≠ 当前 step 角色" not in render.DELEGATION_PROTOCOL:
        errors.append("DELEGATION_PROTOCOL missing same-role prohibition")
    if "能用当前 step 或" in render.DELEGATION_PROTOCOL:
        errors.append("DELEGATION_PROTOCOL still contains old absolute sentence")
    for needle in ("非阻塞", "不阻塞本 step", "不要等待", "回切仅在原切片仍 running"):
        if needle not in render.DISPATCH_PROTOCOL:
            errors.append(f"DISPATCH_PROTOCOL missing: {needle}")
    if "协作路由" not in workflow_service.SLICE_COLLAB_RULE:
        errors.append("SLICE_COLLAB_RULE missing")
    return errors


def offline_report(scenarios: list[dict]) -> dict:
    errors = check_prompt_contract()
    ids = {int(s.get("id")) for s in scenarios}
    missing = sorted(CRITICAL_IDS - ids)
    if missing:
        errors.append(f"missing critical scenarios: {missing}")
    for scenario in scenarios:
        modes = set(scenario.get("expected_modes") or [])
        if not modes or not modes.issubset(MODES):
            errors.append(f"scenario {scenario.get('id')}: invalid expected_modes {modes}")
        if not scenario.get("reason_keywords"):
            errors.append(f"scenario {scenario.get('id')}: missing reason_keywords")
        if not str(scenario.get("input") or "").strip():
            errors.append(f"scenario {scenario.get('id')}: empty input")
    return {
        "mode": "offline",
        "ok": not errors,
        "errors": errors,
        "scenarios": len(scenarios),
        "critical": sorted(CRITICAL_IDS & ids),
    }


def _model_prompt(scenario: dict) -> str:
    return (
        "你是 Anvil 的 workflow step 角色。请只依据以下协作规则，为场景选择唯一模式。\n\n"
        + render.COLLABORATION_PROTOCOL
        + "\n\n场景：\n"
        + str(scenario.get("input") or "")
        + "\n\n只输出 JSON，不要散文："
        '{"mode":"self|subagent|mention|delegation|dispatch|cindy|record",'
        '"blocking":true|false,"reason":"..."}'
    )


def _parse_json(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except ValueError:
            return None


def run_model(command: str, scenarios: list[dict], timeout: float = 120.0) -> dict:
    results: list[dict] = []
    for scenario in scenarios:
        prompt = _model_prompt(scenario)
        try:
            proc = subprocess.run(
                command, shell=True, input=prompt, text=True,
                capture_output=True, timeout=timeout)
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            results.append({
                "id": scenario["id"], "critical": bool(scenario.get("critical")),
                "ok": False, "error": str(exc), "mode": None, "reason": None,
            })
            continue
        parsed = _parse_json(output) or {}
        mode = str(parsed.get("mode") or "").strip().lower()
        reason = str(parsed.get("reason") or "")
        expected = [str(x).lower() for x in scenario.get("expected_modes") or []]
        keywords = [str(x).lower() for x in scenario.get("reason_keywords") or []]
        mode_ok = mode in expected
        keywords_ok = all(k in reason.lower() for k in keywords)
        critical = bool(scenario.get("critical"))
        results.append({
            "id": scenario["id"], "critical": critical,
            "ok": mode_ok and (keywords_ok or not critical),
            "mode": mode, "expected": expected,
            "mode_ok": mode_ok, "keywords_ok": keywords_ok,
            "reason": reason[:500],
        })
    total = len(results)
    mode_hits = sum(1 for r in results if r.get("mode_ok"))
    critical_results = [r for r in results if r.get("critical")]
    critical_ok = all(r.get("ok") for r in critical_results)
    overall = (mode_hits / total) if total else 0.0
    return {
        "mode": "model",
        "ok": critical_ok and overall >= 0.9,
        "total": total,
        "mode_accuracy": round(overall, 4),
        "critical_ok": critical_ok,
        "critical": critical_results,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Anvil collaboration routing eval")
    parser.add_argument("--offline", action="store_true", help="only contract/fixtures")
    parser.add_argument("--command", default="", help="model command; prompt on stdin")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--ids", default="", help="comma-separated scenario ids")
    parser.add_argument("--out", default="", help="write JSON report")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    scenarios = load_scenarios()
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",") if str(x).strip()}
        scenarios = [s for s in scenarios if int(s["id"]) in wanted]
        if not scenarios:
            raise SystemExit("no scenarios matched --ids")
    if args.command:
        report = run_model(args.command, scenarios, timeout=args.timeout)
    else:
        report = offline_report(scenarios)
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"mode={report['mode']} ok={report['ok']}")
        if report.get("errors"):
            for error in report["errors"]:
                print(f"- {error}", file=sys.stderr)
        if report["mode"] == "model":
            print(f"mode_accuracy={report['mode_accuracy']:.1%} "
                  f"critical_ok={report['critical_ok']}")
            for result in report["results"]:
                if not result.get("ok"):
                    print(f"- scenario {result['id']}: mode={result.get('mode')} "
                          f"expected={result.get('expected')}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
