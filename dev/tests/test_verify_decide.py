"""verify 绿跑与 decide 判定链（v1.62 #9）。

- verify 阶段由引擎重跑锁定检查；绿跑前校验基础环境与红跑一致。
- verify 只整理证据，不提交通过结论；decide 逐项记录 met/unmet/n.a. 与证据引用。
- 存在 unknown 或 unmet 时 decide 不能通过；manual 项由 decide 判定。
- manual 占比在 scope 锁定重算，超过 30% 时该片 decide authority 切为 human。
"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import env_integrity, reviews, sandbox
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
TEST_FILE = "tests/test_auth.py"
STUB_FILE = "src/auth/service.py"
TEST_SOURCE = "def test_login():\n    raise NotImplementedError()\n"
STUB_SOURCE = "def login(user: str) -> bool:\n    raise NotImplementedError()\n"
PYPROJECT = (
    "[project]\nname = 'demo'\nversion = '0.1.0'\n"
    "dependencies = ['requests>=2']\n\n"
    "[dependency-groups]\ntest = ['pytest']\n"
)

JUNIT_NOT_IMPLEMENTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login">
    <failure message="NotImplementedError: stub">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""
JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">
  <testcase classname="tests.test_auth" name="test_login"/>
</testsuite></testsuites>
"""


def _slice_spec(*, manual: int = 0) -> dict:
    acceptance = [{
        "id": "a1",
        "kind": "test",
        "criterion": "login returns True for valid user",
        "verification": {"pytest": {"paths": [TEST_FILE], "args": ["-q"]}},
    }]
    for index in range(manual):
        acceptance.append({
            "id": f"m{index + 1}",
            "kind": "manual",
            "criterion": f"manual check {index + 1}",
            "verification": {"judge": "decide", "method": "read the diff"},
        })
    return {
        "acceptance": acceptance,
        "seam": "auth service",
        "out_of_scope": ["web UI"],
        "contract": "def login(user) -> bool",
        "deliverables": [STUB_FILE],
        "stubs": [{"path": STUB_FILE,
                   "signatures": ["def login(user: str) -> bool"]}],
    }


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


@pytest.fixture
def slice_env(tmp_path):
    workspace = config.TASK_WORKSPACES_DIR / "T9"
    import shutil
    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    (workspace / STUB_FILE).write_text(STUB_SOURCE, encoding="utf-8")
    (workspace / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    db_path = str(tmp_path / "vd.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T9','verify decide','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T9", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    yield conn, db_path, wf["id"], workspace
    conn.close()
    shutil.rmtree(workspace, ignore_errors=True)


def _run_checks(conn, db_path, runner):
    worker = SandboxJobWorker(db_path, runner=runner)
    while worker.run_once(conn):
        pass


def _scope(conn, wid, spec):
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready", slice_spec=spec)


def _submit(conn, wid, step_id, role):
    wf_service.submit_step(conn, wid, step_id, role, "done")


def _advance(conn, wid, n):
    wf_service.advance(conn, wid, starter=lambda *a: {"id": f"r{n}"})


def _to_verify2(conn, wid, spec):
    """scope → red run → execute → verify → fix → verify2 就绪。"""
    _scope(conn, wid, spec)
    _advance(conn, wid, 3)
    _submit(conn, wid, "P1.auth.impl", "auditor")
    _advance(conn, wid, 4)
    _submit(conn, wid, "P1.auth.verify", "reviewer")
    _advance(conn, wid, 5)
    _submit(conn, wid, "P1.auth.fix", "developer")
    _advance(conn, wid, 6)


def _submit_final_verify(conn, wid):
    _submit(conn, wid, "P1.auth.verify2", "reviewer")


def _green_jobs(conn):
    return [j for j in sandbox.list_jobs(conn, task_id="T9", kind="check")
            if (j.get("spec") or {}).get("green_ref")]


def _to_decide(conn, wid):
    _advance(conn, wid, 7)
    return next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == "P1.auth.decide")


# --- verify green run -------------------------------------------------------

def test_final_verify_submit_re_runs_locked_checks_after_env_guard(
        slice_env, monkeypatch):
    conn, db_path, wid, _ws = slice_env
    _to_verify2(conn, wid, _slice_spec())
    _run_checks(conn, db_path, FakePytestRunner())

    calls: list[tuple[str, str]] = []
    original = env_integrity.guard_green_environment

    def spy(lock, check_id, green_spec):
        calls.append((check_id, jobs.environment_hash(green_spec)))
        return original(lock, check_id, green_spec)

    monkeypatch.setattr(env_integrity, "guard_green_environment", spy)
    _submit_final_verify(conn, wid)

    green = _green_jobs(conn)
    assert len(green) == 1
    spec = green[0]["spec"]
    assert spec["check"]["id"] == "P1.auth.scope.a1"
    assert spec["green_ref"]["slice_key"] == "auth"
    assert spec["green_ref"]["workflow_id"] == wid
    # 绿跑前先调用握手校验基础环境；check id 与锁定集合一致。
    assert [c[0] for c in calls] == ["P1.auth.scope.a1"]
    assert calls[0][1] == jobs.environment_hash(green[0]["spec"])


def test_green_run_uses_same_base_environment_as_red(slice_env):
    conn, db_path, wid, _ws = slice_env
    _to_verify2(conn, wid, _slice_spec())
    _run_checks(conn, db_path, FakePytestRunner())
    lock = next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == "P1.auth.scope")["slice_lock"]
    red_env = next(c["env_hash"] for c in lock["checks"]
                   if c["id"] == "P1.auth.scope.a1")
    _submit_final_verify(conn, wid)
    green = _green_jobs(conn)[0]
    assert jobs.environment_hash(green["spec"]) == red_env

    _run_checks(conn, db_path, FakePytestRunner(exit_code=0, junit=JUNIT_PASS))
    result = sandbox.job_view(conn, green["id"])["check_result"]
    assert result["classification"] == "passed"
    assert result["env_hash"] == red_env


