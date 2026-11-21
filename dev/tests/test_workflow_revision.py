"""Workflow 增量修订：只改未来切片/步骤，不重启 execution。"""
from __future__ import annotations

import json
import threading

import pytest

from anvil.db import connect
from anvil.services import board as board_service
from anvil.services import leases as leases_service
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso

WF = {
    "steps": [
        {"id": "P1", "title": "切片计划", "role": "lead", "kind": "plan_slices"},
        {"id": "F1", "title": "最终报告", "role": "lead", "depends_on": ["P1"]},
    ],
    "loop": {
        "from": "P1",
        "execute": {"role": "auditor", "title": "执行"},
        "verify": {"role": "reviewer", "title": "验证"},
        "fix": {"role": "developer", "title": "修复"},
        "verify2": {"role": "reviewer", "title": "复验"},
        "decide": {"role": "lead", "title": "决策", "max_rework": 2},
    },
}
PLAN = {"rationale": "按模块切片", "slices": [
    {"id": "auth", "title": "认证", "scope_paths": ["src/auth/**"],
     "execute_steps": [{"id": "static", "title": "静态审计"}]},
    {"id": "pay", "title": "支付", "scope_paths": ["src/pay/**"],
     "execute_steps": [{"id": "static", "title": "静态审计"}]},
]}


