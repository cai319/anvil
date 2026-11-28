"""受控 PoC 作业：spec 校验、脚本、快照、证据与产物（M2a）。"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.sandbox import jobs


def _spec(**overrides) -> dict:
    raw = {
        "task_id": "T1",
        "finding_id": "F1",
        "network": "none",
        "phases": {
            "build": [{"name": "build", "cmd": "make"}],
            "run": [{"name": "crash", "cmd": "./poc"}],
        },
        "artifacts": [{"path": "out/**"}],
        "reason": "verify",
    }
    raw.update(overrides)
    return raw


def test_parse_spec_defaults_and_safe_profile():
    spec = jobs.parse_spec(_spec())
    assert spec["profile"] == "safe"
    assert spec["network"] == "none"
    assert spec["resources"]["timeout_seconds"] == jobs.DEFAULT_RESOURCES["timeout_seconds"]
    assert spec["phases"]["run"][0]["timeout_seconds"] == 600
    assert len(jobs.spec_hash(spec)) == 64


def test_parse_spec_requires_run_command():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(phases={"build": [{"cmd": "make"}]}))


def test_parse_spec_rejects_bad_command_and_env():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(phases={"run": [{"cmd": "x", "argv": ["y"]}]}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(phases={"run": [{"cmd": "x", "env": {"ANVIL_TOKEN": "x"}}]}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(phases={"run": [{"cmd": "x", "env": {"LD_PRELOAD": "y"}}]}))


def test_parse_spec_rejects_traversal_and_hard_limits():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(artifacts=[{"path": "../escape"}]))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(artifacts=[{"path": "/abs"}]))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(resources={"rss_mib": jobs.HARD_MAX["rss_mib"] + 1}))
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(network="host"))


def test_requested_safe_profile_must_be_safe():
    with pytest.raises(jobs.JobSpecError):
        jobs.parse_spec(_spec(profile="safe", network="bridge"))
    relaxed = jobs.parse_spec(_spec(profile="relaxed", network="bridge"))
    assert relaxed["profile"] == "relaxed"


def test_build_script_has_phases_and_quoting():
    spec = jobs.parse_spec(_spec(
        phases={"run": [{"name": "quote", "cmd": "echo 'a b'"}]}))
    script = jobs.build_script(spec)
    assert "run_step run quote 600" in script
    assert "cp -a --no-preserve=ownership /src/. /scratch/work/" in script
    assert "phases.jsonl" in script


class FakeRunner:
    name = "fake"

    def __init__(self, phase_exit=0, files=None, timed_out=False):
        self.phase_exit = phase_exit
        self.files = files or {}
        self.timed_out = timed_out

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        with open(paths.artifacts / "phases.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "phase": "run", "name": "crash",
                "exit_code": self.phase_exit, "duration_ms": 5,
            }) + "\n")
        for name, content in self.files.items():
            target = paths.artifacts / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        log_path.write_text("fake log", encoding="utf-8")
        return jobs.RunnerResult(
            exit_code=self.phase_exit, timed_out=self.timed_out,
            image="fake:1", backend="fake")


def test_execute_job_success_collects_evidence_and_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    ws = tmp_path / "task-workspaces" / "T1"
    ws.mkdir(parents=True)
    (ws / "src.c").write_text("int main(void){return 0;}\n")
    spec = jobs.parse_spec(_spec(
        expect={"phase": "run", "exit_code": 0},
        artifacts=[{"path": "out/**"}],
    ))
    execution = jobs.execute_job(spec, "job-1", FakeRunner(
        files={"out/result.txt": "ok"}))
    assert execution.status == "succeeded"
    assert execution.failure_reason is None
    assert execution.phases[0]["phase"] == "run"
    assert execution.artifacts[0]["rel_path"] == "out/result.txt"
    assert execution.evidence["verified"] is True
    assert execution.evidence["source_tree_hash"].startswith("sha256:")
    paths = jobs.job_paths("job-1")
    assert not paths.source.exists()
    assert not paths.scratch.exists()
    assert (paths.artifacts / "out" / "result.txt").exists()


def test_execute_job_expected_signal_marks_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    spec = jobs.parse_spec(_spec(expect={"phase": "run", "signal": "SIGSEGV"}))
    execution = jobs.execute_job(spec, "job-2", FakeRunner(phase_exit=139))
    assert execution.status == "succeeded"
    assert execution.phases[0]["signal"] == "SIGSEGV"


def test_execute_job_no_expect_crash_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    spec = jobs.parse_spec(_spec())
    execution = jobs.execute_job(spec, "job-3", FakeRunner(phase_exit=139))
    assert execution.status == "failed"
    assert execution.failure_reason == "sandbox_run_failed"


def test_execute_job_timeout_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    spec = jobs.parse_spec(_spec())
    execution = jobs.execute_job(
        spec, "job-4", FakeRunner(phase_exit=124, timed_out=True))
    assert execution.status == "failed"
    assert execution.failure_reason == "sandbox_timeout"


def test_artifact_oversize_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    spec = jobs.parse_spec(_spec(
        expect={"phase": "run", "exit_code": 0},
        artifacts=[{"path": "big.txt", "max_bytes": 2}]))
    execution = jobs.execute_job(
        spec, "job-5", FakeRunner(files={"big.txt": "too large"}))
    assert execution.status == "failed"
    assert execution.failure_reason == "sandbox_artifact_limit"
