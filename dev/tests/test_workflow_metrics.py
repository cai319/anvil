"""Workflow metrics, planned/actual graphs and execution dossiers."""
from __future__ import annotations

from anvil.db import connect
from anvil.services import dossier as dossier_service
from anvil.services import metrics
from anvil.services import workflow as wf_service
from anvil.util import now_iso


def _conn(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','t','todo',?,?)",
        (now, now))
    conn.commit()
    return conn


def _starter(step_id: str):
    return lambda role, task_id, prompt, step: {"id": f"run-{step_id}"}


def test_metrics_after_gate_and_decision(tmp_path):
    conn = _conn(tmp_path)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"},
        {"id": "G1", "title": "评审", "role": "qa", "gate": True,
         "reviewer": "lead", "depends_on": ["S1"]},
    ]})
    wf_service.advance(conn, wf["id"], starter=_starter("S1"))
    wf_service.submit_step(conn, wf["id"], "S1", "dev", "完成")
    wf_service.advance(conn, wf["id"], starter=_starter("G1"))
    wf_service.submit_step(conn, wf["id"], "G1", "qa", "评审完成")
    wf_service.review_step(conn, wf["id"], "G1", True, "通过")
    m = metrics.compute(conn, wf["id"])
    assert m["steps"]["total"] == 2
    assert m["steps"]["done"] == 2
    assert m["steps"]["max_depth"] == 2
    assert m["gates"]["total"] == 1
    assert m["gates"]["pending"] == 0
    assert m["decisions"]["total"] == 1
    assert m["decisions"]["human"] == 1
    wf_row = conn.execute("SELECT status FROM workflows WHERE id=?", (wf["id"],)).fetchone()
    assert wf_row["status"] == "done"
    dossier = dossier_service.get(conn, wf["id"])
    assert dossier["digest"]
    assert dossier["metrics"]["steps"]["done"] == 2
    md = dossier_service.render_md(dossier)
    assert "执行档案" in md
    assert "G1" in md


def test_planned_actual_diff_includes_generated_slices(tmp_path):
    conn = _conn(tmp_path)
    definition = {
        "steps": [
            {"id": "P1", "title": "盘点切片", "role": "lead", "kind": "plan_slices"},
            {"id": "F1", "title": "最终报告", "role": "lead", "depends_on": ["P1"]},
        ],
        "loop": {
            "from": "P1",
            "execute": {"role": "dev"},
            "verify": {"role": "qa"},
            "fix": {"role": "dev"},
            "verify2": {"role": "qa"},
            "decide": {"role": "lead"},
        },
        "weight": "strict",
    }
    wf = wf_service.create_workflow(conn, "T1", definition)
    wf_service.advance(conn, wf["id"], starter=_starter("P1"))
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", {
        "rationale": "按模块",
        "slices": [{"id": "auth", "title": "认证模块"}],
    })
    planned = metrics.planned_graph(conn, wf["id"])
    actual = metrics.actual_graph(conn, wf["id"])
    diff = metrics.graph_diff(conn, wf["id"])
    assert {n["step_id"] for n in planned["nodes"]} == {"P1", "F1"}
    assert len(actual["nodes"]) > len(planned["nodes"])
    assert any(n["source_step_id"] == "P1" for n in diff["added"])
    generated = [n for n in actual["nodes"] if n["source_step_id"] == "P1"]
    assert generated
    # Strict weight requires evidence on generated task steps.
    row = conn.execute(
        "SELECT evidence_required FROM workflow_steps WHERE workflow_id=? "
        "AND step_id=? ", (wf["id"], generated[0]["step_id"])).fetchone()
    assert row["evidence_required"] == 1


def test_light_weight_skips_fix_and_verify2(tmp_path):
    conn = _conn(tmp_path)
    definition = {
        "steps": [{"id": "P1", "title": "盘点切片", "role": "lead",
                   "kind": "plan_slices"}],
        "loop": {
            "from": "P1",
            "execute": {"role": "dev"},
            "verify": {"role": "qa"},
            "decide": {"role": "lead"},
        },
        "weight": "light",
    }
    wf = wf_service.create_workflow(conn, "T1", definition)
    wf_service.advance(conn, wf["id"], starter=_starter("P1"))
    wf = wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", {
        "slices": [{"id": "a", "title": "小改动"}]})
    ids = {s["step_id"] for s in wf["steps"]}
    assert "P1.a.execute" in ids
    assert "P1.a.verify" in ids
    assert "P1.a.decide" in ids
    assert "P1.a.fix" not in ids
    assert "P1.a.verify2" not in ids
    decide = next(s for s in wf["steps"] if s["step_id"] == "P1.a.decide")
    assert decide["depends_on"] == ["P1.a.verify"]
    assert set(decide["on_reject"]) == {"P1.a.execute", "P1.a.verify"}


def test_dossier_persisted_on_termination(tmp_path):
    conn = _conn(tmp_path)
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "S1", "title": "实现", "role": "dev"}]})
    wf_service._terminate_running_workflow(conn, wf["id"], "人工终止", now_iso())
    conn.commit()
    row = conn.execute(
        "SELECT status, dossier_json FROM workflow_dossiers WHERE workflow_id=?",
        (wf["id"],)).fetchone()
    assert row is not None
    assert row["status"] == "terminated"
    assert "planned" in row["dossier_json"]
