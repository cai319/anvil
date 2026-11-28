"""显式 replan 与弱化检测（v1.62 #10）。

- replan 复用 reject-to-ancestor 重置：scope 与其下游回到 pending；
- 审计记录原因与新旧 spec 哈希；每片最多 1 次，第二次升级人工；
- 删除检查 / 降为 manual / 修改断言但无新红证据 → 人工批准；
- replan 后必须重跑红校验，旧绿证据不得沿用。
"""
from __future__ import annotations

import copy
import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import jobs
from anvil.services import ledger, reviews, sandbox, slice_spec, slice_verdicts
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
EXTRA_TEST_FILE = "tests/test_extra.py"
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


def _slice_spec() -> dict:
    return {
        "acceptance": [{
            "id": "a1",
            "kind": "test",
            "criterion": "login returns True for valid user",
            "verification": {"pytest": {"paths": [TEST_FILE], "args": ["-q"]}},
        }],
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
def replan_env(tmp_path):
    workspace = config.TASK_WORKSPACES_DIR / "T10"
    import shutil
    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    (workspace / EXTRA_TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    (workspace / STUB_FILE).write_text(STUB_SOURCE, encoding="utf-8")
    (workspace / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    db_path = str(tmp_path / "replan.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T10','replan','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T10", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    yield conn, db_path, wf["id"], workspace
    conn.close()
    shutil.rmtree(workspace, ignore_errors=True)


def _step(conn, wid, step_id):
    return next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == step_id)


def _scope(conn, wid, spec):
    wf_service.submit_step(
        conn, wid, "P1.auth.scope", "lead", "spec ready", slice_spec=spec)


def _run_checks(conn, db_path, runner=None):
    worker = SandboxJobWorker(
        db_path, runner=runner or FakePytestRunner())
    while worker.run_once(conn):
        pass


def _advance(conn, wid, n):
    wf_service.advance(conn, wid, starter=lambda *a: {"id": f"r{n}"})


def _submit(conn, wid, step_id, role):
    wf_service.submit_step(conn, wid, step_id, role, "done")


def _to_verify2(conn, wid, spec):
    _scope(conn, wid, spec)
    _advance(conn, wid, 3)
    _submit(conn, wid, "P1.auth.impl", "auditor")
    _advance(conn, wid, 4)
    _submit(conn, wid, "P1.auth.verify", "reviewer")
    _advance(conn, wid, 5)
    _submit(conn, wid, "P1.auth.fix", "developer")
    _advance(conn, wid, 6)


def _red_jobs(conn):
    return [j for j in sandbox.list_jobs(conn, task_id="T10", kind="check")
            if not (j.get("spec") or {}).get("green_ref")]


def _mention_targets(conn):
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE task_id='T10' "
        "AND event_type='message.mention' ORDER BY id").fetchall()
    return [json.loads(r["payload_json"]).get("target") for r in rows]


# --- replan reset + audit ---------------------------------------------------

def test_replan_resets_scope_and_downstream_and_audits_hashes(replan_env):
    conn, _db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, _db_path)
    old_spec = _step(conn, wid, "P1.auth.scope")["slice_spec"]
    new_raw = copy.deepcopy(_slice_spec())
    new_raw["seam"] = "updated auth seam"
    expected_new = slice_spec.parse_slice_spec(new_raw)

    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "lead", "seam clarified", new_spec=new_raw)

    steps = {s["step_id"]: s for s in wf_service.get_workflow(conn, wid)["steps"]}
    for sid in ("P1.auth.scope", "P1.auth.impl", "P1.auth.verify",
                "P1.auth.fix", "P1.auth.verify2", "P1.auth.decide"):
        assert steps[sid]["status"] == "pending", sid
    scope = steps["P1.auth.scope"]
    assert scope["feedback"] == "seam clarified"
    # 旧锁定集合作废，重开 scope 后必须重新锁定并重跑红校验。
    assert scope["slice_lock"] is None
    replan = scope["slice_replan"]
    assert replan["count"] == 1
    assert replan["reason"] == "seam clarified"
    assert replan["old_spec_hash"] == slice_spec.spec_hash(old_spec)
    assert replan["new_spec_hash"] == slice_spec.spec_hash(expected_new)
    assert replan["weakening"] == []

    events = ledger.list_events(
        conn, workflow_id=wid, event_type="slice.replanned")["items"]
    assert len(events) == 1
    payload = events[-1]["payload"]
    assert payload["reason"] == "seam clarified"
    assert payload["old_spec_hash"] == slice_spec.spec_hash(old_spec)
    assert payload["new_spec_hash"] == slice_spec.spec_hash(expected_new)


