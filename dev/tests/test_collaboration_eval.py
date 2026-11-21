"""协作方式判断评测：offline contract 始终跑；模型评测需显式环境变量。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collaboration_eval.py"
FIXTURES = ROOT / "tests" / "fixtures" / "collaboration_scenarios.json"


def test_fixtures_have_required_scenarios():
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    ids = {int(item["id"]) for item in scenarios}
    assert len(scenarios) >= 15
    assert {3, 4, 9, 13, 14, 15}.issubset(ids)
    for item in scenarios:
        assert item["expected_modes"]
        assert item["reason_keywords"]
        assert item["input"].strip()


def test_offline_eval_passes():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--offline", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["mode"] == "offline"
    assert report["scenarios"] >= 15


@pytest.mark.skipif(
    not os.environ.get("ANVIL_COLLAB_EVAL_CMD"),
    reason="set ANVIL_COLLAB_EVAL_CMD to run model-based collaboration eval")
def test_model_eval_thresholds():
    command = os.environ["ANVIL_COLLAB_EVAL_CMD"]
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--command", command, "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["critical_ok"] is True
    assert report["mode_accuracy"] >= 0.9
