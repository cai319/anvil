"""Cindy 角色建议：审批创建/绑定/重规划，驳回用现有角色重规划。"""
from __future__ import annotations

import pytest

from anvil import config

pytest.importorskip("anvil.main")
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

SUGGEST = {
    "slug": "security-auditor",
    "display_name": "安全审计员",
    "description": "负责代码安全审计",
    "runtime": {"id": "claude-code"},
    "capabilities": {"security-audit": 0.95},
    "responsibilities": ["审计代码"],
    "system_prompt": "你是安全审计员。",
}


@pytest.fixture
async def seeded(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(id="T-ROLE", assignees=["dev"]),
                      headers=auth_headers())


async def _draft(client, tid: str = "T-ROLE", suggested=None):
    tree = {
        "assessment": "缺少安全审计能力",
        "missing_capabilities": ["security-audit"],
        "suggested_roles": suggested or [SUGGEST],
    }
    r = await client.post(f"{API}/board/drafts", headers=auth_headers("cindy"), json={
        "goal": "需要安全审计", "source_task": tid, "kind": "role_suggestion", "tree": tree,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _fake_planner(monkeypatch):
    calls: list[dict] = []

    def fake(conn, db_path, orchestrator, task_id, planner, assignees=None,
             extra_constraint="", replace_active=False):
        calls.append({
            "task_id": task_id, "planner": planner, "assignees": assignees,
            "extra_constraint": extra_constraint, "replace_active": replace_active,
        })
        return {"id": "run-replan", "role_slug": planner, "task_id": task_id}

    monkeypatch.setattr("anvil.services.planning.start_planner_run", fake)
    return calls


async def test_approve_role_suggestion_binds_without_replan(client, seeded, monkeypatch):
    """自然人单纯批准 = 只落角色；是否继续规划由自然人的具体应答决定。"""
    calls = _fake_planner(monkeypatch)
    draft = await _draft(client)

    r = await client.post(f"{API}/board/drafts/approve",
                          json={"draft_ids": [draft["id"]]}, headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["landed"] == [draft["id"]]

    role = (await client.get(f"{API}/roles/security-auditor", headers=auth_headers())).json()
    assert role["definition"]["capabilities"]["security-audit"] == 0.95
    assert role["definition"]["system_prompt"] == "你是安全审计员。"

    task = (await client.get(f"{API}/tasks/T-ROLE", headers=auth_headers())).json()
    assert set(task["assignees"]) == {"dev", "security-auditor"}
    assert {"dev", "security-auditor"} <= {c["role_slug"] for c in task["role_claims"]}

    assert calls == []  # 批准本身不自动重规划
    timeline = (await client.get(f"{API}/tasks/T-ROLE/timeline",
                                 headers=auth_headers())).json()
    assert not any(
        e["kind"] == "mention"
        and (e.get("payload") or {}).get("target") == "cindy"
        for e in timeline
    )
    # HITL 决策留痕：记录发起请求的客户端特征（浏览器/脚本可区分）。
    audit = (await client.get(f"{API}/audit", params={"action": "board.approve"},
                              headers=auth_headers())).json()
    assert any(a["task_id"] == "T-ROLE" for a in audit)


async def test_reject_role_suggestion_does_not_replan_by_default(client, seeded, monkeypatch):
    calls = _fake_planner(monkeypatch)
    draft = await _draft(client)

    r = await client.post(f"{API}/board/drafts/reject", headers=human_headers(),
                          json={"draft_id": draft["id"], "feedback": "先不加人"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    assert calls == []  # 驳回本身不自动重规划
    assert (await client.get(f"{API}/roles/security-auditor",
                             headers=auth_headers())).status_code == 404


async def test_reject_role_suggestion_without_roles_blocks(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/tasks", json=task_payload(id="T-EMPTY"), headers=auth_headers())
    calls = _fake_planner(monkeypatch)
    draft = await _draft(client, tid="T-EMPTY")

    r = await client.post(f"{API}/board/drafts/reject", headers=human_headers(),
                          json={"draft_id": draft["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] == "no_roles"
    assert calls == []

    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    assert any(i["kind"] == "mention" and "角色建议" in i["title"] for i in inbox["items"])


async def test_existing_role_is_bound_not_overwritten(client, seeded, monkeypatch):
    calls = _fake_planner(monkeypatch)
    r = await client.post(f"{API}/roles", headers=auth_headers(), json=role_payload(
        "existing", description="original", capabilities={"orig": 0.9}))
    assert r.status_code == 201, r.text
    draft = await _draft(client, suggested=[{
        **SUGGEST, "slug": "existing", "display_name": "Existing",
        "capabilities": {"new": 1.0},
    }])

    r = await client.post(f"{API}/board/drafts/approve",
                          json={"draft_ids": [draft["id"]]}, headers=human_headers())
    assert r.status_code == 200, r.text
    role = (await client.get(f"{API}/roles/existing", headers=auth_headers())).json()
    assert role["definition"]["description"] == "original"
    assert "orig" in role["definition"]["capabilities"]
    task = (await client.get(f"{API}/tasks/T-ROLE", headers=auth_headers())).json()
    assert "existing" in task["assignees"]
    assert calls == []  # 仅批准绑定，不自动重规划


async def test_invalid_runtime_keeps_draft_pending(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/tasks", json=task_payload(id="T-RT"), headers=auth_headers())
    draft = await _draft(client, tid="T-RT",
                         suggested=[{**SUGGEST, "runtime": {"id": "no-such-runtime"}}])

    r = await client.post(f"{API}/board/drafts/approve",
                          json={"draft_ids": [draft["id"]]}, headers=human_headers())
    assert r.status_code == 409, r.text
    drafts = (await client.get(f"{API}/board/drafts", params={"source_task": "T-RT"},
                               headers=auth_headers())).json()
    assert drafts[0]["status"] == "pending"
    assert (await client.get(f"{API}/roles/security-auditor",
                             headers=auth_headers())).status_code == 404


async def test_inbox_shows_role_suggestion_meta(client, seeded):
    await _draft(client)
    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    item = next(i for i in inbox["items"] if i["title"].startswith("角色建议"))
    assert item["meta"]["draft_kind"] == "role_suggestion"
    assert item["meta"]["suggested_roles"][0]["slug"] == "security-auditor"


async def test_inbox_workflow_draft_counts_steps(client, seeded):
    r = await client.post(f"{API}/board/drafts", headers=auth_headers("cindy"), json={
        "goal": "wf", "source_task": "T-ROLE", "kind": "workflow",
        "tree": {"steps": [
            {"id": "S1", "title": "a", "role": "dev"},
            {"id": "S2", "title": "b", "role": "dev"},
        ]},
    })
    assert r.status_code == 201, r.text
    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    item = next(i for i in inbox["items"]
                if i["task_id"] == "T-ROLE" and i["title"].startswith("工作流草案"))
    assert item["meta"]["step_count"] == 2


async def test_dispatch_returns_role_suggestion_draft_kind(client, seeded):
    await _draft(client)
    r = await client.post(f"{API}/tasks/T-ROLE/dispatch", headers=human_headers())
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "pending_review"
    assert r.json()["draft_kind"] == "role_suggestion"


async def test_planner_can_bind_existing_roles_without_human(client, tmp_path, monkeypatch):
    """非重大编排：planner 可直接绑定现有角色，无需 HITL 审批。"""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "designer"):
        r = await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
        assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/tasks",
        json={**task_payload(id="T-BIND"), "planner": "pm"},
        headers=auth_headers(),
    )
    assert r.status_code == 201, r.text

    r = await client.post(
        f"{API}/tasks/T-BIND/assign-existing",
        json={"roles": ["pm", "designer"]},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["assignees"]) == {"pm", "designer"}


async def test_non_planner_cannot_bind_roster(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    await client.post(
        f"{API}/tasks", json={**task_payload(id="T-BIND2"), "planner": "pm"},
        headers=auth_headers())

    r = await client.post(
        f"{API}/tasks/T-BIND2/assign-existing",
        json={"roles": ["developer"]},
        headers=auth_headers("developer"),
    )
    assert r.status_code == 403, r.text


async def test_assign_existing_rejects_unknown_role(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("pm"), headers=auth_headers())
    await client.post(
        f"{API}/tasks", json={**task_payload(id="T-BIND3"), "planner": "pm"},
        headers=auth_headers())

    r = await client.post(
        f"{API}/tasks/T-BIND3/assign-existing",
        json={"roles": ["no-such-role"]},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 409, r.text


async def _roster_env(client, tmp_path, monkeypatch, tid: str = "T-ADD"):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("dev", "pm", "designer", "tester"):
        await client.post(f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    await client.post(
        f"{API}/tasks",
        json={**task_payload(id=tid, assignees=["dev"]), "planner": "pm"},
        headers=auth_headers(),
    )


async def test_role_addition_to_existing_roster_creates_proposal(client, tmp_path, monkeypatch):
    """已有 Roster 时 planner 不直接改写，而是自动提交提案 @human。"""
    await _roster_env(client, tmp_path, monkeypatch)

    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["dev", "designer"], "reason": "需要 UI 设计能力"},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending_review"
    assert body["requested_assignees"] == ["designer"]

    task = (await client.get(f"{API}/tasks/T-ADD", headers=auth_headers())).json()
    assert task["assignees"] == ["dev"]
    drafts = (await client.get(f"{API}/board/drafts", params={"source_task": "T-ADD"},
                               headers=auth_headers())).json()
    draft = drafts[0]
    assert draft["kind"] == "role_suggestion" and draft["status"] == "pending"
    assert draft["tree"]["apply_mode"] == "authorize"
    assert draft["tree"]["requested_assignees"] == ["designer"]

    inbox = (await client.get(f"{API}/inbox", headers=human_headers())).json()
    assert any("角色追加提案" in i["title"] for i in inbox["items"])


async def test_approved_role_addition_appends_without_replacing(client, tmp_path, monkeypatch):
    """human 批准后 planner 再 assign：只追加新角色，保留原有 Roster。"""
    await _roster_env(client, tmp_path, monkeypatch)
    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["dev", "designer"]},
        headers=auth_headers("pm"),
    )
    draft_id = r.json()["draft_id"]

    r = await client.post(f"{API}/board/drafts/approve",
                          json={"draft_ids": [draft_id]}, headers=human_headers())
    assert r.status_code == 200, r.text
    task = (await client.get(f"{API}/tasks/T-ADD", headers=auth_headers())).json()
    assert task["assignees"] == ["dev"], "批准只授权，不替 planner 绑定"

    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["dev", "designer"]},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["assignees"]) == {"dev", "designer"}


async def test_rejected_role_addition_feedback_reaches_planner(client, tmp_path, monkeypatch):
    """human 驳回时反馈给 planner；planner 修改后可再次提交新提案。"""
    await _roster_env(client, tmp_path, monkeypatch)
    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["designer"]},
        headers=auth_headers("pm"),
    )
    draft_id = r.json()["draft_id"]
    r = await client.post(f"{API}/board/drafts/reject",
                          json={"draft_id": draft_id, "feedback": "请补充职责范围"},
                          headers=human_headers())
    assert r.status_code == 200, r.text

    inbox = (await client.get(f"{API}/me/inbox", params={"task_id": "T-ADD"},
                              headers=auth_headers("pm"))).json()
    assert any("角色追加提案被驳回" in i["text"] for i in inbox["items"])

    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["designer"], "reason": "已补充职责范围"},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 202, r.text
    assert r.json()["draft_id"] != draft_id
    assert r.json()["status"] == "pending_review"


async def test_authorize_proposal_can_precede_role_creation(client, tmp_path, monkeypatch):
    """允许先提案、批准后由 planner 创建角色，再绑定任务。"""
    await _roster_env(client, tmp_path, monkeypatch)
    tree = {
        "apply_mode": "authorize",
        "rationale": "缺少审计能力",
        "requested_assignees": ["new-auditor"],
        "suggested_roles": [{
            "slug": "new-auditor", "display_name": "新审计员",
            "description": "负责安全审计", "responsibilities": ["审计代码"],
        }],
    }
    r = await client.post(f"{API}/board/drafts", headers=auth_headers("pm"), json={
        "goal": "补充审计角色", "source_task": "T-ADD", "kind": "role_suggestion",
        "tree": tree,
    })
    assert r.status_code == 201, r.text
    draft_id = r.json()["id"]

    r = await client.post(f"{API}/board/drafts/approve",
                          json={"draft_ids": [draft_id]}, headers=human_headers())
    assert r.status_code == 200, r.text
    assert (await client.get(f"{API}/roles/new-auditor",
                             headers=auth_headers())).status_code == 404

    r = await client.post(f"{API}/roles",
                          json={"slug": "new-auditor", "display_name": "新审计员"},
                          headers=human_headers())
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["dev", "new-auditor"]},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["assignees"]) == {"dev", "new-auditor"}


async def test_assign_existing_existing_role_is_noop(client, tmp_path, monkeypatch):
    """重复绑定已有角色不产生提案、不改 Roster。"""
    await _roster_env(client, tmp_path, monkeypatch)
    r = await client.post(
        f"{API}/tasks/T-ADD/assign-existing",
        json={"roles": ["dev"]},
        headers=auth_headers("pm"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignees"] == ["dev"]
    drafts = (await client.get(f"{API}/board/drafts", params={"source_task": "T-ADD"},
                               headers=auth_headers())).json()
    assert drafts == []
