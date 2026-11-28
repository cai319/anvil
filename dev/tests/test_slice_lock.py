"""切片锁定集合：路径、配置、stub 签名、不相交（v1.62 #6）。

所有场景都通过 workflow 服务层驱动（create → plan → scope 提交 → execute 提交），
断言持久化状态与拒绝行为，不触碰实现细节。
"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import sandbox
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

TEST_FILE = "tests/test_auth.py"
STUB_FILE = "src/auth/service.py"
TEST_SOURCE = "def test_login():\n    raise NotImplementedError()\n"
STUB_SOURCE = "def login(user: str) -> bool:\n    raise NotImplementedError()\n"


def _plan(scope_paths=None):
    return {"rationale": "单切片", "slices": [{
        "id": "auth", "title": "认证",
        "scope_paths": scope_paths if scope_paths is not None else ["src/auth/**"],
        "execute_steps": [{"id": "impl", "title": "实现"}],
    }]}


def _slice_spec() -> dict:
    return {
        "acceptance": [{
            "id": "a1",
            "kind": "test",
            "criterion": "stub raises NotImplementedError until implemented",
            "verification": {"pytest": {"paths": [TEST_FILE], "args": ["-q"]}},
        }],
        "seam": "auth service",
        "out_of_scope": ["web UI"],
        "contract": "def login(user) -> bool",
        "deliverables": [STUB_FILE],
        "stubs": [{"path": STUB_FILE,
                   "signatures": ["def login(user: str) -> bool"]}],
    }


JUNIT_NOT_IMPLEMENTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login">
    <failure message="NotImplementedError: stub">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""


class FakePytestRunner:
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


def _env(tmp_path, *, plan_scope_paths=None, extra_files=None):
    """建任务工作区 + workflow，推进到 scope 步骤 running。"""
    workspace = config.TASK_WORKSPACES_DIR / "T1"
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    (workspace / STUB_FILE).write_text(STUB_SOURCE, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    db_path = str(tmp_path / "slice.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','slice','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(
        conn, wf["id"], "P1", "lead", _plan(plan_scope_paths))
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    return conn, db_path, wf["id"], workspace


def _step(conn, wid, step_id):
    return next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == step_id)


def _submit_scope(conn, wid, spec=None):
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready",
        slice_spec=spec or _slice_spec())


def _to_execute(conn, wid, runner_id="r3"):
    wf_service.advance(conn, wid, starter=lambda *a: {"id": runner_id})


# --- lock record contents & visibility -------------------------------------

def test_scope_lock_persists_and_is_visible(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)

    lock = _step(conn, wid, "P1.auth.scope")["slice_lock"]
    assert lock is not None
    assert lock["spec_hash"].startswith("sha256:")
    assert lock["test_files"] == [TEST_FILE]
    assert lock["paths"] and TEST_FILE in lock["paths"]
    assert lock["stubs"][0]["path"] == STUB_FILE
    assert lock["stubs"][0]["ast_sha256"].startswith("sha256:")
    assert lock["check_dirs"] == ["tests"]
    assert "locked_at" in lock
    assert "runner_config" in lock


def test_lock_records_runner_config_section_and_file(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path, extra_files={
        "pytest.ini": "[pytest]\naddopts = -q\n",
        "pyproject.toml": (
            "[tool.pytest.ini_options]\naddopts = '-q'\n\n"
            "[project]\nname = 'demo'\nversion = '0.1.0'\n"),
    })
    _submit_scope(conn, wid)
    lock = _step(conn, wid, "P1.auth.scope")["slice_lock"]
    locked_files = {f["path"] for f in lock["runner_config"]["files"]}
    locked_sections = {(s["path"], s["section"])
                       for s in lock["runner_config"]["sections"]}
    assert "pytest.ini" in locked_files
    assert ("pyproject.toml", "tool.pytest.ini_options") in locked_sections


# --- scope_paths disjointness (refuse at lock) -----------------------------

def test_scope_lock_refuses_execute_scope_paths_intersection(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path, plan_scope_paths=["tests/**"])
    with pytest.raises(ConflictError) as excinfo:
        _submit_scope(conn, wid)
    assert "lock_scope_conflict" in str(excinfo.value)
    step = _step(conn, wid, "P1.auth.scope")
    assert step["status"] == "running"
    assert not step.get("slice_lock")
    assert not step.get("slice_spec")


# --- reject test file changes ----------------------------------------------

def test_execute_rejects_locked_test_file_change(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.impl", "auditor", "done",
            changed_paths=[TEST_FILE])
    assert "lock_violation" in str(excinfo.value)
    assert _step(conn, wid, "P1.auth.impl")["status"] == "running"


# --- reject new files in check directories ---------------------------------

def test_execute_rejects_new_file_in_check_directory(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.impl", "auditor", "done",
            changed_paths=["tests/test_extra.py"])
    assert "lock_violation" in str(excinfo.value)


# --- reject runner config changes ------------------------------------------

def test_execute_rejects_new_conftest_in_check_directory(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.impl", "auditor", "done",
            changed_paths=["tests/conftest.py"])
    assert "lock_violation" in str(excinfo.value)


def test_execute_rejects_standalone_runner_config_change(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path, extra_files={
        "pytest.ini": "[pytest]\naddopts = -q\n"})
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.impl", "auditor", "done",
            changed_paths=["pytest.ini"])
    assert "lock_violation" in str(excinfo.value)


def test_execute_rejects_changed_runner_config_section(tmp_path):
    conn, _db, wid, ws = _env(tmp_path, extra_files={
        "pyproject.toml": (
            "[tool.pytest.ini_options]\naddopts = '-q'\n\n"
            "[project]\nname = 'demo'\nversion = '0.1.0'\n")})
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    # 改生产依赖段：允许（runner 段未变）。
    (ws / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n\n"
        "[project]\nname = 'demo'\nversion = '0.1.0'\ndependencies = ['rich']\n",
        encoding="utf-8")
    wf_service.submit_step(
        conn, wid, "P1.auth.impl", "auditor", "prod dep ok",
        changed_paths=["pyproject.toml"])
    assert _step(conn, wid, "P1.auth.impl")["status"] == "approved"


def test_execute_rejects_changed_pytest_section(tmp_path):
    conn, _db, wid, ws = _env(tmp_path, extra_files={
        "pyproject.toml": (
            "[tool.pytest.ini_options]\naddopts = '-q'\n\n"
            "[project]\nname = 'demo'\nversion = '0.1.0'\n")})
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    (ws / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q -p no:cacheprovider'\n\n"
        "[project]\nname = 'demo'\nversion = '0.1.0'\n",
        encoding="utf-8")
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.impl", "auditor", "weakened",
            changed_paths=["pyproject.toml"])
    assert "lock_violation" in str(excinfo.value)


# --- reject stub signature changes, allow body-only edits ------------------

def test_execute_rejects_stub_signature_change(tmp_path):
    conn, _db, wid, ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    (ws / STUB_FILE).write_text(
        "def login(user: str, remember: bool) -> bool:\n"
        "    raise NotImplementedError()\n", encoding="utf-8")
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.impl", "auditor", "signature changed",
            changed_paths=[STUB_FILE])
    assert "lock_violation" in str(excinfo.value)


def test_execute_allows_stub_body_implementation(tmp_path):
    conn, _db, wid, ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    (ws / STUB_FILE).write_text(
        "def login(user: str) -> bool:\n    return bool(user)\n",
        encoding="utf-8")
    wf_service.submit_step(
        conn, wid, "P1.auth.impl", "auditor", "implemented",
        changed_paths=[STUB_FILE])
    assert _step(conn, wid, "P1.auth.impl")["status"] == "approved"


# --- test-count baseline is captured by the engine -------------------------

def test_lock_records_test_count_baseline_after_red_run(tmp_path):
    conn, db_path, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    worker = SandboxJobWorker(db_path, runner=FakePytestRunner())
    while worker.run_once(conn):
        pass
    lock = _step(conn, wid, "P1.auth.scope")["slice_lock"]
    assert lock["test_count_baseline"] == 1
    assert lock["checks"][0]["id"] == "P1.auth.scope.a1"