@pytest.fixture
def revision_env(tmp_path):
    conn = connect(str(tmp_path / "revision.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','rev','in_progress',?,?)", (now, now))
    conn.commit()
    wf = wf_service.create_workflow(conn, "T1", WF)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    # auth 已全部完成，pay 仍全部 pending；F1 依赖 pay.decide。
    conn.execute(
        "UPDATE workflow_steps SET status='approved' "
        "WHERE workflow_id=? AND slice_key='auth'", (wf["id"],))
    conn.commit()
    yield conn, wf["id"]
    conn.close()


def _steps(conn, wid):
    return {s["step_id"]: s for s in wf_service.get_workflow(conn, wid)["steps"]}


def test_append_slices_in_place_without_restart(revision_env):
    conn, wid = revision_env
    before = wf_service.get_workflow(conn, wid)
    intent = {
        "add_slices": [{
            "id": "auth2", "title": "认证补充",
            "scope_paths": ["src/auth/**"],
            "execute_steps": [{"id": "static", "title": "补充审计"}],
        }],
        "rationale": "认证还需要一轮",
    }
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True
    assert impact["classification"] == "safe_loop"

    after = wf_service.apply_revision(conn, wid, intent, actor="human")
    assert after["id"] == before["id"]
    assert after["execution_no"] == before["execution_no"]
    assert after["status"] == "running"

    steps = _steps(conn, wid)
    assert "P1.auth2.static" in steps
    assert steps["P1.pay.static"]["status"] == "pending"
    assert steps["P1.auth2.static"]["depends_on"] == ["P1.pay.decide"]
    assert steps["F1"]["depends_on"] == ["P1.auth2.decide"]
    assert steps["P1.auth.static"]["status"] == "approved"
    plan = json.loads(steps["P1"]["plan_json"])
    assert [s["id"] for s in plan["slices"]] == ["auth", "pay", "auth2"]


def test_append_two_slices_while_current_slice_running(revision_env):
    """当前片 running 时只能在其后追加；不终止 run、不改 execution。"""
    conn, wid = revision_env
    conn.execute(
        "UPDATE workflow_steps SET status='running', run_id='run-pay' "
        "WHERE workflow_id=? AND step_id='P1.pay.static'", (wid,))
    conn.commit()
    intent = {"add_slices": [
        {"id": "x1", "title": "补充一", "execute_steps": [{"id": "static"}]},
        {"id": "x2", "title": "补充二", "execute_steps": [{"id": "static"}]},
    ]}
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True, impact

    after = wf_service.apply_revision(conn, wid, intent, actor="human")
    assert after["execution_no"] == 1
    assert after["status"] == "running"
    steps = _steps(conn, wid)
    # 当前 run 原样保留，未被终止或重建。
    assert steps["P1.pay.static"]["status"] == "running"
    assert steps["P1.pay.static"]["run_id"] == "run-pay"
    # 新片接在当前片之后，最终静态步骤等最后一片。
    assert steps["P1.x1.static"]["depends_on"] == ["P1.pay.decide"]
    assert steps["P1.x2.static"]["depends_on"] == ["P1.x1.decide"]
    assert steps["F1"]["depends_on"] == ["P1.x2.decide"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM workflows WHERE task_id='T1'").fetchone()["n"] == 1


def test_update_pending_slice_spec(revision_env):
    conn, wid = revision_env
    intent = {"update_slices": [{
        "id": "pay",
        "patch": {"title": "支付补强", "acceptance": ["覆盖退款路径"]},
    }]}
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True
    wf_service.apply_revision(conn, wid, intent, actor="human")
    plan = json.loads(_steps(conn, wid)["P1"]["plan_json"])
    pay = next(s for s in plan["slices"] if s["id"] == "pay")
    assert pay["title"] == "支付补强"
    assert pay["acceptance"] == ["覆盖退款路径"]
    assert "支付补强" in _steps(conn, wid)["P1.pay.static"]["title"]


def test_update_slice_cannot_change_id(revision_env):
    conn, wid = revision_env
    impact = wf_service.preview_revision(conn, wid, {
        "update_slices": [{"id": "pay", "patch": {"id": "pay2", "title": "改名"}}],
    })
    assert impact["classification"] == "invalid"
    assert "id" in impact["summary"]


def test_apply_revision_refreshes_dossier(revision_env):
    """修订后的 planned graph/dossier 必须同步，不能保留旧快照。"""
    from anvil.services import dossier

    conn, wid = revision_env
    dossier.persist(conn, wid)
    intent = {"add_slices": [{"id": "auth2", "title": "补充",
                              "execute_steps": [{"id": "static"}]}]}
    after = wf_service.apply_revision(conn, wid, intent, actor="human")
    row = conn.execute(
        "SELECT dossier_json, definition_hash FROM workflow_dossiers "
        "WHERE workflow_id=?", (wid,)).fetchone()
    assert row is not None
    assert "P1.auth2.static" in row["dossier_json"]
    assert row["definition_hash"] == after["definition_hash"]


def test_patch_only_ignores_started_downstream_steps(tmp_path):
    """仅 patch 未来静态步骤时，不应因为别的最终步骤已开始而要求重启。"""
    conn = connect(str(tmp_path / "patch_only.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T3','rev','in_progress',?,?)", (now, now))
    conn.commit()
    definition = {
        **WF,
        "steps": WF["steps"] + [
            {"id": "F2", "title": "附录", "role": "lead", "depends_on": ["P1"]},
        ],
    }
    wf = wf_service.create_workflow(conn, "T3", definition)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r3"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=? "
        "AND step_id IN ('P1','F1')", (wf["id"],))
    conn.commit()
    before_slice_ids = {
        r["step_id"] for r in conn.execute(
            "SELECT step_id FROM workflow_steps WHERE workflow_id=? "
            "AND source_step_id='P1'", (wf["id"],)).fetchall()}
    impact = wf_service.preview_revision(conn, wf["id"], {
        "patch_steps": [{"step_id": "F2",
                         "fields": {"instructions": "补充附录要求"}}],
    })
    assert impact["classification"] == "safe_future", impact
    wf_service.apply_revision(conn, wf["id"], {
        "patch_steps": [{"step_id": "F2",
                         "fields": {"instructions": "补充附录要求"}}],
    }, actor="human")
    after_slice_ids = {
        r["step_id"] for r in conn.execute(
            "SELECT step_id FROM workflow_steps WHERE workflow_id=? "
            "AND source_step_id='P1'", (wf["id"],)).fetchall()}
    assert after_slice_ids == before_slice_ids
    conn.close()


def test_patch_only_can_depend_on_existing_pending_slice(revision_env):
    """仅 patch 时切片未重建，依赖现有 pending 切片步骤仍然合法。"""
    conn, wid = revision_env
    impact = wf_service.preview_revision(conn, wid, {
        "patch_steps": [{"step_id": "F1",
                         "fields": {"depends_on": ["P1.pay.static"]}}],
    })
    assert impact["classification"] == "safe_future", impact


def test_patch_dependency_cycle_is_invalid(tmp_path):
    """patch 引入依赖环时必须在预览阶段拒绝，而不是批准后才失败。"""
    conn = connect(str(tmp_path / "cycle.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T4','rev','in_progress',?,?)", (now, now))
    conn.commit()
    definition = {
        **WF,
        "steps": WF["steps"] + [
            {"id": "F2", "title": "附录", "role": "lead", "depends_on": ["P1"]},
        ],
    }
    wf = wf_service.create_workflow(conn, "T4", definition)
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r4"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    conn.commit()
    impact = wf_service.preview_revision(conn, wf["id"], {
        "patch_steps": [
            {"step_id": "F1", "fields": {"depends_on": ["F2"]}},
            {"step_id": "F2", "fields": {"depends_on": ["F1"]}},
        ],
    })
    assert impact["classification"] == "invalid", impact
    assert "cycle" in impact["summary"]
    conn.close()


def test_remove_pending_slice_rewires_final_step(revision_env):
    conn, wid = revision_env
    impact = wf_service.preview_revision(
        conn, wid, {"remove_slices": ["pay"]})
    assert impact["safe"] is True
    wf_service.apply_revision(conn, wid, {"remove_slices": ["pay"]}, actor="human")

    steps = _steps(conn, wid)
    assert not any(sid.startswith("P1.pay.") for sid in steps)
    assert steps["F1"]["depends_on"] == ["P1.auth.decide"]
    plan = json.loads(steps["P1"]["plan_json"])
    assert [s["id"] for s in plan["slices"]] == ["auth"]


def test_patch_future_static_step_updates_definition(revision_env):
    conn, wid = revision_env
    before = wf_service.get_workflow(conn, wid)
    intent = {"patch_steps": [{
        "step_id": "F1",
        "fields": {"instructions": "新报告要求", "acceptance": ["必须附证据"]},
    }]}
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True
    assert impact["classification"] == "safe_future"

    after = wf_service.apply_revision(conn, wid, intent, actor="human")
    step = next(s for s in after["steps"] if s["step_id"] == "F1")
    assert step["instructions"] == "新报告要求"
    assert step["acceptance"] == ["必须附证据"]
    # patch-only 不能触发静态下游重挂：F1 仍应等待最后一片 pay。
    assert step["depends_on"] == ["P1.pay.decide"]
    definition_step = next(
        s for s in after["definition"]["steps"] if s["id"] == "F1")
    assert definition_step["instructions"] == "新报告要求"
    assert after["definition_hash"] != before["definition_hash"]


def test_append_when_final_step_already_started_requires_restart(revision_env):
    conn, wid = revision_env
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=? AND step_id='F1'",
        (wid,))
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, {
        "add_slices": [{"id": "late", "title": "迟到切片",
                        "execute_steps": [{"id": "static"}]}],
    })
    assert impact["safe"] is False
    assert impact["classification"] == "requires_restart"
    assert any(b["step_id"] == "F1" for b in impact["blocked_by"])


def test_patch_started_step_requires_restart_and_does_not_mutate(revision_env):
    conn, wid = revision_env
    before = _steps(conn, wid)["P1.auth.static"]
    impact = wf_service.preview_revision(conn, wid, {
        "patch_steps": [{"step_id": "P1.auth.static",
                         "fields": {"instructions": "change"}}],
    })
    assert impact["classification"] == "invalid"
    with pytest.raises(ConflictError):
        wf_service.apply_revision(conn, wid, {
            "patch_steps": [{"step_id": "P1.auth.static",
                             "fields": {"instructions": "change"}}]})
    assert _steps(conn, wid)["P1.auth.static"]["instructions"] == before["instructions"]


def test_apply_rechecks_state_and_rejects_stale_target(revision_env):
    conn, wid = revision_env
    intent = {"add_slices": [{"id": "auth2", "title": "补充",
                              "execute_steps": [{"id": "static"}]}]}
    assert wf_service.preview_revision(conn, wid, intent)["safe"] is True
    # 预览后 F1 被启动：apply 必须重新判定并拒绝。
    conn.execute(
        "UPDATE workflow_steps SET status='running' WHERE workflow_id=? AND step_id='F1'",
        (wid,))
    conn.commit()
    with pytest.raises(wf_service.RevisionConflict) as exc:
        wf_service.apply_revision(conn, wid, intent, actor="human")
    assert exc.value.impact["classification"] == "stale"
    assert "P1.auth2.static" not in _steps(conn, wid)


def test_patch_role_to_unbound_role_is_invalid(revision_env):
    """静态步骤只能改成任务已绑定或当前 workflow 已使用的角色。"""
    conn, wid = revision_env
    now = now_iso()
    conn.execute("UPDATE tasks SET assignee='lead' WHERE id='T1'")
    conn.execute(
        "INSERT INTO roles (slug, display_name, home_path, definition_json, "
        "created_at, updated_at, enabled) VALUES "
        "('outsider','Outsider','', '{}', ?, ?, 1)", (now, now))
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, {
        "patch_steps": [{"step_id": "F1", "fields": {"role_slug": "outsider"}}],
    })
    assert impact["classification"] == "invalid"
    assert "outsider" in impact["summary"]


