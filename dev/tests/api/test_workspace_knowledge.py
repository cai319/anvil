"""v1.30 频道知识：知识卡 CRUD、审批、authority、收割与幂等。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")

FULL_CONVENTION = {
    "rule": "所有 API 变更必须同步更新 OpenAPI 文档。",
    "scope": "当前频道下的后端任务",
    "rationale": "避免接口漂移，评审时可机械检查。",
    "examples": ["新增 /tasks 字段时更新 openapi.json"],
    "verification": "评审时检查 diff 是否包含文档变更。",
    "exceptions": "纯内部重构、不影响接口签名时可豁免。",
}


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())


async def _workspace(client, *, repo="repo-a", knowledge_repo="kb-a"):
    r = await client.post(
        f"{API}/workspaces",
        json={"name": "main", "repo": repo, "knowledge_repo": knowledge_repo},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _agent_headers(app, role: str = "qa-role", token: str = "kb-agent-token") -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def test_human_creates_approved_convention(client):
    await _seed(client)
    ws = await _workspace(client)
    r = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge",
        json={
            "title": "API 变更必须更新文档",
            "tldr": "所有接口变更随代码更新 OpenAPI。",
            "kind": "convention",
            "fields": FULL_CONVENTION,
            "review_at": "2027-01-01",
        },
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["status"] == "approved"
    assert card["authority"] == "normative"
    assert card["repo"] == "kb-a"
    assert card["completeness"] == 1.0
    assert "## 规则" in card["body_md"]
    assert card["fields"]["rule"].startswith("所有 API")


async def test_agent_proposes_and_cannot_approve(client, app):
    await _seed(client)
    ws = await _workspace(client)
    headers = _agent_headers(app, role="agent-role", token="kb-agent-role-token")
    r = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge",
        json={
            "title": "考虑使用批量提交",
            "tldr": "批量提交可能减少往返。",
            "kind": "decision",
            "fields": {"context": "任务量大", "decision": "观察后决定"},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["status"] == "proposed"
    assert card["origin"] == "agent"

    denied = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{card['id']}/approve",
        json={"expected_version": card["version"]},
        headers=headers,
    )
    assert denied.status_code == 403


async def test_approve_requires_full_fields_and_version(client, app):
    await _seed(client)
    ws = await _workspace(client)
    headers = _agent_headers(app)
    r = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge",
        json={
            "title": "不完整的约定",
            "tldr": "缺少示例和验证。",
            "kind": "convention",
            "fields": {"rule": "必须这样", "scope": "当前频道"},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    card = r.json()
    bad = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{card['id']}/approve",
        json={"expected_version": card["version"]},
        headers=human_headers(),
    )
    assert bad.status_code == 409
    assert "missing required field" in bad.text

    stale = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{card['id']}/approve",
        json={"expected_version": card["version"] + 99},
        headers=human_headers(),
    )
    assert stale.status_code == 409


async def test_approved_card_metadata_edit_creates_proposal(client):
    """只改 tags/owner 等元数据也必须生成新提案，不能被去重吞掉。"""
    await _seed(client)
    ws = await _workspace(client)
    original = (
        await client.post(
            f"{API}/workspaces/{ws['id']}/knowledge",
            json={"title": "元数据编辑约定", "tldr": "只改标签也要生成新版本。",
                  "kind": "convention", "fields": FULL_CONVENTION,
                  "source_task_id": "T-META"},
            headers=human_headers(),
        )
    ).json()
    edited = await client.patch(
        f"{API}/workspaces/{ws['id']}/knowledge/{original['id']}",
        json={"tags": ["docs"], "expected_version": original["version"]},
        headers=human_headers(),
    )
    assert edited.status_code == 200, edited.text
    proposal = edited.json()
    assert proposal["id"] != original["id"]
    assert proposal["status"] == "proposed"
    assert proposal["supersedes_id"] == original["id"]
    assert proposal["tags"] == ["docs"]


async def test_rejected_card_requires_content_change(client, app):
    """被驳回的同内容卡不能原样重插；必须改内容后重新提案。"""
    await _seed(client)
    ws = await _workspace(client)
    payload = {"title": "可重建约定", "tldr": "驳回后需修改内容再提案。",
               "kind": "convention", "fields": FULL_CONVENTION,
               "source_task_id": "T-REBUILD"}
    first = (
        await client.post(f"{API}/workspaces/{ws['id']}/knowledge", json=payload,
                          headers=_agent_headers(app))
    ).json()
    rejected = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{first['id']}/reject",
        json={"expected_version": first["version"]}, headers=human_headers())
    assert rejected.status_code == 200, rejected.text
    again = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge", json=payload,
        headers=_agent_headers(app))
    assert again.status_code == 409, again.text

    changed = {**payload, "tldr": "修改后的摘要，允许重新提案。"}
    second = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge", json=changed,
        headers=_agent_headers(app))
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]


async def test_supersedes_must_stay_in_same_workspace(client):
    await _seed(client)
    ws_a = await _workspace(client, repo="repo-a", knowledge_repo="kb-a")
    ws_b = await _workspace(client, repo="repo-b", knowledge_repo="kb-b")
    card_a = (
        await client.post(
            f"{API}/workspaces/{ws_a['id']}/knowledge",
            json={"title": "A 的规范", "tldr": "A 的规范摘要。",
                  "kind": "convention", "fields": FULL_CONVENTION},
            headers=human_headers(),
        )
    ).json()
    r = await client.post(
        f"{API}/workspaces/{ws_b['id']}/knowledge",
        json={"title": "B 的新规范", "tldr": "B 的新规范摘要。",
              "kind": "convention", "fields": FULL_CONVENTION,
              "supersedes_id": card_a["id"]},
        headers=human_headers(),
    )
    assert r.status_code == 409, r.text


async def test_knowledge_route_requires_matching_workspace(client):
    """URL 中的 ws_id 必须与卡片所属 workspace 一致。"""
    await _seed(client)
    ws_a = await _workspace(client, repo="repo-a", knowledge_repo="kb-a")
    ws_b = await _workspace(client, repo="repo-b", knowledge_repo="kb-b")
    card_b = (
        await client.post(
            f"{API}/workspaces/{ws_b['id']}/knowledge",
            json={"title": "B 的卡片", "tldr": "只在 B 中可见。",
                  "kind": "convention", "fields": FULL_CONVENTION},
            headers=human_headers(),
        )
    ).json()

    deleted = await client.delete(
        f"{API}/workspaces/{ws_a['id']}/knowledge/{card_b['id']}",
        headers=human_headers())
    assert deleted.status_code == 404
    still_there = await client.get(
        f"{API}/workspaces/{ws_b['id']}/knowledge?status=approved",
        headers=human_headers())
    assert card_b["id"] in {c["id"] for c in still_there.json()}


async def test_reharvest_returns_approved_successor(client, app):
    """旧卡已被新版本取代后，重复收割应返回新的已批准卡。"""
    await _seed(client)
    ws = await _workspace(client)
    agent = _agent_headers(app, token="kb-chain-token")
    payload = {"title": "链式修订约定", "tldr": "同内容新版本替代旧卡。",
               "kind": "convention", "fields": FULL_CONVENTION,
               "source_task_id": "T-CHAIN"}
    first = (await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge", json=payload,
        headers=agent)).json()
    agree1 = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{first['id']}/approve",
        json={"expected_version": first["version"]}, headers=human_headers())
    assert agree1.status_code == 200, agree1.text

    proposal = (await client.patch(
        f"{API}/workspaces/{ws['id']}/knowledge/{first['id']}",
        json={"tags": ["docs"], "expected_version": agree1.json()["version"]},
        headers=human_headers())).json()
    agree2 = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{proposal['id']}/approve",
        json={"expected_version": proposal["version"]}, headers=human_headers())
    assert agree2.status_code == 200, agree2.text

    again = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge", json=payload, headers=agent)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == proposal["id"]
    assert again.json()["status"] == "approved"


async def test_harvest_from_done_task_is_idempotent(client):
    await _seed(client)
    ws = await _workspace(client)
    created = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-HARVEST", workspace_id=ws["id"]),
        headers=human_headers(),
    )
    assert created.status_code == 201
    assert (await client.post(
        f"{API}/tasks/T-HARVEST/claim", json={}, headers=auth_headers()
    )).status_code == 200
    assert (await client.post(
        f"{API}/tasks/T-HARVEST/submit", json={"summary": "done"}, headers=auth_headers()
    )).status_code == 200
    assert (await client.post(
        f"{API}/tasks/T-HARVEST/review", json={"approve": True}, headers=human_headers()
    )).status_code == 200

    first = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/harvest",
        json={"task_id": "T-HARVEST"},
        headers=human_headers(),
    )
    assert first.status_code == 201, first.text
    card = first.json()
    assert card["source_task_id"] == "T-HARVEST"
    assert card["kind"] == "insight"
    assert card["status"] == "approved"

    second = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/harvest",
        json={"task_id": "T-HARVEST"},
        headers=human_headers(),
    )
    assert second.status_code == 201
    assert second.json()["id"] == card["id"]


async def test_approved_card_is_immutable_and_edit_creates_proposal(client):
    await _seed(client)
    ws = await _workspace(client)
    original = (
        await client.post(
            f"{API}/workspaces/{ws['id']}/knowledge",
            json={
                "title": "API 变更必须更新文档", "tldr": "所有接口变更随代码更新 OpenAPI。",
                "kind": "convention", "fields": FULL_CONVENTION,
            },
            headers=human_headers(),
        )
    ).json()
    assert original["status"] == "approved"

    stale = await client.patch(
        f"{API}/workspaces/{ws['id']}/knowledge/{original['id']}",
        json={"tldr": "新的摘要", "expected_version": original["version"] + 1},
        headers=human_headers(),
    )
    assert stale.status_code == 409

    edited = await client.patch(
        f"{API}/workspaces/{ws['id']}/knowledge/{original['id']}",
        json={"tldr": "新的摘要", "expected_version": original["version"]},
        headers=human_headers(),
    )
    assert edited.status_code == 200, edited.text
    proposal = edited.json()
    assert proposal["id"] != original["id"]
    assert proposal["status"] == "proposed"
    assert proposal["supersedes_id"] == original["id"]
    assert proposal["version"] == 1

    unchanged = (
        await client.get(f"{API}/workspaces/{ws['id']}/knowledge?status=approved",
                         headers=human_headers())
    ).json()
    assert [c["id"] for c in unchanged] == [original["id"]]
    assert unchanged[0]["tldr"] == "所有接口变更随代码更新 OpenAPI。"

    approved = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{proposal['id']}/approve",
        json={"expected_version": proposal["version"]},
        headers=human_headers(),
    )
    assert approved.status_code == 200, approved.text
    deprecated = (
        await client.get(f"{API}/workspaces/{ws['id']}/knowledge?status=deprecated",
                         headers=human_headers())
    ).json()
    assert [c["id"] for c in deprecated] == [original["id"]]


async def test_conflicts_block_injection_and_can_be_resolved(client, app, monkeypatch):
    await _seed(client)
    ws = await _workspace(client)
    first = (
        await client.post(
            f"{API}/workspaces/{ws['id']}/knowledge",
            json={
                "title": "禁止直接 push 到 main 分支",
                "tldr": "禁止直接 push 到 main 分支，必须走 PR。",
                "kind": "convention", "fields": FULL_CONVENTION,
            },
            headers=human_headers(),
        )
    ).json()
    second = (
        await client.post(
            f"{API}/workspaces/{ws['id']}/knowledge",
            json={
                "title": "必须直接 push 到 main 分支",
                "tldr": "必须直接 push 到 main 分支，禁止走 PR。",
                "kind": "convention", "fields": FULL_CONVENTION,
            },
            headers=human_headers(),
        )
    ).json()
    assert second["conflicts"] == [first["id"]]
    listed = (
        await client.get(f"{API}/workspaces/{ws['id']}/knowledge",
                          headers=human_headers())
    ).json()
    newer = next(c for c in listed if c["id"] == second["id"])
    older = next(c for c in listed if c["id"] == first["id"])
    assert newer["conflicts"] == [first["id"]]
    assert older["conflicts"] == [second["id"]]

    task = (
        await client.post(
            f"{API}/tasks",
            json=task_payload(id_="T-CONFLICT", workspace_id=ws["id"]),
            headers=human_headers(),
        )
    ).json()
    from anvil import config, db
    from anvil.services import workspace_knowledge
    monkeypatch.setattr(config, "CHANNEL_KNOWLEDGE_ENABLED", True)
    conn = db.open_conn(app.state.db_path)
    try:
        assert "push 到 main" not in workspace_knowledge.render_for_task(conn, task)
    finally:
        conn.close()

    resolved = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{second['id']}/resolve-conflicts",
        json={}, headers=human_headers(),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["conflicts"] == []
    remaining = (
        await client.get(f"{API}/workspaces/{ws['id']}/knowledge",
                          headers=human_headers())
    ).json()
    assert all(not c["conflicts"] for c in remaining)


async def test_review_candidates_report_aging_signals(client):
    await _seed(client)
    ws = await _workspace(client)
    async def make(title: str, **extra):
        r = await client.post(
            f"{API}/workspaces/{ws['id']}/knowledge",
            json={
                "title": title, "tldr": f"{title} 的独立摘要",
                "kind": "convention",
                "fields": {
                    "rule": f"{title}的具体规则",
                    "scope": f"{title}适用范围",
                    "rationale": f"{title}的理由",
                    "examples": [f"{title}示例"],
                    "verification": f"{title}验证方式",
                    "exceptions": f"{title}例外情况",
                },
                **extra,
            },
            headers=human_headers(),
        )
        assert r.status_code == 201, r.text
        return r.json()

    await make("复核已到期", review_at="2000-01-01")
    await make("已经过期", expires_at="2000-01-01T00:00:00+00:00")
    healthy = await make("正常约定")

    review = (
        await client.get(f"{API}/workspaces/{ws['id']}/knowledge/review",
                          headers=human_headers())
    )
    assert review.status_code == 200, review.text
    body = review.json()
    reasons = {item["card"]["title"]: item["reasons"] for item in body["items"]}
    assert "review_due" in reasons["复核已到期"]
    assert "expired" in reasons["已经过期"]
    assert "正常约定" not in reasons
    assert body["counts"]["review_due"] >= 1
    assert body["counts"]["expired"] >= 1
    assert healthy["status"] == "approved"


async def test_approve_with_same_version_only_succeeds_once(client, app):
    await _seed(client)
    ws = await _workspace(client)
    headers = _agent_headers(app, role="agent-role", token="kb-concurrent-token")
    card = (
        await client.post(
            f"{API}/workspaces/{ws['id']}/knowledge",
            json={
                "title": "并发审批约定", "tldr": "同一版本只能审批一次。",
                "kind": "convention", "fields": FULL_CONVENTION,
            },
            headers=headers,
        )
    ).json()
    first = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{card['id']}/approve",
        json={"expected_version": card["version"]}, headers=human_headers(),
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge/{card['id']}/approve",
        json={"expected_version": card["version"]}, headers=human_headers(),
    )
    assert second.status_code == 409
    assert second.json()["detail"]


async def test_card_template_and_validate_endpoints(client):
    template = await client.get(
        f"{API}/knowledge/card/template?kind=convention", headers=human_headers())
    assert template.status_code == 200, template.text
    body = template.json()
    assert body["template_version"] == 1
    assert body["kinds"]["convention"]["required_fields"][0] == "rule"
    assert any(f["key"] == "examples" for f in body["kinds"]["convention"]["fields"])

    bad = await client.post(
        f"{API}/knowledge/card/validate",
        json={"kind": "convention", "fields": {"rule": "必须这样"},
              "for_approval": True},
        headers=human_headers(),
    )
    assert bad.status_code == 200
    assert any("missing required field" in err for err in bad.json()["errors"])

    good = await client.post(
        f"{API}/knowledge/card/validate",
        json={"kind": "convention", "fields": FULL_CONVENTION, "for_approval": True},
        headers=human_headers(),
    )
    assert good.status_code == 200
    assert good.json()["errors"] == []
    assert good.json()["completeness"] == 1.0


async def test_list_filters(client):
    await _seed(client)
    ws = await _workspace(client)
    await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge",
        json={
            "title": "约定一", "tldr": "t", "kind": "convention", "fields": FULL_CONVENTION,
        },
        headers=human_headers(),
    )
    await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge",
        json={
            "title": "决策一", "tldr": "t", "kind": "decision",
            "fields": {
                "context": "c", "decision": "d", "alternatives": ["a"], "consequences": "q",
            },
        },
        headers=human_headers(),
    )
    conventions = (
        await client.get(
            f"{API}/workspaces/{ws['id']}/knowledge?kind=convention", headers=human_headers()
        )
    ).json()
    assert [c["title"] for c in conventions] == ["约定一"]
    approved = (
        await client.get(
            f"{API}/workspaces/{ws['id']}/knowledge?status=approved", headers=human_headers()
        )
    ).json()
    assert {c["kind"] for c in approved} == {"convention", "decision"}
