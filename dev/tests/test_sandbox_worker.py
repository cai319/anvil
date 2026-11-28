"""严格档作业 worker：claim → execute → phases/artifacts/evidence（M2a）。"""
from __future__ import annotations

import json

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import sandbox
from anvil.services.sandbox_worker import SandboxJobWorker


class FakeRunner:
    name = "fake"

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        with open(paths.artifacts / "phases.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "phase": "run", "name": "poc", "exit_code": 139,
                "duration_ms": 12,
            }) + "\n")
        (paths.artifacts / "out").mkdir(parents=True, exist_ok=True)
        (paths.artifacts / "out" / "crash.txt").write_text("boom", encoding="utf-8")
        log_path.write_text("fake log", encoding="utf-8")
        return jobs.RunnerResult(
            exit_code=139, signal="SIGSEGV", image="fake:1", backend="fake")


def test_worker_runs_job_and_records_evidence(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    conn = connect(db_path)
    try:
        job = sandbox.create_job(
            conn, spec_raw={
                "network": "none",
                "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
                "expect": {"phase": "run", "signal": "SIGSEGV"},
                "artifacts": [{"path": "out/**"}],
            },
            actor_role="human", actor_is_human=True, check_available=False)
        worker = SandboxJobWorker(db_path, runner=FakeRunner())
        assert worker.run_once(conn) is True
        view = sandbox.job_view(conn, job["id"])
        assert view["status"] == "succeeded"
        assert view["phases"][0]["signal"] == "SIGSEGV"
        assert view["artifacts"][0]["rel_path"] == "out/crash.txt"
        assert view["evidence"]["verified"] is True
        assert view["log_dir"] and view["artifact_dir"]
    finally:
        conn.close()


class BrokenRunner:
    name = "broken"

    def available(self):
        return False, "no docker"

    def run(self, **kwargs):
        raise RuntimeError("docker unavailable")


def test_worker_marks_failure_and_keeps_going(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    conn = connect(db_path)
    try:
        job = sandbox.create_job(
            conn, spec_raw={
                "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
            },
            actor_role="human", actor_is_human=True, check_available=False)
        worker = SandboxJobWorker(db_path, runner=BrokenRunner())
        assert worker.run_once(conn) is True
        view = sandbox.get_job(conn, job["id"])
        assert view["status"] == "failed"
        assert "docker unavailable" in (view["failure_reason"] or "")
    finally:
        conn.close()