def test_verify_cannot_submit_pass_verdict_or_slice_spec(slice_env):
    conn, _db_path, wid, _ws = slice_env
    _scope(conn, wid, _slice_spec())
    _advance(conn, wid, 3)
    _submit(conn, wid, "P1.auth.impl", "auditor")
    _advance(conn, wid, 4)
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.verify", "reviewer", "全部通过",
            slice_spec=_slice_spec())
    assert "slice_spec" in str(excinfo.value)
    # verify 提交不会产生任何决策记录或逐项 verdict。
    assert reviews.decisions_for_step(conn, wid, "P1.auth.verify") == []


def test_environment_mismatch_skips_green_run_leaving_unknown(slice_env, monkeypatch):
    conn, _db_path, wid, _ws = slice_env
    _to_verify2(conn, wid, _slice_spec())

    def mismatch(lock, check_id, green_spec):
        raise env_integrity.EnvironmentMismatch("boom")

    monkeypatch.setattr(env_integrity, "guard_green_environment", mismatch)
    _submit_final_verify(conn, wid)
    assert _green_jobs(conn) == []


# --- decide verdicts --------------------------------------------------------

def _green_and_decide(conn, db_path, wid, *, manual=0, green_runner=None):
    _to_verify2(conn, wid, _slice_spec(manual=manual))
    _run_checks(conn, db_path, FakePytestRunner())  # red run
    _submit_final_verify(conn, wid)
    _run_checks(conn, db_path, green_runner
                or FakePytestRunner(exit_code=0, junit=JUNIT_PASS))
    return _to_decide(conn, wid)


def _decide(conn, wid, *, approve=True, role="lead", verdicts=None):
    return wf_service.decide_step(
        conn, wid, "P1.auth.decide", role, approve, "decision",
        verdicts=verdicts)