def test_patch_dependency_on_removed_slice_is_invalid(revision_env):
    """patch 的依赖不能指向同一修订中会被删除的切片步骤。"""
    conn, wid = revision_env
    impact = wf_service.preview_revision(conn, wid, {
        "remove_slices": ["pay"],
        "patch_steps": [{"step_id": "F1",
                         "fields": {"depends_on": ["P1.pay.static"]}}],
    })
    assert impact["classification"] == "invalid"


def test_patch_that_drops_bound_role_coverage_is_invalid(tmp_path):
    """改静态步骤角色后，任务绑定角色仍必须出现在 workflow 中。"""
    conn = connect(str(tmp_path / "coverage.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T2','rev','in_progress',?,?)", (now, now))
    conn.execute(
        "INSERT INTO roles (slug, display_name, home_path, definition_json, "
        "created_at, updated_at, enabled) VALUES "
        "('lead','Lead','', '{}', ?, ?, 1)", (now, now))
    conn.commit()
    definition = {
        **WF,
        "steps": [
            WF["steps"][0],
            {"id": "OS1", "title": "运维确认", "role": "ops",
             "depends_on": ["P1"]},
            WF["steps"][1],
        ],
    }
    wf = wf_service.create_workflow(conn, "T2", definition)
    conn.execute(
        "INSERT INTO task_assignees (task_id, role_slug, joined_at) "
        "VALUES ('T2','ops',?)", (now,))
    conn.commit()
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r2"})
    wf_service.apply_slice_plan(conn, wf["id"], "P1", "lead", PLAN)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=? "
        "AND step_id='P1'", (wf["id"],))
    conn.commit()
    impact = wf_service.preview_revision(conn, wf["id"], {
        "patch_steps": [{"step_id": "OS1", "fields": {"role_slug": "lead"}}],
    })
    assert impact["classification"] == "invalid"
    assert "ops" in impact["summary"]
    conn.close()


