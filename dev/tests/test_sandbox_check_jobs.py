"""检查作业：spec 校验、junit 取证、超时/信号分类与环境哈希（v1.62 #2）。"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.sandbox import jobs


def _check_spec(**overrides) -> dict:
    raw = {
        "task_id": "T1",
        "check": {
            "id": "c1",
            "kind": "test",
            "cmd": "pytest -q --junit-xml=/out/junit.xml",
            "junit": ["junit.xml"],
        },
        "network": "none",
    }
    raw.update(overrides)
    return raw


def test_parse_check_spec_defaults_and_no_finding_required():
    spec = jobs.parse_check_spec(_check_spec())
    assert spec["kind"] == "check"
    assert spec["task_id"] == "T1"
    assert spec["check"]["id"] == "c1"
    assert spec["check"]["kind"] == "test"
    assert spec["network"] == "none"
    assert spec["tier"] == "standard"
    assert spec["profile"] == "safe"
    assert spec["finding_id"] is None
    assert spec["phases"]["run"][0]["name"] == "c1"
    assert [a["path"] for a in spec["artifacts"]] == ["junit.xml"]


def test_parse_check_spec_requires_task_and_check():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(task_id=None))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec({"task_id": "T1"})


def test_parse_check_spec_validates_kind_and_command():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "nonsense", "cmd": "true"}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "test", "cmd": "x", "argv": ["y"]}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(check={"kind": "test"}))
    argv_spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "test", "argv": ["true"]}))
    assert argv_spec["check"]["argv"] == ["true"]


def test_parse_check_spec_rejects_junit_traversal():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "test", "cmd": "true", "junit": ["../escape.xml"]}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "test", "cmd": "true", "junit": ["/abs.xml"]}))


def test_parse_check_spec_bridge_is_relaxed():
    spec = jobs.parse_check_spec(_check_spec(network="bridge"))
    assert spec["profile"] == "relaxed"
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(network="host"))


def test_environment_hash_is_stable_and_sensitive():
    a = jobs.parse_check_spec(_check_spec())
    b = jobs.parse_check_spec(_check_spec())
    assert jobs.environment_hash(a) == jobs.environment_hash(b)
    assert jobs.environment_hash(a).startswith("sha256:")
    c = jobs.parse_check_spec(_check_spec(
        check={"kind": "test", "cmd": "pytest -q"}))
    assert jobs.environment_hash(a) != jobs.environment_hash(c)
    d = jobs.parse_check_spec(_check_spec(network="bridge"))
    assert jobs.environment_hash(a) != jobs.environment_hash(d)


JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="1" tests="3" time="0.02">
  <testcase classname="tests.test_a" name="test_one"/>
  <testcase classname="tests.test_a" name="test_two"/>
  <testcase classname="tests.test_a" name="test_three"><skipped/></testcase>
</testsuite></testsuites>
"""

JUNIT_FAIL = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="0" tests="4" time="0.03">
  <testcase classname="tests.test_a" name="test_one"><failure message="boom"/></testcase>
  <testcase classname="tests.test_a" name="test_two"><failure message="boom"/></testcase>
  <testcase classname="tests.test_a" name="test_three"><error message="err"/></testcase>
  <testcase classname="tests.test_a" name="test_four"/>
</testsuite></testsuites>
"""


class FakeCheckRunner:
    name = "fake"

    def __init__(self, exit_code=0, junit=None, timed_out=False, error=None):
        self.exit_code = exit_code
        self.junit = junit
        self.timed_out = timed_out
        self.error = error

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        name = spec["phases"]["run"][0]["name"]
        with open(paths.artifacts / "phases.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "phase": "run", "name": name,
                "exit_code": self.exit_code, "duration_ms": 7,
            }) + "\n")
        if self.junit is not None:
            (paths.artifacts / "junit.xml").write_text(self.junit, encoding="utf-8")
        log_path.write_text("check log", encoding="utf-8")
        return jobs.RunnerResult(
            exit_code=self.exit_code, timed_out=self.timed_out,
            signal=jobs._signal_name(self.exit_code),
            image="fake:1", backend="fake", error=self.error)


def _run(tmp_path, monkeypatch, runner, spec=None):
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    (tmp_path / "task-workspaces" / "T1").mkdir(parents=True, exist_ok=True)
    parsed = spec or jobs.parse_check_spec(_check_spec())
    return jobs.execute_check_job(parsed, "chk-1", runner)


def test_execute_check_passed_records_structured_evidence(tmp_path, monkeypatch):
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(junit=JUNIT_PASS))
    result = execution.evidence["check"]
    assert execution.status == "succeeded"
    assert result["run_id"] == "chk-1"
    assert result["check_id"] == "c1"
    assert result["exit_code"] == 0
    assert result["signal"] is None
    assert result["timed_out"] is False
    assert result["status"] == "passed"
    assert result["collected_count"] == 3
    assert result["junit_summary"] == {
        "tests": 3, "failures": 0, "errors": 0, "skipped": 1}
    assert result["env_hash"].startswith("sha256:")


def test_execute_check_nonzero_exit_is_distinct_from_signal(tmp_path, monkeypatch):
    execution = _run(tmp_path, monkeypatch,
                     FakeCheckRunner(exit_code=1, junit=JUNIT_FAIL))
    result = execution.evidence["check"]
    assert execution.status == "failed"
    assert execution.failure_reason == "check_failed"
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["signal"] is None
    assert result["timed_out"] is False
    assert result["junit_summary"] == {
        "tests": 4, "failures": 2, "errors": 1, "skipped": 0}


def test_execute_check_signal_is_distinct(tmp_path, monkeypatch):
    execution = _run(tmp_path, monkeypatch,
                     FakeCheckRunner(exit_code=139, junit=JUNIT_FAIL))
    result = execution.evidence["check"]
    assert execution.status == "failed"
    assert execution.failure_reason == "check_signal"
    assert result["status"] == "signal"
    assert result["signal"] == "SIGSEGV"
    assert result["timed_out"] is False


def test_execute_check_timeout_is_distinct(tmp_path, monkeypatch):
    execution = _run(tmp_path, monkeypatch,
                     FakeCheckRunner(exit_code=124, timed_out=True))
    result = execution.evidence["check"]
    assert execution.status == "failed"
    assert execution.failure_reason == "check_timeout"
    assert result["status"] == "timeout"
    assert result["timed_out"] is True


def test_execute_check_without_junit_still_records_core_fields(tmp_path, monkeypatch):
    spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "command", "cmd": "true"}))
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(), spec=spec)
    result = execution.evidence["check"]
    assert result["status"] == "passed"
    assert result["junit_summary"] is None
    assert result["collected_count"] is None
    assert result["exit_code"] == 0
    assert result["env_hash"].startswith("sha256:")
