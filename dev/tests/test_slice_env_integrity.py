"""环境与依赖完整性（v1.62 #7）。

红跑与绿跑共享基础环境哈希（镜像、runner 命令、环境变量）；依赖集单独记录。
测试依赖闭包变化升级人工；生产依赖变化时在 base 代码 + 新依赖集上反事实重跑。
依赖 delta 与反事实结论进入评审包；需要网络的安装走人工审批。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import env_integrity, reviews, sandbox
from anvil.services import workflow as wf_service
from anvil.services.sandbox_worker import SandboxJobWorker
from anvil.util import ConflictError, now_iso


def _plain_spec() -> dict:
    return jobs.parse_check_spec({
        "task_id": "T1",
        "check": {"id": "c1", "kind": "test",
                  "pytest": {"paths": ["tests/test_auth.py"]}},
    })


def _install_spec(requirements: list[str]) -> dict:
    return jobs.parse_check_spec({
        "task_id": "T1",
        "check": {"id": "c1", "kind": "test",
                  "pytest": {"paths": ["tests/test_auth.py"]},
                  "install": {"requirements": requirements}},
    })


def _dependency_hash(requirements: list[str]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(sorted(requirements), separators=(",", ":")).encode()).hexdigest()


def test_environment_hash_is_base_only_and_dependencies_are_separate():
    plain = _plain_spec()
    with_dep = _install_spec(["rich"])

    # 依赖集变化不改变基础环境哈希；它与镜像/runner 命令/环境变量分开记录。
    assert jobs.environment_hash(plain) == jobs.environment_hash(with_dep)
    assert jobs.environment_parts(with_dep)["base"]["image"] == config.SANDBOX_IMAGE
    assert "install" not in jobs.environment_parts(with_dep)["base"]

    assert jobs.dependency_digest(plain) == _dependency_hash([])
    assert jobs.dependency_digest(with_dep) == _dependency_hash(["rich"])
    assert jobs.dependency_digest(plain) != jobs.dependency_digest(with_dep)


def test_install_requirements_are_sorted_and_deduplicated():
    a = _install_spec(["rich", "requests"])
    b = _install_spec(["requests", "rich", "requests"])
    assert jobs.dependency_digest(a) == jobs.dependency_digest(b)
    assert a["check"]["install"]["requirements"] == ["requests", "rich"]


# ---- workflow helpers ------------------------------------------------------

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
PYPROJECT = (
    "[project]\nname = 'demo'\nversion = '0.1.0'\n"
    "dependencies = ['requests>=2']\n\n"
    "[dependency-groups]\ntest = ['pytest']\n"
)


def _plan():
    return {"rationale": "单切片", "slices": [{
        "id": "auth", "title": "认证",
        "scope_paths": ["src/auth/**"],
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

JUNIT_VALUE_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login">
    <failure message="ValueError: boom">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""

JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login"/>
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


def _env(tmp_path, *, pyproject=PYPROJECT):
    workspace = config.TASK_WORKSPACES_DIR / "T1"
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    (workspace / STUB_FILE).write_text(STUB_SOURCE, encoding="utf-8")
    if pyproject is not None:
        (workspace / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    db_path = str(tmp_path / "env.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','env','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", _plan())
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    return conn, db_path, wf["id"], workspace


def _step(conn, wid, step_id):
    return next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == step_id)


def _submit_scope(conn, wid):
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready",
        slice_spec=_slice_spec())


def _to_execute(conn, wid, runner_id="r3"):
    wf_service.advance(conn, wid, starter=lambda *a: {"id": runner_id})


def _run_checks(conn, db_path, runner):
    worker = SandboxJobWorker(db_path, runner=runner)
    while worker.run_once(conn):
        pass


# --- lock records dependency set separately --------------------------------

def test_lock_records_dependencies_separately_from_base_environment(tmp_path):
    conn, _db, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    lock = _step(conn, wid, "P1.auth.scope")["slice_lock"]
    assert lock["dependencies"]["production"] == ["requests>=2"]
    assert lock["dependencies"]["test"] == ["pytest"]
    assert "pyproject.toml" in lock["dependencies"]["files"]


def test_red_run_records_base_environment_hash_and_dependency_digest(tmp_path):
    conn, db_path, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _run_checks(conn, db_path, FakePytestRunner())
    checks = _step(conn, wid, "P1.auth.scope")["slice_lock"]["checks"]
    entry = next(c for c in checks if c["id"] == "P1.auth.scope.a1")
    assert entry["env_hash"].startswith("sha256:")
    assert entry["dependency_digest"].startswith("sha256:")


# --- test dependency closure change escalates, never auto-rejects ----------

def test_test_dependency_closure_change_escalates_without_rejecting(tmp_path):
    conn, _db, wid, ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    (ws / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n"
        "dependencies = ['requests>=2']\n\n"
        "[dependency-groups]\ntest = ['pytest', 'pytest-timeout']\n",
        encoding="utf-8")
    wf_service.submit_step(
        conn, wid, "P1.auth.impl", "auditor", "added test dependency",
        changed_paths=["pyproject.toml"])

    # 提交仍然通过；变化以评审信号升级人工，而不是自动拒绝。
    assert _step(conn, wid, "P1.auth.impl")["status"] == "approved"
    integrity = _step(conn, wid, "P1.auth.scope")["slice_lock"]["dependency_integrity"]
    assert integrity["test_closure_changed"] is True
    assert "dependency_test_closure_changed" in integrity["signals"]


# --- production dependency change triggers a counterfactual re-run --------

def _prod_change(ws, dependencies: list[str]) -> None:
    quoted = ", ".join(f"'{d}'" for d in dependencies)
    (ws / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n"
        f"dependencies = [{quoted}]\n\n"
        "[dependency-groups]\ntest = ['pytest']\n",
        encoding="utf-8")


def _prod_change_to_execute(tmp_path, dependencies):
    conn, db_path, wid, ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _run_checks(conn, db_path, FakePytestRunner())
    _to_execute(conn, wid)
    _prod_change(ws, dependencies)
    wf_service.submit_step(
        conn, wid, "P1.auth.impl", "auditor", "added production dependency",
        changed_paths=["pyproject.toml"])
    jobs_list = sandbox.list_jobs(conn, task_id="T1", kind="check")
    counterfactuals = [j for j in jobs_list if j["spec"].get("counterfactual")]
    return conn, db_path, wid, ws, counterfactuals


def test_production_change_creates_network_gated_counterfactual(tmp_path):
    conn, _db, _wid, ws, counterfactuals = _prod_change_to_execute(
        tmp_path, ["requests>=2", "rich"])
    assert len(counterfactuals) == 1
    job = counterfactuals[0]
    # 安装新生产依赖需要网络：角色发起时必须等待人工审批。
    assert job["status"] == "waiting_approval"
    assert job["spec"]["check"]["install"]["requirements"] == [
        "requests>=2", "rich"]
    snapshot = job["spec"]["base_snapshot"]
    assert str(ws / ".anvil" / "slice-base") in snapshot


def _approved_counterfactual(conn, counterfactual):
    sandbox.approve_job(conn, counterfactual["id"], actor_role="human")
    return counterfactual


def _counterfactual_outcome(conn, wid, db_path, runner):
    _run_checks(conn, db_path, runner)
    return _step(conn, wid, "P1.auth.scope")["slice_lock"]["dependency_integrity"]


def test_counterfactual_passing_on_base_invalidates_check(tmp_path):
    conn, db_path, wid, _ws, counterfactuals = _prod_change_to_execute(
        tmp_path, ["requests>=2", "rich"])
    _approved_counterfactual(conn, counterfactuals[0])
    integrity = _counterfactual_outcome(
        conn, wid, db_path, FakePytestRunner(exit_code=0, junit=JUNIT_PASS))
    entry = integrity["counterfactual"]["P1.auth.scope.a1"]
    assert entry["outcome"] == "check_invalid"
    assert "dependency_counterfactual_passed" in integrity["signals"]


def test_counterfactual_still_red_keeps_check(tmp_path):
    conn, db_path, wid, _ws, counterfactuals = _prod_change_to_execute(
        tmp_path, ["requests>=2", "rich"])
    _approved_counterfactual(conn, counterfactuals[0])
    integrity = _counterfactual_outcome(
        conn, wid, db_path, FakePytestRunner())
    entry = integrity["counterfactual"]["P1.auth.scope.a1"]
    assert entry["outcome"] == "still_red"
    assert "dependency_counterfactual_passed" not in integrity["signals"]
    assert "dependency_counterfactual_harness_error" not in integrity["signals"]


def test_counterfactual_harness_error_escalates(tmp_path):
    conn, db_path, wid, _ws, counterfactuals = _prod_change_to_execute(
        tmp_path, ["requests>=2", "rich"])
    _approved_counterfactual(conn, counterfactuals[0])
    integrity = _counterfactual_outcome(
        conn, wid, db_path, FakePytestRunner(junit=JUNIT_VALUE_ERROR))
    entry = integrity["counterfactual"]["P1.auth.scope.a1"]
    assert entry["outcome"] == "harness_error"
    assert "dependency_counterfactual_harness_error" in integrity["signals"]


# --- dependency delta and counterfactual outcome in the gate packet --------

def _advance(conn, wid, n):
    wf_service.advance(conn, wid, starter=lambda *a: {"id": f"r{n}"})


def _drive_to_decide(conn, wid):
    _advance(conn, wid, 10)
    wf_service.submit_step(conn, wid, "P1.auth.verify", "reviewer", "验证完成")
    _advance(conn, wid, 11)
    wf_service.submit_step(conn, wid, "P1.auth.fix", "developer", "修复完成")
    _advance(conn, wid, 12)
    wf_service.submit_step(conn, wid, "P1.auth.verify2", "reviewer", "复验完成")
    _advance(conn, wid, 13)
    return _step(conn, wid, "P1.auth.decide")


def _decide_packet(conn, wid):
    wf = wf_service.get_workflow(conn, wid)
    decide = next(s for s in wf["steps"] if s["step_id"] == "P1.auth.decide")
    return reviews.ensure_packet(conn, wf, decide, created_by="lead")


def test_dependency_delta_and_counterfactual_are_visible_in_packet(tmp_path):
    conn, db_path, wid, _ws, counterfactuals = _prod_change_to_execute(
        tmp_path, ["requests>=2", "rich"])
    _approved_counterfactual(conn, counterfactuals[0])
    _run_checks(conn, db_path, FakePytestRunner(exit_code=0, junit=JUNIT_PASS))
    _drive_to_decide(conn, wid)

    packet = _decide_packet(conn, wid)
    assert "dependency_delta" in packet["risks"]
    assert "dependency_counterfactual_passed" in packet["risks"]
    dependency_evidence = next(
        e for e in packet["evidence"]
        if e.get("ref") == "slice_lock.dependency_integrity")
    assert "rich" in dependency_evidence["label"]
    assert "check_invalid" in dependency_evidence["label"]


def test_pytest11_entry_point_is_surfaced_as_review_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        env_integrity, "pytest11_signals",
        lambda packages, entry_points=None: [
            {"package": "rich", "plugin": "rich.pytest"}])
    conn, _db, wid, _ws, _jobs = _prod_change_to_execute(
        tmp_path, ["requests>=2", "rich"])
    _drive_to_decide(conn, wid)
    packet = _decide_packet(conn, wid)
    assert "dependency_pytest11_plugin" in packet["risks"]
    dependency_evidence = next(
        e for e in packet["evidence"]
        if e.get("ref") == "slice_lock.dependency_integrity")
    assert "pytest11" in dependency_evidence["label"]


def test_base_environment_mismatch_blocks_green_run(tmp_path):
    conn, db_path, wid, _ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _run_checks(conn, db_path, FakePytestRunner())
    lock = _step(conn, wid, "P1.auth.scope")["slice_lock"]
    check_id = "P1.auth.scope.a1"

    same = jobs.parse_check_spec({
        "task_id": "T1",
        "check": {"id": check_id, "kind": "test",
                  "pytest": {"paths": [TEST_FILE], "args": ["-q"]}},
    })
    # 基础环境一致时可以绿跑；依赖集变化不进基础哈希，另行反事实重跑。
    env_integrity.guard_green_environment(lock, check_id, same)
    with_install = jobs.parse_check_spec({
        "task_id": "T1",
        "check": {"id": check_id, "kind": "test",
                  "pytest": {"paths": [TEST_FILE], "args": ["-q"]},
                  "install": {"requirements": ["rich"]}},
    })
    env_integrity.guard_green_environment(lock, check_id, with_install)

    mismatched = jobs.parse_check_spec({
        "task_id": "T1",
        "network": "bridge",
        "check": {"id": check_id, "kind": "test",
                  "pytest": {"paths": [TEST_FILE], "args": ["-q"]}},
    })
    with pytest.raises(env_integrity.EnvironmentMismatch):
        env_integrity.guard_green_environment(lock, check_id, mismatched)


def test_ai_decide_escalates_on_test_closure_change(tmp_path):
    conn, _db, wid, ws = _env(tmp_path)
    _submit_scope(conn, wid)
    _to_execute(conn, wid)
    (ws / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n"
        "dependencies = ['requests>=2']\n\n"
        "[dependency-groups]\ntest = ['pytest', 'pytest-timeout']\n",
        encoding="utf-8")
    wf_service.submit_step(
        conn, wid, "P1.auth.impl", "auditor", "added test dependency",
        changed_paths=["pyproject.toml"])
    _drive_to_decide(conn, wid)
    with pytest.raises(ConflictError) as excinfo:
        wf_service.decide_step(
            conn, wid, "P1.auth.decide", "lead", True, "通过")
    assert "escalation_required" in str(excinfo.value)
    assert "dependency_test_closure_changed" in str(excinfo.value)