def test_revision_rejected_for_closed_task(revision_env):
    conn, wid = revision_env
    conn.execute("UPDATE tasks SET status='closed' WHERE id='T1'")
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, {
        "patch_steps": [{"step_id": "F1", "fields": {"instructions": "x"}}],
    })
    assert impact["classification"] == "invalid"


def test_revision_cannot_remove_all_slices(revision_env):
    conn, wid = revision_env
    # 把 auth 也置回 pending，使全部切片可重建；全部删除仍应拒绝。
    conn.execute(
        "UPDATE workflow_steps SET status='pending' WHERE workflow_id=? AND slice_key='auth'",
        (wid,))
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, {"remove_slices": ["auth", "pay"]})
    assert impact["classification"] == "invalid"


def _add_role(conn, slug="fuzzer"):
    now = now_iso()
    conn.execute(
        "INSERT INTO roles (slug, display_name, home_path, definition_json, "
        "created_at, updated_at, enabled) VALUES (?,?,?,?,?,?,1)",
        (slug, slug.title(), "", "{}", now, now))
    conn.commit()


def _external_intent(**overrides):
    node = {
        "id": "dtfuzz",
        "title": "DT-Fuzz 验证",
        "role": "fuzzer",
        "instructions": "对已发现问题做定向 fuzz",
        "acceptance": ["每个问题有复现或无法复现说明"],
        "depends_on": ["P1"],
        "target_slice_id": "pay",
    }
    node.update(overrides)
    return {"external_nodes": [node]}


