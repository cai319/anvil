"""检查作业服务/worker：kind 区分、任务绑定、审批与取证（v1.62 #2）。"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import sandbox, tasks as tasks_service
from anvil.services.sandbox_worker import SandboxJobWorker


def _check_raw(**overrides) -> dict:
    raw = {
        "task_id": "T1",
        "network": "none",
        "check": {
            "id": "c1",
            "kind": "test",
            "cmd": "pytest -q --junit-xml=/out/junit.xml",
            "junit": ["junit.xml"],
        },
    }
    raw.update(overrides)
    return raw


def _poc_raw(**overrides) -> dict:
    raw = {
        "network": "none",
        "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
    }
    raw.update(overrides)
    return raw


JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2" time="0.01">
  <testcase classname="t" name="a"/><testcase classname="t" name="b"/>
</testsuite></testsuites>
"""


class FakeCheckRunner:
    name = "fake"

    def __init__(self, exit_code=0, timed_out=False, junit=JUNIT_PASS):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.junit = junit

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        name = spec["phases"]["run"][0]["name"]
        with open(paths.artifacts / "phases.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "phase": "run", "name": name,
                "exit_code": self.exit_code, "duration_ms": 3,
            }) + "\n")
        if self.junit is not None:
            (paths.artifacts / "junit.xml").write_text(self.junit, encoding="utf-8")
        log_path.write_text("check log", encoding="utf-8")
        return jobs.RunnerResult(
            exit_code=self.exit_code, timed_out=self.timed_out,
            signal=jobs._signal_name(self.exit_code), backend="fake")


def _conn(tmp_path):
    return connect(str(tmp_path / "t.db"))


def test_create_check_job_without_finding(tmp_path):
    conn = _conn(tmp_path)
    try:
        tasks_service.create(conn, id="T1", title="t", assignees=["pm"])
        job = sandbox.create_job(
            conn, spec_raw=_check_raw(), actor_role="human",
            actor_is_human=True, kind="check", check_available=False)
        assert job["kind"] == "check"
        assert job["task_id"] == "T1"
        assert job["finding_id"] is None
        assert job["tier"] == "standard"
        assert job["spec"]["network"] == "none"
        assert job["status"] == "queued"
        assert sandbox.list_jobs(conn, kind="check")[0]["id"] == job["id"]
        assert sandbox.list_jobs(conn, kind="poc") == []
    finally:
        conn.close()


def test_agent_relaxed_check_waits_for_approval(tmp_path):
    conn = _conn(tmp_path)
    try:
        tasks_service.create(conn, id="T1", title="t", assignees=["pm"])
        safe = sandbox.create_job(
            conn, spec_raw=_check_raw(), actor_role="pm",
            actor_is_human=False, kind="check", check_available=False)
        assert safe["status"] == "queued"
        relaxed = sandbox.create_job(
            conn, spec_raw=_check_raw(network="bridge"), actor_role="pm",
            actor_is_human=False, kind="check", check_available=False)
        assert relaxed["status"] == "waiting_approval"
        approved = sandbox.approve_job(conn, relaxed["id"], actor_role="human")
        assert approved["status"] == "queued"
    finally:
        conn.close()


def test_check_job_requires_task_binding(tmp_path):
    conn = _conn(tmp_path)
    try:
        with pytest.raises(jobs.JobSpecError):
            sandbox.create_job(
                conn, spec_raw=_check_raw(task_id=None), actor_role="pm",
                actor_is_human=False, kind="check", check_available=False)
    finally:
        conn.close()


def test_worker_records_check_result(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    conn = connect(db_path)
    try:
        tasks_service.create(conn, id="T1", title="t")
        job = sandbox.create_job(
            conn, spec_raw=_check_raw(), actor_role="human",
            actor_is_human=True, kind="check", check_available=False)
        worker = SandboxJobWorker(db_path, runner=FakeCheckRunner())
        assert worker.run_once(conn) is True
        view = sandbox.job_view(conn, job["id"])
        assert view["kind"] == "check"
        assert view["status"] == "succeeded"
        result = view["check_result"]
        assert result["run_id"] == job["id"]
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        assert result["collected_count"] == 2
        assert result["junit_summary"]["tests"] == 2
        assert result["env_hash"].startswith("sha256:")
        assert view["evidence"]["check"]["status"] == "passed"
    finally:
        conn.close()


def test_worker_records_timeout_distinct(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    conn = connect(db_path)
    try:
        tasks_service.create(conn, id="T1", title="t")
        job = sandbox.create_job(
            conn, spec_raw=_check_raw(), actor_role="human",
            actor_is_human=True, kind="check", check_available=False)
        runner = FakeCheckRunner(exit_code=124, timed_out=True, junit=None)
        SandboxJobWorker(db_path, runner=runner).run_once(conn)
        view = sandbox.job_view(conn, job["id"])
        assert view["status"] == "failed"
        assert view["failure_reason"] == "check_timeout"
        assert view["check_result"]["status"] == "timeout"
        assert view["check_result"]["timed_out"] is True
    finally:
        conn.close()


def test_poc_jobs_still_default_kind(tmp_path):
    conn = _conn(tmp_path)
    try:
        tasks_service.create(conn, id="T1", title="t")
        job = sandbox.create_job(
            conn, spec_raw=_poc_raw(task_id="T1"), actor_role="human",
            actor_is_human=True, check_available=False)
        assert job["kind"] == "poc"
        assert job["tier"] == "strict"
        assert job["check_result"] is None
    finally:
        conn.close()
