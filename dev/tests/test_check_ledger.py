"""检查台账与指标报告（v1.62 #12）。

- 每次引擎检查运行与 decide verdict 进入结构化台账，字段足以复算指标。
- 报告按 GLOSSARY 定义输出：N_total、N_qualified、有效红占比、harness 错误红占比、
  可机械化占比（分 kind）、假通过输入、人工返工、human authority 片占比、
  replan 与弱化计数，绝对数与比例同时输出。
- 可从 fixture workflow 生成完整报告；试点执行本身不在本票范围。
"""
from __future__ import annotations

import json
import shutil

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import check_ledger, ledger, reviews, tasks as tasks_service
from anvil.services import workflow as wf_service
from anvil.services.sandbox_worker import SandboxJobWorker
from anvil.util import now_iso

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
REPORT_FILE = "reports/out.md"
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


def _slice_spec(*, manual: int = 2) -> dict:
    """test(合法红) + command(首跑 harness) + file_assert(平凡红) + manual。

    5 条验收项：可机械化 3 条；manual 2/5 > 30%，该片 decide authority 切 human。
    """
    acceptance = [
        {"id": "a1", "kind": "test",
         "criterion": "login returns True for valid user",
         "verification": {"pytest": {"paths": [TEST_FILE], "args": ["-q"]}}},
        {"id": "c1", "kind": "command", "criterion": "cli reports failure",
         "verification": {"cmd": "python -m app --check", "exit_code": 1}},
        {"id": "f1", "kind": "file_assert", "criterion": "report produced",
         "verification": {"path": REPORT_FILE, "contains": "PASS"}},
    ]
    for index in range(manual):
        acceptance.append({
            "id": f"m{index + 1}", "kind": "manual",
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


class FakeRunner:
    """红跑：test=合法红、command=harness、file_assert 由引擎原生判定。

    绿跑：test/command 均通过；file_assert 由工作区实际报告文件判定。
    """

    name = "fake"

    def __init__(self, *, red: bool):
        self.red = red

    def available(self):
        return True, ""

    def run(self, *, spec, paths, job_id, log_path):
        check = spec["check"]
        kind = check["kind"]
        if kind == "test":
            exit_code = 1 if self.red else 0
            (paths.artifacts / "collect.txt").write_text(
                "1 tests collected in 0.01s\n", encoding="utf-8")
            (paths.artifacts / "junit.xml").write_text(
                JUNIT_NOT_IMPLEMENTED if self.red else JUNIT_PASS,
                encoding="utf-8")
        else:  # command
            # 红跑退出码 2 不是声明的断言失败退出码 1 → harness 错误。
            exit_code = 2 if self.red else 0
        lines = []
        phases = spec["phases"]["run"]
        for index, phase in enumerate(phases):
            last = index == len(phases) - 1
            lines.append(json.dumps({
                "phase": "run", "name": phase["name"],
                "exit_code": exit_code if last else 0, "duration_ms": 5,
            }))
        (paths.artifacts / "phases.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        log_path.write_text("check log", encoding="utf-8")
        return jobs.RunnerResult(
            exit_code=exit_code, timed_out=False,
            signal=jobs._signal_name(exit_code), backend="fake")


def _make_workspace(workspace):
    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    (workspace / STUB_FILE).write_text(STUB_SOURCE, encoding="utf-8")
    (workspace / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")


def _fixture(tmp_path):
    workspace = config.TASK_WORKSPACES_DIR / "T12"
    _make_workspace(workspace)
    db_path = str(tmp_path / "ledger.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T12','ledger','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T12", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    return conn, db_path, wf["id"], workspace


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


def _to_decide(conn, db_path, wid, workspace, spec=None):
    """scope → 红跑 → execute/verify/fix → verify2 → 绿跑 → decide 就绪。"""
    _scope(conn, wid, spec or _slice_spec())
    _advance(conn, wid, 3)
    _run_checks(conn, db_path, FakeRunner(red=True))
    _submit(conn, wid, "P1.auth.impl", "auditor")
    _advance(conn, wid, 4)
    _submit(conn, wid, "P1.auth.verify", "reviewer")
    _advance(conn, wid, 5)
    _submit(conn, wid, "P1.auth.fix", "developer")
    _advance(conn, wid, 6)
    report = workspace / REPORT_FILE
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("status: PASS\n", encoding="utf-8")
    _submit(conn, wid, "P1.auth.verify2", "reviewer")
    _run_checks(conn, db_path, FakeRunner(red=False))
    _advance(conn, wid, 7)
    return next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == "P1.auth.decide")


# --- ledger recording -------------------------------------------------------

def test_every_engine_check_run_is_recorded_with_recomputable_fields(tmp_path):
    conn, db_path, wid, workspace = _fixture(tmp_path)
    try:
        _scope(conn, wid, _slice_spec())
        _run_checks(conn, db_path, FakeRunner(red=True))

        rows = check_ledger.rows(conn, workflow_id=wid, record_type="run")
        assert len(rows) == 3  # test + command + file_assert（manual 不跑引擎）
        by_check = {row["check_id"]: row for row in rows}
        test_row = by_check["P1.auth.scope.a1"]
        assert test_row["kind"] == "test"
        assert test_row["source"] == "red"
        assert test_row["classification"] == "valid_red"
        assert test_row["exit_code"] == 1
        assert test_row["collected_count"] == 1
        assert test_row["collected_baseline"] == 1
        assert test_row["junit_summary"]["tests"] == 1
        assert test_row["env_hash"].startswith("sha256:")
        assert test_row["command"]
        assert test_row["run_id"].startswith("chk_")
        command_row = by_check["P1.auth.scope.c1"]
        assert command_row["classification"] == "harness_error"
        assert command_row["cause"] == "unexpected_exit_code"
        assert command_row["red_exit_code"] == 1
        file_row = by_check["P1.auth.scope.f1"]
        assert file_row["classification"] == "trivial_red"
        assert file_row["cause"] == "file_missing"
    finally:
        conn.close()
        shutil.rmtree(workspace, ignore_errors=True)


def test_failed_check_job_is_recorded_as_harness_error(tmp_path):
    conn, db_path, wid, workspace = _fixture(tmp_path)
    try:
        _scope(conn, wid, _slice_spec())

        class ExplodingRunner(FakeRunner):
            def run(self, *, spec, paths, job_id, log_path):
                raise RuntimeError("sandbox boom")

        _run_checks(conn, db_path, ExplodingRunner(red=True))
        rows = check_ledger.rows(conn, workflow_id=wid, record_type="run")
        # test / command 崩溃 → harness 错误；file_assert 由引擎原生判定，不受影响。
        assert len(rows) == 3
        harness = [row for row in rows
                   if row["classification"] == "harness_error"]
        assert {row["kind"] for row in harness} == {"test", "command"}
        assert all(row["cause"].startswith("sandbox_error") for row in harness)
        assert next(row for row in rows
                    if row["kind"] == "file_assert")["classification"] == "trivial_red"
    finally:
        conn.close()
        shutil.rmtree(workspace, ignore_errors=True)


def test_decide_verdicts_are_recorded_in_ledger(tmp_path):
    conn, db_path, wid, workspace = _fixture(tmp_path)
    try:
        _to_decide(conn, db_path, wid, workspace)
        wf_service.decide_step(
            conn, wid, "P1.auth.decide", "human", True, "decision",
            verdicts=[{"id": "m1", "status": "met", "evidence_refs": [STUB_FILE]},
                      {"id": "m2", "status": "n.a."}])

        verdicts = check_ledger.rows(conn, workflow_id=wid, record_type="verdict")
        by_item = {row["check_id"]: row for row in verdicts}
        assert by_item["a1"]["verdict_status"] == "met"
        assert by_item["a1"]["source"] == "engine"
        assert by_item["a1"]["evidence_refs"] == ["check:P1.auth.scope.a1"]
        assert by_item["m1"]["verdict_status"] == "met"
        assert by_item["m1"]["source"] == "manual"
        assert by_item["m2"]["verdict_status"] == "n.a."
        assert all(row["slice_key"] == "auth" for row in verdicts)
        assert all(row["decision_id"] for row in verdicts)
    finally:
        conn.close()
        shutil.rmtree(workspace, ignore_errors=True)


# --- report -----------------------------------------------------------------

def test_report_from_fixture_workflow_matches_glossary_metrics(tmp_path):
    conn, db_path, wid, workspace = _fixture(tmp_path)
    try:
        _to_decide(conn, db_path, wid, workspace)
        wf_service.decide_step(
            conn, wid, "P1.auth.decide", "human", True, "decision",
            verdicts=[{"id": "m1", "status": "met"}, {"id": "m2", "status": "met"}])

        report = check_ledger.report(conn, wid)

        assert report["workflow_id"] == wid
        assert report["n_total"] == 5
        assert report["n_qualified"] == 1  # 只有 test 在 base 上合法红

        assert report["valid_red"]["count"] == 1
        assert report["valid_red"]["ratio"] == 1.0
        assert report["harness_error_red"]["count"] == 1
        assert report["harness_error_red"]["ratio"] == 0.2

        mechanizable = report["mechanizable"]
        assert mechanizable["test"]["count"] == 1
        assert mechanizable["command"]["count"] == 1
        assert mechanizable["file_assert"]["count"] == 1
        assert mechanizable["manual"]["count"] == 2
        assert mechanizable["count"] == 3
        assert mechanizable["ratio"] == 0.6

        # 假通过：decide 放行 1 片；尚无人工审计结论，只输出输入。
        false_pass = report["false_pass"]
        assert false_pass["decide_passed_slices"] == 1
        assert false_pass["audited_defect_slices"] == 0
        assert false_pass["audited"] is False
        assert false_pass["rate"] is None

        assert report["human_rework"]["count"] == 0
        assert report["human_authority_slices"]["human"] == 1
        assert report["human_authority_slices"]["total"] == 1
        assert report["human_authority_slices"]["ratio"] == 1.0
        assert report["replan"]["count"] == 0
        assert report["replan"]["weakening"] == 0

        # 绝对数与比例同时输出，且每个检查的状态可追溯到台账（红跑 + 绿跑）。
        assert len(report["checks"]) == 6
        assert {row["source"] for row in report["checks"]} == {"red", "green"}
        assert len(report["slices"]) == 1
        assert report["slices"][0]["slice_key"] == "auth"
    finally:
        conn.close()
        shutil.rmtree(workspace, ignore_errors=True)


def test_report_counts_human_rework_and_replan_from_generic_records(tmp_path):
    conn = connect(str(tmp_path / "r.db"))
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T1','t','in_progress',?,?)", (now, now))
        conn.commit()
        wf = wf_service.create_workflow(conn, "T1", {"steps": [
            {"id": "S1", "title": "实现", "role": "dev"},
            {"id": "D1", "title": "验收", "role": "lead", "kind": "decision",
             "depends_on": ["S1"], "on_reject": ["S1"]},
        ]})
        wid = wf["id"]
        wf_service.advance(conn, wid, starter=lambda *a: {"id": "r1"})
        wf_service.submit_step(conn, wid, "S1", "dev", "done")
        wf_service.advance(conn, wid, starter=lambda *a: {"id": "r2"})
        # 人工驳回 = human rework（ai 权威的人工决策需 override_reason）。
        wf_service.decide_step(
            conn, wid, "D1", "human", False, "返工",
            override_reason="人工要求返工")

        # 人工重试：把步骤置为 failed 后 retry。
        conn.execute(
            "UPDATE workflow_steps SET status='failed', failure_reason='x' "
            "WHERE workflow_id=? AND step_id='S1'", (wid,))
        conn.commit()
        wf_service.retry_step(conn, wid, "S1")

        # 人工重开任务 = human rework。
        conn.execute("UPDATE tasks SET status='closed' WHERE id='T1'")
        conn.commit()
        tasks_service.reopen(conn, "T1", "human")

        # replan / 弱化：从通用审计/事件记录读取（#10 后续写入同形态即可）。
        ledger.emit(conn, "workflow.replan", task_id="T1", workflow_id=wid,
                    payload={"slice_key": "auth", "weakening": True,
                             "reason": "loosen", "old_spec_hash": "a",
                             "new_spec_hash": "b"})
        ledger.emit(conn, "workflow.replan", task_id="T1", workflow_id=wid,
                    payload={"slice_key": "auth", "weakening": False,
                             "reason": "fix typo"})
        conn.commit()

        report = check_ledger.report(conn, wid)
        assert report["human_rework"]["human_rejects"] == 1
        assert report["human_rework"]["retries"] == 1
        assert report["human_rework"]["reopens"] == 1
        assert report["human_rework"]["count"] == 3
        assert report["replan"]["count"] == 2
        assert report["replan"]["weakening"] == 1
        assert report["replan"]["weakening_ratio"] == 0.5
    finally:
        conn.close()


def test_report_without_slices_reports_zero_counts(tmp_path):
    conn = connect(str(tmp_path / "z.db"))
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T0','zero','todo',?,?)", (now, now))
        conn.commit()
        wf = wf_service.create_workflow(
            conn, "T0", {"steps": [{"id": "S1", "title": "t", "role": "dev"}]})
        report = check_ledger.report(conn, wf["id"])
        assert report["n_total"] == 0
        assert report["n_qualified"] == 0
        assert report["valid_red"]["ratio"] == 0.0
        assert report["harness_error_red"]["ratio"] == 0.0
        assert report["mechanizable"]["ratio"] == 0.0
        assert report["slices"] == []
    finally:
        conn.close()


def test_pilot_audit_feeds_false_pass_inputs(tmp_path):
    conn, db_path, wid, workspace = _fixture(tmp_path)
    try:
        _to_decide(conn, db_path, wid, workspace)
        wf_service.decide_step(
            conn, wid, "P1.auth.decide", "human", True, "decision",
            verdicts=[{"id": "m1", "status": "met"}, {"id": "m2", "status": "met"}])
        check_ledger.record_pilot_audit(
            conn, workflow_id=wid, task_id="T12", slice_key="auth",
            defect=True, scope_paths=["src/auth/**"], audited_by="human",
            within_7d=True)
        report = check_ledger.report(conn, wid)
        false_pass = report["false_pass"]
        assert false_pass["audited"] is True
        assert false_pass["decide_passed_slices"] == 1
        assert false_pass["audited_defect_slices"] == 1
        assert false_pass["rate"] == 1.0
    finally:
        conn.close()
        shutil.rmtree(workspace, ignore_errors=True)
