"""切片 scope 提交 → 引擎在 base 上红跑 → 证据分类（v1.62 #4）。"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import sandbox, tasks as tasks_service
from anvil.services import workflow as wf_service
from anvil.services.sandbox_worker import SandboxJobWorker
from anvil.util import ConflictError, now_iso

WF = {
    "steps": [
        {"id": "P1", "title": "切片计划", "role": "lead", "kind": "plan_slices"},
    ],
    "loop": {
        "from": "P1",
        "scope": {"role": "lead", "title": "切片 spec"},
        "execute": {"role": "auditor", "title": "执行"},
        "verify": {"role": "reviewer", "title": "验证"},
        "fix": {"role": "developer", "title": "修复"},
        "verify2": {"role": "reviewer", "title": "复验"},
        "decide": {"role": "lead", "title": "决策", "max_rework": 2},
    },
}
PLAN = {"rationale": "单切片", "slices": [
    {"id": "auth", "title": "认证", "scope_paths": ["src/auth/**"],
     "execute_steps": [{"id": "impl", "title": "实现"}]},
]}


def _slice_spec() -> dict:
    return {
        "acceptance": [{
            "id": "a1",
            "kind": "test",
            "criterion": "stub raises NotImplementedError until implemented",
            "verification": {"pytest": {"paths": ["tests/test_auth.py"], "args": ["-q"]}},
        }],
        "seam": "auth service",
        "out_of_scope": ["web UI"],
        "contract": "def login(user) -> bool",
        "deliverables": ["src/auth/service.py"],
        "stubs": [{"path": "src/auth/service.py",
                   "signatures": ["def login(user: str) -> bool"]}],
    }


JUNIT_NOT_IMPLEMENTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login">
    <failure message="NotImplementedError: stub">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""

JUNIT_VALUE_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login">
    <failure message="ValueError: boom">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""


class FakePytestRunner:
    """模拟沙箱：collect 基线 + pytest 执行，写出 phases/collect/junit。"""

    name = "fake"

    def __init__(self, *, exit_code=1, junit=JUNIT_NOT_IMPLEMENTED,
                 collected=1, timed_out=False):
        self.exit_code = exit_code
        self.junit = junit
        self.collected = collected
        self.timed_out = timed_out

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        lines = []
        for index, phase in enumerate(spec["phases"]["run"]):
            code = 0 if index == 0 else self.exit_code
            lines.append(json.dumps({
                "phase": "run", "name": phase["name"],
                "exit_code": code, "duration_ms": 5,
            }))
        (paths.artifacts / "phases.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        (paths.artifacts / "collect.txt").write_text(
            f"{self.collected} tests collected in 0.01s\n", encoding="utf-8")
        if self.junit is not None:
            (paths.artifacts / "junit.xml").write_text(
                self.junit, encoding="utf-8")
        log_path.write_text("check log", encoding="utf-8")
        return jobs.RunnerResult(
            exit_code=self.exit_code, timed_out=self.timed_out,
            signal=jobs._signal_name(self.exit_code), backend="fake")


@pytest.fixture
def scope_env(tmp_path):
    db_path = str(tmp_path / "slice.db")
    workspace = config.TASK_WORKSPACES_DIR / "T1"
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_auth.py").write_text(
        "def test_login():\n    raise NotImplementedError()\n", encoding="utf-8")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','slice','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    yield conn, db_path, wf["id"]
    conn.close()


def _scope_step(conn, wid):
    steps = wf_service.get_workflow(conn, wid)["steps"]
    return next(s for s in steps if s["step_id"] == "P1.auth.scope")


def _run_checks(conn, db_path, runner):
    worker = SandboxJobWorker(db_path, runner=runner)
    while worker.run_once(conn):
        pass


def test_scope_submit_accepts_spec_and_runs_test_kind_check(scope_env):
    conn, db_path, wid = scope_env
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready",
        slice_spec=_slice_spec())

    step = _scope_step(conn, wid)
    assert step["slice_spec"]["seam"] == "auth service"
    assert step["slice_spec"]["acceptance"][0]["kind"] == "test"

    jobs_list = [j for j in sandbox.list_jobs(conn, task_id="T1", kind="check")]
    assert len(jobs_list) == 1
    job = jobs_list[0]
    assert job["spec"]["check"]["pytest"]["paths"] == ["tests/test_auth.py"]
    assert job["spec"]["check"]["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert job["status"] == "queued"

    _run_checks(conn, db_path, FakePytestRunner())
    view = sandbox.job_view(conn, job["id"])
    result = view["check_result"]
    assert result["classification"] == "valid_red"
    assert "NotImplementedError" in result["failure_types"]
    assert result["collected_baseline"] == 1
    assert result["collected_count"] == 1
    assert result["env_hash"].startswith("sha256:")
    assert view["evidence"]["check"]["classification"] == "valid_red"


def test_scope_submit_rejects_invalid_spec(scope_env):
    conn, _db_path, wid = scope_env
    bad = _slice_spec()
    bad["acceptance"][0]["kind"] = "integration"
    with pytest.raises(ConflictError):
        wf_service.submit_step(
            conn, wid, "P1.auth.scope", "lead", "bad",
            slice_spec=bad)
    step = _scope_step(conn, wid)
    assert step["status"] == "running"
    assert not step.get("slice_spec")


def test_unexpected_exception_is_harness_error(scope_env):
    conn, db_path, wid = scope_env
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready",
        slice_spec=_slice_spec())
    _run_checks(conn, db_path, FakePytestRunner(junit=JUNIT_VALUE_ERROR))
    job = sandbox.list_jobs(conn, task_id="T1", kind="check")[0]
    result = sandbox.job_view(conn, job["id"])["check_result"]
    assert result["classification"] == "harness_error"
    assert result["cause"] == "unexpected_exception"


def test_timeout_is_harness_error(scope_env):
    conn, db_path, wid = scope_env
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready",
        slice_spec=_slice_spec())
    _run_checks(conn, db_path, FakePytestRunner(
        exit_code=124, junit=None, timed_out=True))
    job = sandbox.list_jobs(conn, task_id="T1", kind="check")[0]
    result = sandbox.job_view(conn, job["id"])["check_result"]
    assert result["classification"] == "harness_error"
    assert result["cause"] == "timeout"
