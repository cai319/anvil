"""decide≠verify 模板规则与 stage 上下文槽（ticket #11）。

新 loop 模板要求 decide.role ≠ verify.role；存量相等模板的 decide 步骤改走
带 stage 的独立上下文槽（role, task, slice, stage）。这些测试只通过服务层
公开入口断言持久化行为，不依赖分类器/路由器内部实现。
"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import tasks as tasks_service
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso


pytest.importorskip("anvil.graph")


def _conn(tmp_path, assignees=()):
    conn = connect(str(tmp_path / "stage.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','t','todo',?,?)", (now, now))
    if assignees:
        tasks_service.update(conn, "T1", assignees=list(assignees))
    conn.commit()
    return conn


def _loop(verify_role="qa", decide_role="qa"):
    return {
        "steps": [
            {"id": "P1", "title": "盘点", "role": decide_role,
             "kind": "plan_slices"},
        ],
        "loop": {
            "from": "P1",
            "execute": {"role": "dev"},
            "verify": {"role": verify_role},
            "decide": {"role": decide_role, "max_rework": 2},
        },
    }


def test_new_loop_rejects_equal_decide_verify(tmp_path):
    """新 loop 模板在定义校验阶段拒绝 decide.role == verify.role。"""
    conn = _conn(tmp_path)
    try:
        definition = _loop(verify_role="qa", decide_role="qa")
        with pytest.raises(ConflictError, match="decide.role must differ"):
            wf_service.validate_definition(conn, definition)
        with pytest.raises(ConflictError, match="decide.role must differ"):
            wf_service.create_workflow(conn, "T1", definition)
    finally:
        conn.close()


def test_new_template_publish_rejects_equal_decide_verify(tmp_path):
    """模板发布校验：decide 与 verify 复用同一 Meeple 直接报错。"""
    from anvil.services import templates as templates_service

    conn = _conn(tmp_path)
    try:
        definition = {
            "schema_version": 1,
            "role_slots": [
                {"key": "exec", "label": "执行"},
                {"key": "checker", "label": "验证与决策"},
            ],
            "steps": [{"id": "P1", "title": "切片", "role_slot": "exec",
                       "kind": "plan_slices", "depends_on": []}],
            "loop": {
                "from": "P1",
                "execute": {"role_slot": "exec", "subagent_hint": False},
                "verify": {"role_slot": "checker", "subagent_hint": False},
                "decide": {"role_slot": "checker", "subagent_hint": False},
            },
        }
        issues = templates_service.validate_template(conn, definition)
        assert any(i["severity"] == "error" and "decide.role must differ" in i["message"]
                   for i in issues)

        # 模板定义自带存量放行标记也不能绕过新模板规则。
        from anvil.services.workflow_definition import LEGACY_EQUAL_ROLE_MARKER
        tampered = dict(definition)
        tampered[LEGACY_EQUAL_ROLE_MARKER] = True
        issues = templates_service.validate_template(conn, tampered)
        assert any(i["severity"] == "error" and "decide.role must differ" in i["message"]
                   for i in issues)
    finally:
        conn.close()


def test_legacy_template_marker_allows_equal_roles(tmp_path):
    """存量模板编译后由服务端打标放行；新定义没有标记则被拒绝。"""
    from anvil.services import templates as templates_service
    from anvil.services.workflow_definition import LEGACY_EQUAL_ROLE_MARKER

    conn = _conn(tmp_path)
    try:
        definition = _loop(verify_role="qa", decide_role="qa")
        with pytest.raises(ConflictError):
            wf_service.validate_definition(conn, definition)
        compiled = dict(definition)
        templates_service._mark_legacy_equal_loop(compiled)
        assert compiled[LEGACY_EQUAL_ROLE_MARKER] is True
        wf_service.validate_definition(conn, compiled)  # 不抛错
    finally:
        conn.close()


def test_equal_role_loop_stages_decide_step_only(tmp_path):
    """存量相等模板：只有 decide 改走 stage 槽，verify 仍留在普通切片槽。"""
    conn = _conn(tmp_path)
    try:
        loop = _loop(verify_role="qa", decide_role="qa")["loop"]
        generated = wf_service._dynamic_slice_steps(
            conn, "P1", loop, [{"id": "A", "title": "认证"}], set())
        by_id = {s["id"]: s for s in generated}
        assert by_id["P1.A.decide"]["stage"] == "decide"
        assert by_id["P1.A.verify"].get("stage") is None
        assert by_id["P1.A.verify2"].get("stage") is None
        assert by_id["P1.A.execute"].get("stage") is None
    finally:
        conn.close()


def test_separated_role_loop_keeps_default_context_slot(tmp_path):
    """新模板 decide≠verify：decide 不启用 stage 槽，行为不回归。"""
    conn = _conn(tmp_path)
    try:
        loop = _loop(verify_role="qa", decide_role="lead")["loop"]
        generated = wf_service._dynamic_slice_steps(
            conn, "P1", loop, [{"id": "A", "title": "认证"}], set())
        by_id = {s["id"]: s for s in generated}
        assert by_id["P1.A.decide"].get("stage") is None
    finally:
        conn.close()


def test_static_slice_equal_roles_stages_decide_step():
    """静态存量切片同样隔离：decide 步骤带 stage，verify 不带。"""
    expanded = wf_service._expand_slices({"slices": [{
        "id": "A", "title": "认证",
        "execute_role": "dev", "verify_role": "qa", "decision_role": "qa",
    }]})
    by_id = {s["id"]: s for s in expanded["steps"]}
    assert by_id["A.decide"]["stage"] == "decide"
    assert by_id["A.verify"].get("stage") is None
    assert by_id["A.verify2"].get("stage") is None


def test_migration_adds_stage_columns(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    try:
        step_cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_steps)")}
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert "stage" in step_cols
        assert "stage" in run_cols
    finally:
        conn.close()


DYNAMIC_PLAN = {"slices": [{"id": "A", "title": "认证"}]}


def test_migration_backfills_legacy_decide_stage(tmp_path):
    """升级前已展开的存量 workflow：待执行 decide 步骤补标 stage，幂等。"""
    from anvil import db as db_module
    from anvil.util import jdumps

    conn = connect(str(tmp_path / "m2.db"))
    try:
        now = now_iso()
        loop = _loop(verify_role="qa", decide_role="qa")["loop"]
        conn.execute(
            "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
            "VALUES ('W1','T1',?,'running',?,?)",
            (jdumps({"loop": loop}), now, now))
        conn.execute(
            "INSERT INTO workflow_steps "
            "(workflow_id,step_id,title,role_slug,kind,updated_at) "
            "VALUES ('W1','P1.A.decide','决策','qa','decision',?)", (now,))
        conn.execute("DELETE FROM settings WHERE key='schema.stage_slot_v1'")
        conn.execute("UPDATE workflow_steps SET stage=NULL")
        conn.commit()

        db_module._migrate_stage_slot_v1(conn)
        row = conn.execute(
            "SELECT stage FROM workflow_steps WHERE workflow_id='W1'").fetchone()
        assert row["stage"] == "decide"
        db_module._migrate_stage_slot_v1(conn)  # 幂等
        row2 = conn.execute(
            "SELECT stage FROM workflow_steps WHERE workflow_id='W1'").fetchone()
        assert row2["stage"] == "decide"
    finally:
        conn.close()


def _existing_equal_workflow(conn):
    """模拟升级前落库的 loop 模板：创建后把存储定义改成 decide==verify。"""
    from anvil.util import jdumps
    definition = _loop(verify_role="qa", decide_role="lead")
    wf = wf_service.create_workflow(conn, "T1", definition)
    stored = dict(definition)
    stored["loop"] = _loop(verify_role="qa", decide_role="qa")["loop"]
    conn.execute("UPDATE workflows SET definition_json=? WHERE id=?",
                 (jdumps(stored), wf["id"]))
    conn.commit()
    return wf


def _advance(conn, wf):
    started = []

    def starter(role, task_id, prompt, step):
        started.append(step["step_id"])
        return {"id": f"run-{step['step_id']}"}

    wf_service.advance(conn, wf["id"], starter=starter)
    return started


def test_existing_equal_workflow_persists_stage_on_decide(tmp_path):
    """存量相等模板展开后，decide 步骤的 stage 落库且经 API 形状可查。"""
    conn = _conn(tmp_path)
    try:
        wf = _existing_equal_workflow(conn)
        assert _advance(conn, wf) == ["P1"]
        wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
        by_id = {s["step_id"]: s for s in wf["steps"]}
        assert by_id["P1.A.decide"]["stage"] == "decide"
        assert by_id["P1.A.verify"].get("stage") is None
        row = conn.execute(
            "SELECT stage FROM workflow_steps WHERE workflow_id=? AND step_id=?",
            (wf["id"], "P1.A.decide")).fetchone()
        assert row["stage"] == "decide"
        row = conn.execute(
            "SELECT stage FROM workflow_steps WHERE workflow_id=? AND step_id=?",
            (wf["id"], "P1.A.verify")).fetchone()
        assert row["stage"] in (None, "")
    finally:
        conn.close()


def test_run_persists_stage_and_delivery_uses_stage_slot(tmp_path, monkeypatch):
    """step.stage → run.stage → 投递上下文 key，整条链路使用独立槽。"""
    from anvil.services import daemon as daemon_service
    from anvil.services import runs as runs_service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "data" / "agents")
    conn = connect(str(tmp_path / "deliver.db"))
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T1','t','in_progress',?,?)", (now, now))
        conn.commit()
        run = runs_service.create_run(
            conn, "qa", "T1", prompt="x", slice_key="A", stage="decide")
        row = conn.execute("SELECT stage FROM runs WHERE id=?", (run["id"],)).fetchone()
        assert row["stage"] == "decide"

        mgr = daemon_service.DaemonManager(str(tmp_path / "deliver.db"), enabled=False)
        captured = {}
        mgr._switch_context_inner = (
            lambda conn, r, item, key: captured.setdefault("key", key) or True)
        mgr.ensure_and_deliver(
            conn, "qa", executable="", args_new=[], args_resume=[], ctx={},
            cwd=str(tmp_path), env={}, prompt="x", run_id=run["id"],
            task_id="T1", slice_key="A", stage="decide")
        assert captured["key"] == "T1::slice::A::stage::decide"
    finally:
        conn.close()


def test_stage_slot_stats_count_triggers(tmp_path, monkeypatch):
    from anvil.services import metrics as metrics_service
    from anvil.services import runs as runs_service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "data" / "agents")
    conn = _conn(tmp_path)
    try:
        wf = _existing_equal_workflow(conn)
        _advance(conn, wf)
        wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", DYNAMIC_PLAN)
        runs_service.create_run(
            conn, "qa", "T1", prompt="x", slice_key="A", stage="decide",
            workflow_id=wf["id"])

        stats = metrics_service.stage_slot_stats(conn)
        assert stats["decide_steps"] == 1
        assert stats["runs"] == 1
        assert stats["workflows"] == 1
        assert stats["by_role"] == {"qa": 1}

        per_wf = metrics_service.compute(conn, wf["id"])["stage_slot"]
        assert per_wf["decide_steps"] == 1
        assert per_wf["runs"] == 1
    finally:
        conn.close()
