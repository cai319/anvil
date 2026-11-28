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


def test_parse_check_spec_pytest_adapter_builds_collect_and_run():
    spec = jobs.parse_check_spec(_check_spec(
        check={"id": "c1", "kind": "test",
               "pytest": {"paths": ["tests/test_a.py"],
                          "args": ["-q"], "plugins": ["pytest_timeout"]}}))
    assert spec["check"]["pytest"]["paths"] == ["tests/test_a.py"]
    assert spec["check"]["junit"] == ["junit.xml"]
    assert spec["check"]["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert len(spec["phases"]["run"]) == 2
    collect, run = spec["phases"]["run"]
    assert "--collect-only" in collect["cmd"]
    assert "/out/collect.txt" in collect["cmd"]
    assert "--junit-xml=/out/junit.xml" in run["argv"]
    assert jobs.environment_hash(spec).startswith("sha256:")


def test_parse_check_spec_pytest_validation():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "command", "pytest": {"paths": ["tests"]}}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "test", "cmd": "pytest",
                   "pytest": {"paths": ["tests"]}}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "test", "pytest": {"paths": ["tests"], "x": 1}}))


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
        check={"kind": "command", "cmd": "true", "exit_code": 1}))
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(), spec=spec)
    result = execution.evidence["check"]
    assert result["status"] == "passed"
    assert result["junit_summary"] is None
    assert result["collected_count"] is None
    assert result["exit_code"] == 0
    assert result["env_hash"].startswith("sha256:")


# --- command / file_assert / manual（v1.62 #5）------------------------------


def test_parse_command_requires_declared_red_exit_code():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "command", "cmd": "python -m app --check"}))
    for bad in (0, 256, -1, "1", True):
        with pytest.raises(jobs.JobSpecError):
            jobs.parse_check_spec(_check_spec(
                check={"kind": "command", "cmd": "false", "exit_code": bad}))


def test_parse_command_records_red_exit_code_and_environment_hash():
    spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "command", "cmd": "false", "exit_code": 2}))
    assert spec["check"]["kind"] == "command"
    assert spec["check"]["red_exit_code"] == 2
    assert spec["check"]["cmd"] == "false"
    other = jobs.parse_check_spec(_check_spec(
        check={"kind": "command", "cmd": "false", "exit_code": 3}))
    assert jobs.environment_hash(spec) != jobs.environment_hash(other)


def test_parse_file_assert_is_native_without_run_command():
    spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "file_assert", "path": "reports/out.md",
               "contains": "PASS"}))
    assert spec["check"]["kind"] == "file_assert"
    assert spec["check"]["path"] == "reports/out.md"
    assert spec["check"]["contains"] == "PASS"
    assert spec["phases"]["run"] == []
    assert spec["artifacts"] == []


def test_parse_file_assert_rejects_command_fields_and_bad_paths():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "file_assert", "path": "reports/out.md",
                   "cmd": "true"}))
    for bad in ("/abs.md", "../escape.md"):
        with pytest.raises(jobs.JobSpecError):
            jobs.parse_check_spec(_check_spec(
                check={"kind": "file_assert", "path": bad}))


def test_parse_manual_check_is_rejected_as_not_engine_executed():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_check_spec(_check_spec(
            check={"kind": "manual", "judge": "human",
                   "method": "read the diff"}))


def _write_workspace(tmp_path, rel_path: str, content: str) -> None:
    target = tmp_path / "task-workspaces" / "T1" / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_execute_command_declared_red_exit_is_valid_red(tmp_path, monkeypatch):
    spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "command", "cmd": "false", "exit_code": 1}))
    execution = _run(tmp_path, monkeypatch,
                     FakeCheckRunner(exit_code=1), spec=spec)
    result = execution.evidence["check"]
    assert execution.status == "failed"
    assert result["classification"] == "valid_red"
    assert result["cause"] is None
    assert result["red_exit_code"] == 1


def test_execute_command_undeclared_exit_is_harness_error(tmp_path, monkeypatch):
    spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "command", "cmd": "false", "exit_code": 1}))
    execution = _run(tmp_path, monkeypatch,
                     FakeCheckRunner(exit_code=2), spec=spec)
    result = execution.evidence["check"]
    assert result["classification"] == "harness_error"
    assert result["cause"] == "unexpected_exit_code"


def test_execute_command_zero_exit_is_passed(tmp_path, monkeypatch):
    spec = jobs.parse_check_spec(_check_spec(
        check={"kind": "command", "cmd": "true", "exit_code": 1}))
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(), spec=spec)
    result = execution.evidence["check"]
    assert execution.status == "succeeded"
    assert result["classification"] == "passed"


def _file_assert_spec(**overrides):
    check = {"kind": "file_assert", "path": "reports/out.md", "contains": "PASS"}
    check.update(overrides)
    return jobs.parse_check_spec(_check_spec(check=check))


def test_execute_file_assert_missing_file_is_trivial_red(tmp_path, monkeypatch):
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(),
                     spec=_file_assert_spec())
    result = execution.evidence["check"]
    assert execution.status == "failed"
    assert result["classification"] == "trivial_red"
    assert result["cause"] == "file_missing"


def test_execute_file_assert_content_mismatch_is_valid_red(tmp_path, monkeypatch):
    _write_workspace(tmp_path, "reports/out.md", "FAIL")
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(),
                     spec=_file_assert_spec())
    result = execution.evidence["check"]
    assert execution.status == "failed"
    assert result["classification"] == "valid_red"
    assert result["cause"] == "content_mismatch"


def test_execute_file_assert_matching_content_is_passed(tmp_path, monkeypatch):
    _write_workspace(tmp_path, "reports/out.md", "status: PASS\n")
    execution = _run(tmp_path, monkeypatch, FakeCheckRunner(),
                     spec=_file_assert_spec())
    result = execution.evidence["check"]
    assert execution.status == "succeeded"
    assert result["classification"] == "passed"
    assert result["kind"] == "file_assert"