def test_external_node_targets_pending_slice(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    intent = _external_intent()
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True, impact
    assert impact["classification"] == "safe_external"
    assert impact["diff"]["restart_steps"] == []

    before_updated = wf_service.get_workflow(conn, wid)["updated_at"]
    draft = board_service.create_revision_draft(conn, wid, intent, "补 fuzz", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    steps = _steps(conn, wid)
    assert steps["EXT.dtfuzz"]["status"] == "pending"
    assert steps["EXT.dtfuzz"]["kind"] == "external"
    assert "EXT.dtfuzz" in steps["P1.pay.static"]["depends_on"]
    # pay 还没执行，不需要重置。
    assert steps["P1.pay.static"]["status"] == "pending"
    wf = wf_service.get_workflow(conn, wid)
    assert wf["status"] == "running"
    assert wf["updated_at"] != before_updated
    ext = next(s for s in wf["steps"] if s["step_id"] == "EXT.dtfuzz")
    prompt = wf_service.step_prompt(wf, ext, {"title": "t", "description": "d"})
    assert "所属切片" not in prompt


def test_external_node_reopens_done_workflow(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
    conn.commit()
    wf_service.advance(conn, wid)
    assert wf_service.get_workflow(conn, wid)["status"] == "done"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='T1'").fetchone()["status"] == "in_review"

    intent = _external_intent(
        target_slice_id=None, target_step_id="F1", restart_scope="target")
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True, impact
    assert impact["classification"] == "safe_external"
    assert impact["diff"]["restart_steps"] == ["F1"]

    draft = board_service.create_revision_draft(conn, wid, intent, "补 fuzz", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    wf = wf_service.get_workflow(conn, wid)
    steps = {s["step_id"]: s for s in wf["steps"]}
    assert wf["status"] == "running"
    assert steps["EXT.dtfuzz"]["status"] == "pending"
    assert steps["F1"]["status"] == "pending"
    assert "EXT.dtfuzz" in steps["F1"]["depends_on"]
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='T1'").fetchone()["status"] == "in_progress"
    # 收口必须重新检查步骤：旁路节点 pending 时不能被重新标记 done。
    wf_service._finalize_if_done(conn, wid)
    assert wf_service.get_workflow(conn, wid)["status"] == "running"

    started: list[str] = []
    wf_service.advance(
        conn, wid,
        starter=lambda r, t, p, s: started.append(s["step_id"]) or {"id": "run-ext"})
    assert started == ["EXT.dtfuzz"]
    wf_service.submit_step(
        conn, wid, "EXT.dtfuzz", "fuzzer", "fuzz 完成：2 个问题可复现")
    wf = wf_service.get_workflow(conn, wid)
    f1 = next(s for s in wf["steps"] if s["step_id"] == "F1")
    prompt = wf_service.step_prompt(wf, f1, {"title": "t", "description": "d"})
    assert "上游结果" in prompt and "fuzz 完成" in prompt

    started.clear()
    wf_service.advance(
        conn, wid,
        starter=lambda r, t, p, s: started.append(s["step_id"]) or {"id": "run-f1"})
    assert started == ["F1"]
    wf_service.submit_step(conn, wid, "F1", "lead", "最终报告已更新")
    assert wf_service.get_workflow(conn, wid)["status"] == "done"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='T1'").fetchone()["status"] == "in_review"


def test_external_node_restart_downstream_resets_dependents(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
    conn.execute(
        "UPDATE workflow_steps SET rework_count=2 "
        "WHERE workflow_id=? AND step_id='P1.auth.decide'", (wid,))
    conn.commit()
    intent = _external_intent(
        target_slice_id="auth", restart_scope="downstream")
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True, impact
    restart = set(impact["diff"]["restart_steps"])
    assert "P1.auth.static" in restart
    assert "P1.pay.static" in restart
    assert "F1" in restart

    draft = board_service.create_revision_draft(conn, wid, intent, "补 fuzz", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    steps = _steps(conn, wid)
    assert steps["P1.auth.static"]["status"] == "pending"
    assert steps["P1.pay.static"]["status"] == "pending"
    assert steps["F1"]["status"] == "pending"
    assert "EXT.dtfuzz" in steps["P1.auth.static"]["depends_on"]
    # 重跑 decision 时预算归零，避免第一次 reject 就再次耗尽。
    assert steps["P1.auth.decide"]["rework_count"] == 0


def test_external_node_restart_scope_target_keeps_downstream(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
    conn.commit()
    intent = _external_intent(target_slice_id="auth", restart_scope="target")
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["safe"] is True, impact
    restart = set(impact["diff"]["restart_steps"])
    assert "P1.auth.static" in restart
    assert "P1.pay.static" not in restart
    assert "F1" not in restart

    draft = board_service.create_revision_draft(conn, wid, intent, "补 fuzz", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    steps = _steps(conn, wid)
    assert steps["P1.auth.static"]["status"] == "pending"
    assert steps["P1.pay.static"]["status"] == "approved"
    assert steps["F1"]["status"] == "approved"


def test_external_node_rejects_running_target(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='running' "
        "WHERE workflow_id=? AND step_id='P1.pay.static'", (wid,))
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, _external_intent())
    assert impact["safe"] is False
    assert impact["classification"] == "requires_restart"
    assert any(b["step_id"] == "P1.pay.static" for b in impact["blocked_by"])
    with pytest.raises(wf_service.RevisionConflict):
        board_service.create_revision_draft(
            conn, wid, _external_intent(), "x", "cindy")
    assert "EXT.dtfuzz" not in _steps(conn, wid)


def test_external_node_rejects_done_task(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute("UPDATE tasks SET status='done' WHERE id='T1'")
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, _external_intent())
    assert impact["classification"] == "invalid"


def test_external_node_requires_valid_role(revision_env):
    conn, wid = revision_env
    impact = wf_service.preview_revision(
        conn, wid, _external_intent(role="no-such-role"))
    assert impact["classification"] == "invalid"


def test_external_nodes_cannot_combine_with_slice_changes(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    intent = _external_intent()
    intent["add_slices"] = [
        {"id": "extra", "title": "extra", "execute_steps": [{"id": "static"}]}]
    impact = wf_service.preview_revision(conn, wid, intent)
    assert impact["classification"] == "invalid"


def test_external_deps_cannot_be_set_directly(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    impact = wf_service.preview_revision(conn, wid, {
        "update_slices": [{"id": "pay", "patch": {
            "external_deps": ["EXT.evil"]}}]})
    assert impact["classification"] == "invalid"
    impact = wf_service.preview_revision(conn, wid, {
        "add_slices": [{"id": "extra", "title": "extra",
                        "external_deps": ["EXT.evil"],
                        "execute_steps": [{"id": "static"}]}]})
    assert impact["classification"] == "invalid"


def test_slice_linked_to_external_node_cannot_be_removed(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    draft = board_service.create_revision_draft(
        conn, wid, _external_intent(), "补 fuzz", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    impact = wf_service.preview_revision(
        conn, wid, {"remove_slices": ["pay"]})
    assert impact["classification"] == "invalid"
    assert "external" in impact["summary"]


def test_revision_reaper_rejects_drafts_for_terminated_workflow(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    draft = board_service.create_revision_draft(
        conn, wid, _external_intent(), "补 fuzz", "cindy")
    conn.execute("UPDATE workflows SET status='terminated' WHERE id=?", (wid,))
    conn.commit()
    assert board_service.reap_stale_revision_drafts(conn, task_id="T1") == 1
    row = conn.execute(
        "SELECT status FROM board_drafts WHERE id=?", (draft["id"],)).fetchone()
    assert row["status"] == "rejected"


def test_revision_reaper_rejects_draft_for_terminal_task(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    draft = board_service.create_revision_draft(
        conn, wid, _external_intent(), "x", "cindy")
    conn.execute("UPDATE tasks SET status='done' WHERE id='T1'")
    conn.commit()
    assert board_service.reap_stale_revision_drafts(conn, task_id="T1") == 1
    row = conn.execute(
        "SELECT status FROM board_drafts WHERE id=?", (draft["id"],)).fetchone()
    assert row["status"] == "rejected"


def test_revision_reaper_keeps_external_draft_for_done_workflow(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
    conn.execute("UPDATE workflows SET status='done' WHERE id=?", (wid,))
    conn.commit()
    draft = board_service.create_revision_draft(
        conn, wid,
        _external_intent(target_slice_id="auth", restart_scope="target"),
        "补 fuzz", "cindy")
    assert board_service.reap_stale_revision_drafts(conn, task_id="T1") == 0
    row = conn.execute(
        "SELECT status FROM board_drafts WHERE id=?", (draft["id"],)).fetchone()
    assert row["status"] == "pending"


def test_create_revision_draft_rejects_when_lifecycle_busy(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    lease = leases_service.acquire(
        conn, "workflow", "lifecycle:T1", holder_type="system",
        holder="workflow", commit=True)
    try:
        with pytest.raises(ConflictError) as exc:
            board_service.create_revision_draft(
                conn, wid, _external_intent(), "x", "cindy")
        assert "workflow_busy" in str(exc.value)
    finally:
        leases_service.release(conn, lease["id"])


def test_revision_reaper_expires_old_draft(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    draft = board_service.create_revision_draft(
        conn, wid, _external_intent(), "x", "cindy")
    conn.execute(
        "UPDATE board_drafts SET created_at='2000-01-01T00:00:00+00:00' "
        "WHERE id=?", (draft["id"],))
    conn.commit()
    assert board_service.reap_stale_revision_drafts(conn, task_id="T1") == 1
    row = conn.execute(
        "SELECT status FROM board_drafts WHERE id=?", (draft["id"],)).fetchone()
    assert row["status"] == "rejected"


def test_patch_steps_preserves_external_dependency(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    ext = _external_intent(
        target_slice_id=None, target_step_id="F1", restart_scope="target")
    draft = board_service.create_revision_draft(conn, wid, ext, "x", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    assert "EXT.dtfuzz" in _steps(conn, wid)["F1"]["depends_on"]

    patch = {"patch_steps": [{
        "step_id": "F1", "fields": {"depends_on": ["P1.pay.decide"]}}]}
    impact = wf_service.preview_revision(conn, wid, patch)
    assert impact["safe"] is True, impact
    wf_service.apply_revision(conn, wid, patch, actor="human")
    deps = _steps(conn, wid)["F1"]["depends_on"]
    assert "EXT.dtfuzz" in deps
    assert "P1.pay.decide" in deps


def test_external_node_rejects_dispatched_target(revision_env):
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='pending', run_id='r-pending' "
        "WHERE workflow_id=? AND step_id='P1.pay.static'", (wid,))
    conn.commit()
    impact = wf_service.preview_revision(conn, wid, _external_intent())
    assert impact["safe"] is False
    assert impact["classification"] == "requires_restart"
    assert any(
        b["step_id"] == "P1.pay.static" for b in impact["blocked_by"])


def test_finalize_and_external_apply_do_not_interleave_badly(
        revision_env, monkeypatch):
    """A 已进入 BEGIN IMMEDIATE 时启动 B 的 external apply；B 必须等 A 收口后
    再重开，最终不能出现 done + pending external step。"""
    conn, wid = revision_env
    _add_role(conn)
    conn.execute(
        "UPDATE workflow_steps SET status='approved' WHERE workflow_id=?", (wid,))
    conn.commit()
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    intent = _external_intent(
        target_slice_id=None, target_step_id="F1", restart_scope="target")

    original_get = wf_service.get_workflow
    main_calls = 0
    thread_result: dict = {}

    def worker():
        other = connect(db_path)
        try:
            wf_service.apply_revision(other, wid, intent, actor="human")
        except BaseException as exc:  # noqa: BLE001
            thread_result["error"] = exc
        finally:
            other.close()

    thread = threading.Thread(target=worker, name="ext-apply")

    def hooked(c, w):
        nonlocal main_calls
        result = original_get(c, w)
        if threading.current_thread() is threading.main_thread():
            main_calls += 1
            if main_calls == 2:
                # A 已持锁和 BEGIN IMMEDIATE；此时启动 B。
                thread.start()
        return result

    monkeypatch.setattr(wf_service, "get_workflow", hooked)
    wf_service._finalize_if_done(conn, wid)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "error" not in thread_result, thread_result.get("error")

    wf = wf_service.get_workflow(conn, wid)
    steps = {s["step_id"]: s for s in wf["steps"]}
    assert wf["status"] == "running"
    assert steps["EXT.dtfuzz"]["status"] == "pending"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='T1'").fetchone()["status"] == "in_progress"


def test_external_dep_survives_slice_rebuild(revision_env):
    """后续普通修订重建目标切片时，external_deps 不能丢。"""
    conn, wid = revision_env
    _add_role(conn)
    ext_intent = _external_intent()
    draft = board_service.create_revision_draft(
        conn, wid, ext_intent, "补 fuzz", "cindy")
    board_service.approve(conn, [draft["id"]], by="human")
    assert "EXT.dtfuzz" in _steps(conn, wid)["P1.pay.static"]["depends_on"]

    update = {"update_slices": [
        {"id": "pay", "patch": {"title": "支付补强"}}]}
    impact = wf_service.preview_revision(conn, wid, update)
    assert impact["safe"] is True, impact
    wf_service.apply_revision(conn, wid, update, actor="human")
    steps = _steps(conn, wid)
    assert "EXT.dtfuzz" in steps["P1.pay.static"]["depends_on"]
    assert steps["EXT.dtfuzz"]["status"] == "pending"
