"""Docker 严格档 runner 的命令构建与可用性（M2a；真实容器需 ANVIL_SANDBOX_IT=1）。"""
from __future__ import annotations

import os

import pytest

from anvil.sandbox import jobs
from anvil.sandbox.backends import docker


def _paths(tmp_path):
    return jobs.JobPaths(
        root=tmp_path, source=tmp_path / "src", script=tmp_path / "job" / "job.sh",
        logs=tmp_path / "logs", artifacts=tmp_path / "out",
        scratch=tmp_path / "scratch")


def _spec():
    return jobs.parse_spec({
        "network": "none",
        "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
    })


def test_docker_cmd_has_isolation_flags(tmp_path):
    spec = _spec()
    cmd = docker._run_cmd("docker", spec, _paths(tmp_path), "img:1", "anvil-poc-x")
    joined = " ".join(cmd)
    assert "--network none" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined
    assert "--memory 8192m" in joined
    assert f"type=bind,src={tmp_path / 'src'},dst=/src,readonly" in joined
    assert f"type=bind,src={tmp_path / 'out'},dst=/out" in joined
    assert cmd[-3:] == ["img:1", "/bin/bash", "/job/job.sh"]


def test_available_without_cli(monkeypatch):
    docker.reset_for_tests()
    monkeypatch.setattr(docker, "_cli", lambda: None)
    ok, detail = docker.available(refresh=True)
    assert ok is False
    assert "not found" in detail


def test_image_build_disabled_when_missing(monkeypatch):
    monkeypatch.setattr(docker, "_image_present", lambda cli, image: False)
    monkeypatch.setenv("ANVIL_SANDBOX_SKIP_IMAGE_BUILD", "1")
    with pytest.raises(RuntimeError):
        docker.ensure_image("docker", "missing:1", open(os.devnull, "wb"))


@pytest.mark.skipif(
    os.environ.get("ANVIL_SANDBOX_IT") != "1" or not docker.available()[0],
    reason="docker integration requires ANVIL_SANDBOX_IT=1 and a running docker",
)
def test_real_docker_job_smoke(tmp_path, monkeypatch):
    from anvil import config
    monkeypatch.setattr(config, "SANDBOX_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    spec = jobs.parse_spec({
        "network": "none",
        "phases": {"run": [{"name": "true", "cmd": "true"}]},
        "expect": {"phase": "run", "exit_code": 0},
    })
    execution = jobs.execute_job(spec, "job-it", docker.DockerRunner())
    assert execution.status == "succeeded"