def test_replan_requires_a_reason(replan_env):
    conn, _db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    with pytest.raises(ConflictError) as excinfo:
        wf_service.replan_slice(
            conn, wid, "P1.auth.scope", "lead", "   ")
    assert "reason" in str(excinfo.value)


# --- weakening requires a human --------------------------------------------

@pytest.mark.parametrize("weakening", [
    "deleted",
    "downgraded",
    "assertion_changed",
])
def test_weakening_replan_escalates_to_human(replan_env, weakening):
    conn, _db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, _db_path)
    new_raw = copy.deepcopy(_slice_spec())
    if weakening == "deleted":
        new_raw["acceptance"] = []
    elif weakening == "downgraded":
        new_raw["acceptance"][0] = {
            "id": "a1", "kind": "manual",
            "criterion": "login returns True for valid user",
            "verification": {"judge": "decide", "method": "read the diff"},
        }
    else:
        new_raw["acceptance"][0]["verification"]["pytest"]["args"] = ["-q", "-x"]

    with pytest.raises(ConflictError) as excinfo:
        wf_service.replan_slice(
            conn, wid, "P1.auth.scope", "lead", "adjust acceptance",
            new_spec=new_raw)
    assert "human" in str(excinfo.value)
    # 未批准的弱化 replan 不改变步骤状态，也不消耗重开次数。
    scope = _step(conn, wid, "P1.auth.scope")
    assert scope["status"] == "approved"
    assert scope["slice_replan"] is None
    escalations = ledger.list_events(
        conn, workflow_id=wid,
        event_type="slice.human_approval_required")["items"]
    assert len(escalations) == 1
    assert escalations[0]["payload"]["action"] == "replan"
    assert escalations[0]["payload"]["weakening"][0]["type"]
    assert _mention_targets(conn)[-1] == "human"
    # 未应用的 replan 不写入 slice.replanned，避免台账虚增 replan 次数。
    assert ledger.list_events(
        conn, workflow_id=wid, event_type="slice.replanned")["items"] == []


def test_assertion_change_with_new_engine_red_evidence_is_not_weakening(
        replan_env):
    conn, db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, db_path)
    red_job = _red_jobs(conn)[0]
    assert red_job["check_result"]["classification"] == "valid_red"
    new_raw = copy.deepcopy(_slice_spec())
    new_raw["acceptance"][0]["verification"]["pytest"]["args"] = ["-q", "-x"]

    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "lead", "assertion tightened",
        new_spec=new_raw, red_evidence=[red_job["id"]])

    replan = _step(conn, wid, "P1.auth.scope")["slice_replan"]
    assert replan["count"] == 1
    assert replan["weakening"] == []
    assert replan["approved_by"] == "lead"
    assert replan["red_evidence"] == [red_job["id"]]
    assert replan["red_evidence_checks"] == ["a1"]


def test_human_can_approve_a_weakening_replan(replan_env):
    conn, _db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, _db_path)
    new_raw = copy.deepcopy(_slice_spec())
    new_raw["acceptance"] = []

    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "human", "acceptance was over-specified",
        new_spec=new_raw)

    scope = _step(conn, wid, "P1.auth.scope")
    assert scope["status"] == "pending"
    replan = scope["slice_replan"]
    assert replan["count"] == 1
    assert replan["approved_by"] == "human"
    assert replan["weakening"] == [
        {"id": "a1", "type": "check_removed"}]

    # 人工批准的是这份新 spec；重新提交它本身不再被弱化守卫拦截。
    _advance(conn, wid, 3)
    _scope(conn, wid, new_raw)
    assert _step(conn, wid, "P1.auth.scope")["status"] == "approved"


def test_second_replan_escalates_to_human(replan_env):
    conn, _db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, _db_path)
    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "lead", "first replan")
    _advance(conn, wid, 3)
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, _db_path)

    with pytest.raises(ConflictError) as excinfo:
        wf_service.replan_slice(
            conn, wid, "P1.auth.scope", "lead", "second replan")
    assert "human" in str(excinfo.value)
    assert _step(conn, wid, "P1.auth.scope")["slice_replan"]["count"] == 1

    # 人类可以批准第二次重开。
    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "human", "second replan approved")
    assert _step(conn, wid, "P1.auth.scope")["slice_replan"]["count"] == 2


