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