def test_green_pass_yields_met_verdict_and_decide_approves(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(conn, db_path, wid)
    wf = _decide(conn, wid)
    step = next(s for s in wf["steps"] if s["step_id"] == "P1.auth.decide")
    assert step["status"] == "approved"
    decision = reviews.decisions_for_step(conn, wid, "P1.auth.decide")[-1]
    assert decision["verdicts"] == [{
        "id": "a1", "kind": "test", "criterion": "login returns True for valid user",
        "status": "met", "source": "engine",
        "evidence_refs": ["check:P1.auth.scope.a1"],
    }]


def test_failed_green_check_yields_unmet_and_blocks_approval(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(
        conn, db_path, wid,
        green_runner=FakePytestRunner(exit_code=1, junit=JUNIT_NOT_IMPLEMENTED))
    with pytest.raises(ConflictError) as excinfo:
        _decide(conn, wid)
    assert "acceptance_unmet" in str(excinfo.value)


def test_missing_green_run_is_unknown_and_blocks_approval(slice_env):
    conn, _db_path, wid, _ws = slice_env
    _to_verify2(conn, wid, _slice_spec())
    _submit_final_verify(conn, wid)
    _to_decide(conn, wid)  # 绿跑作业尚未执行 → unknown
    with pytest.raises(ConflictError) as excinfo:
        _decide(conn, wid)
    assert "verdict_unknown" in str(excinfo.value)


def test_harness_error_green_run_is_unknown_and_blocks_approval(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(
        conn, db_path, wid,
        green_runner=FakePytestRunner(exit_code=1, junit=None))
    with pytest.raises(ConflictError) as excinfo:
        _decide(conn, wid)
    assert "verdict_unknown" in str(excinfo.value)


def test_manual_items_are_judged_by_decide(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(conn, db_path, wid, manual=1)
    # 未提交 manual 判定时是 unknown，不能通过（该片 authority 已切 human）。
    with pytest.raises(ConflictError) as excinfo:
        _decide(conn, wid, role="human")
    assert "verdict_unknown" in str(excinfo.value)

    # 1/2 manual > 30%，该片 decide authority 已切为 human。
    wf = _decide(conn, wid, role="human", verdicts=[
        {"id": "m1", "status": "met", "evidence_refs": ["src/auth/service.py"]}])
    assert next(s for s in wf["steps"]
                if s["step_id"] == "P1.auth.decide")["status"] == "approved"
    decision = reviews.decisions_for_step(conn, wid, "P1.auth.decide")[-1]
    manual = next(v for v in decision["verdicts"] if v["id"] == "m1")
    assert manual["status"] == "met"
    assert manual["source"] == "manual"
    assert manual["evidence_refs"] == ["src/auth/service.py"]


def test_manual_na_verdict_allows_approval(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(conn, db_path, wid, manual=1)
    wf = _decide(conn, wid, role="human", verdicts=[
        {"id": "m1", "status": "n.a."}])
    assert next(s for s in wf["steps"]
                if s["step_id"] == "P1.auth.decide")["status"] == "approved"
    decision = reviews.decisions_for_step(conn, wid, "P1.auth.decide")[-1]
    assert next(v for v in decision["verdicts"]
                if v["id"] == "m1")["status"] == "n.a."


def test_decide_cannot_override_engine_verdict(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(
        conn, db_path, wid,
        green_runner=FakePytestRunner(exit_code=1, junit=JUNIT_NOT_IMPLEMENTED))
    with pytest.raises(ConflictError) as excinfo:
        _decide(conn, wid, verdicts=[{"id": "a1", "status": "met"}])
    assert "engine" in str(excinfo.value)


def test_manual_ratio_over_30_percent_switches_decide_authority_to_human(slice_env):
    conn, _db_path, wid, _ws = slice_env
    _scope(conn, wid, _slice_spec(manual=2))  # 2 manual / 3 total > 30%
    decide = next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                  if s["step_id"] == "P1.auth.decide")
    assert decide["decision_authority"] == "human"
    scope = next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                 if s["step_id"] == "P1.auth.scope")
    manual = scope["slice_lock"]["manual"]
    assert manual["manual"] == 2 and manual["total"] == 3
    assert manual["authority"] == "human"
    assert manual["ratio"] > 0.30


def test_manual_ratio_within_limit_keeps_ai_authority(slice_env):
    conn, _db_path, wid, _ws = slice_env
    spec = _slice_spec()
    for index in range(3):
        spec["acceptance"].append({
            "id": f"a{index + 2}", "kind": "test",
            "criterion": f"extra test {index + 2}",
            "verification": {"pytest": {"paths": [TEST_FILE], "args": ["-q"]}},
        })
    spec["acceptance"].append({
        "id": "m1", "kind": "manual", "criterion": "manual",
        "verification": {"judge": "decide", "method": "read"}})
    _scope(conn, wid, spec)  # 1 manual / 5 total = 20%
    decide = next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                  if s["step_id"] == "P1.auth.decide")
    assert decide["decision_authority"] != "human"


def test_packet_shows_verdicts_evidence_and_manual_ratio(slice_env):
    conn, db_path, wid, _ws = slice_env
    _green_and_decide(conn, db_path, wid, manual=1)
    wf = wf_service.get_workflow(conn, wid)
    decide = next(s for s in wf["steps"] if s["step_id"] == "P1.auth.decide")
    packet = reviews.ensure_packet(conn, wf, decide, created_by="lead")
    acceptance = {item["criterion"]: item for item in packet["acceptance"]}
    assert acceptance["login returns True for valid user"]["status"] == "met"
    assert acceptance["login returns True for valid user"]["source"] == "engine"
    assert acceptance["login returns True for valid user"]["evidence_refs"] == [
        "check:P1.auth.scope.a1"]
    assert acceptance["manual check 1"]["status"] == "unknown"
    assert acceptance["manual check 1"]["source"] == "manual"
    check_evidence = next(e for e in packet["evidence"]
                          if e["id"] == "check:P1.auth.scope.a1")
    assert check_evidence["ref"].startswith("chk_")
    assert any("manual" in e["label"] for e in packet["evidence"]
               if e["id"] == "engine:manual_ratio")