def test_enabling_slice_replan_follows_the_same_constraints(replan_env):
    conn, _db_path, _wid, workspace = replan_env
    _ = _db_path
    other = config.TASK_WORKSPACES_DIR / "T11"
    if other.exists():
        import shutil
        shutil.rmtree(other)
    import shutil
    shutil.copytree(workspace, other)
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T11','enabling replan','in_progress',?,?)",
        (now_iso(), now_iso()))
    conn.commit()
    plan = {"rationale": "enabling", "slices": [
        {"id": "base", "title": "基线", "enabling": True, "enables": ["feature"],
         "scope_paths": ["src/base/**"],
         "execute_steps": [{"id": "impl", "title": "预重构"}]},
        {"id": "feature", "title": "功能", "depends_on": ["base"],
         "scope_paths": ["src/feature/**"],
         "execute_steps": [{"id": "impl", "title": "实现"}]},
    ]}
    wf = wf_service.create_workflow(conn, "T11", {
        "steps": [{"id": "P2", "title": "计划", "role": "lead",
                   "kind": "plan_slices"}],
        "loop": {**WF["loop"], "from": "P2"},
    })
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "e1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P2", "lead", plan)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "e2"})
    wf_service.submit_step(
        conn, wf["id"], "P2.base.scope", "lead", "spec ready",
        slice_spec=_slice_spec())
    new_raw = copy.deepcopy(_slice_spec())
    new_raw["acceptance"] = []
    with pytest.raises(ConflictError) as excinfo:
        wf_service.replan_slice(
            conn, wf["id"], "P2.base.scope", "lead", "drop check",
            new_spec=new_raw)
    assert "human" in str(excinfo.value)


# --- resubmission and red re-run -------------------------------------------

def test_scope_resubmit_after_replan_cannot_weaken_without_human(replan_env):
    conn, _db_path, wid, _ws = replan_env
    _scope(conn, wid, _slice_spec())
    _run_checks(conn, _db_path)
    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "lead", "reopen scope")
    _advance(conn, wid, 3)

    weakened = copy.deepcopy(_slice_spec())
    weakened["acceptance"] = []
    with pytest.raises(ConflictError) as excinfo:
        wf_service.submit_step(
            conn, wid, "P1.auth.scope", "lead", "weakened", slice_spec=weakened)
    assert "replan_weakening_requires_human" in str(excinfo.value)
    assert _step(conn, wid, "P1.auth.scope")["status"] == "running"


def test_replan_reruns_red_checks_and_ignores_old_green_evidence(replan_env):
    conn, db_path, wid, _ws = replan_env
    _to_verify2(conn, wid, _slice_spec())
    _run_checks(conn, db_path)  # red
    wf_service.submit_step(
        conn, wid, "P1.auth.verify2", "reviewer", "final verify")
    _run_checks(conn, db_path, FakePytestRunner(exit_code=0, junit=JUNIT_PASS))
    red_before = len(_red_jobs(conn))
    green_before = [j for j in sandbox.list_jobs(
        conn, task_id="T10", kind="check")
        if (j.get("spec") or {}).get("green_ref")]
    assert green_before
    wf = wf_service.get_workflow(conn, wid)
    decide = next(s for s in wf["steps"] if s["step_id"] == "P1.auth.decide")
    assert slice_verdicts.build_verdicts(conn, wf, decide)[0]["status"] == "met"

    new_raw = copy.deepcopy(_slice_spec())
    new_raw["seam"] = "auth seam v2"
    wf_service.replan_slice(
        conn, wid, "P1.auth.scope", "lead", "re-run checks", new_spec=new_raw)

    # replan 之后的旧绿跑证据不算数，decide 看到 unknown。
    wf = wf_service.get_workflow(conn, wid)
    decide = next(s for s in wf["steps"] if s["step_id"] == "P1.auth.decide")
    assert slice_verdicts.build_verdicts(conn, wf, decide)[0]["status"] == "unknown"

    _advance(conn, wid, 3)
    _scope(conn, wid, new_raw)
    assert len(_red_jobs(conn)) == red_before + 1
    _run_checks(conn, db_path)
    latest = _step(conn, wid, "P1.auth.scope")["slice_lock"]
    red = next(c for c in latest["checks"] if c["id"] == "P1.auth.scope.a1")
    assert red["env_hash"]
    assert _red_jobs(conn)[0]["check_result"]["classification"] == "valid_red"
    assert reviews.decisions_for_step(conn, wid, "P1.auth.decide") == []
