"""RSI replay 验证器：strict 作业与 host-unsafe 回退（M4）。"""
from __future__ import annotations

import json

from anvil import config
from anvil.sandbox import jobs
from anvil.services import eval_runner


class FakeRunner:
    name = "fake"

    def __init__(self, exits):
        self.exits = exits

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        with open(paths.artifacts / "phases.jsonl", "w", encoding="utf-8") as fh:
            for i, code in enumerate(self.exits):
                fh.write(json.dumps({
                    "phase": "run", "name": f"check-{i}",
                    "exit_code": code, "duration_ms": 1,
                }) + "\n")
        log_path.write_text("fake", encoding="utf-8")
        return jobs.RunnerResult(exit_code=0, backend="fake")


def _item(checks):
    return {"verifier": {"checks": checks, "timeout_seconds": 30}}


def test_verify_strict_uses_job_phases(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_MODE", "enforce")
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    fake = FakeRunner([0, 2])
    monkeypatch.setattr(
        eval_runner.strict_backend, "select_runner",
        lambda: (fake, True, "ready"))
    item = _item([
        {"id": "a", "cmd": "true", "expect_exit": 0, "weight": 2},
        {"id": "b", "cmd": "false", "expect_exit": 2, "weight": 1},
    ])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    score, hard, failures = eval_runner.ReplayRunner._verify(item, workspace)
    assert score == 1.0
    assert hard is False
    assert failures == []


def test_verify_strict_unavailable_marks_hard_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_MODE", "enforce")
    monkeypatch.setattr(
        eval_runner.strict_backend, "select_runner",
        lambda: (FakeRunner([]), False, "no docker/wsl"))
    score, hard, failures = eval_runner.ReplayRunner._verify(
        _item([{"id": "a", "cmd": "true"}]), tmp_path)
    assert score == 0.0
    assert hard is True
    assert "strict sandbox unavailable" in failures[0]


def test_verify_host_unsafe_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_MODE", "host-unsafe")
    item = _item([
        {"id": "a", "cmd": "true", "expect_exit": 0, "weight": 1},
        {"id": "b", "cmd": "false", "expect_exit": 0, "weight": 1},
    ])
    score, hard, failures = eval_runner.ReplayRunner._verify(item, tmp_path)
    assert score == 0.5
    assert failures == ["b"]
    assert hard is False
