"""知识库统一作用域：跨频道/跨任务聚合列表、筛选、分页与权限。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import API, human_headers, role_payload, runtime_payload, task_payload

pytestmark = pytest.mark.usefixtures("client")

CONVENTION = {
    "rule": "所有 API 变更必须同步更新 OpenAPI。",
    "scope": "当前频道",
    "rationale": "避免接口漂移。",
    "examples": ["新增字段时更新 openapi.json"],
    "verification": "评审检查 diff。",
    "exceptions": "纯内部重构可豁免。",
}


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    for slug in ("agent-role", "qa-role"):
        await client.post(f"{API}/roles", json=role_payload(slug=slug))


def _agent_headers(app, role: str = "agent-role", token: str = "library-agent") -> dict:
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


async def _workspace(client, name: str) -> dict:
    r = await client.post(
        f"{API}/workspaces",
        json={"name": name, "repo": f"repo-{name}", "knowledge_repo": f"kb-{name}"},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _card(client, ws_id: str, title: str, *, kind: str = "convention") -> dict:
    fields = CONVENTION if kind == "convention" else {
        "observation": title, "evidence": "测试", "lesson": title,
        "applicability": "测试", "counterexamples": "暂无",
    }
    r = await client.post(
        f"{API}/workspaces/{ws_id}/knowledge",
        json={"title": title, "tldr": f"{title} 摘要", "kind": kind, "fields": fields},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _task(client, tid: str, **kw) -> dict:
    r = await client.post(
        f"{API}/tasks", json=task_payload(id_=tid, **kw), headers=human_headers()
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _task_card(client, tid: str, title: str) -> dict:
    r = await client.post(
        f"{API}/tasks/{tid}/knowledge",
        json={
            "title": title, "tldr": f"{title} 摘要", "kind": "task_finding",
            "fields": {
                "task_context": "测试任务", "finding": title,
                "verification": "测试", "reuse_scope": "当前任务",
            },
        },
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_cards_aggregate_filters_and_pagination(client, app):
    await _seed(client)
    ws_a = await _workspace(client, "alpha")
    ws_b = await _workspace(client, "beta")
    await _card(client, ws_a["id"], "接口约定")
    await client.post(
        f"{API}/workspaces/{ws_b['id']}/knowledge",
        json={
            "title": "覆盖率 100% 的约定", "tldr": "包含百分号的标题",
            "kind": "insight",
            "fields": {
                "observation": "覆盖率", "evidence": "100%", "lesson": "保持覆盖",
                "applicability": "测试", "counterexamples": "暂无",
            },
        },
        headers=human_headers(),
    )
    agent = await client.post(
        f"{API}/workspaces/{ws_b['id']}/knowledge",
        json={
            "title": "待批准提案", "tldr": "Agent 提议", "kind": "insight",
            "fields": {
                "observation": "观察", "evidence": "证据", "lesson": "结论",
                "applicability": "测试", "counterexamples": "暂无",
            },
        },
        headers=_agent_headers(app),
    )
    assert agent.status_code == 201, agent.text
    assert agent.json()["status"] == "proposed"

    r = await client.get(f"{API}/knowledge/cards", headers=human_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert {item["workspace_name"] for item in body["items"]} == {"alpha", "beta"}
    assert body["items"][0]["status"] == "proposed"  # proposed 排前

    proposed = await client.get(
        f"{API}/knowledge/cards?status=proposed", headers=human_headers()
    )
    assert [x["title"] for x in proposed.json()["items"]] == ["待批准提案"]

    by_ws = await client.get(
        f"{API}/knowledge/cards?workspace_id={ws_a['id']}", headers=human_headers()
    )
    assert [x["title"] for x in by_ws.json()["items"]] == ["接口约定"]

    by_kind = await client.get(
        f"{API}/knowledge/cards?kind=insight", headers=human_headers()
    )
    assert by_kind.json()["total"] == 2

    chinese = await client.get(
        f"{API}/knowledge/cards?q=%E6%8E%A5%E5%8F%A3", headers=human_headers()
    )
    assert [x["title"] for x in chinese.json()["items"]] == ["接口约定"]

    percent = await client.get(
        f"{API}/knowledge/cards?q=%25", headers=human_headers()
    )
    assert [x["title"] for x in percent.json()["items"]] == ["覆盖率 100% 的约定"]

    first = await client.get(
        f"{API}/knowledge/cards?limit=1", headers=human_headers()
    )
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["total"] == 3
    assert first_body["has_more"] is True
    second = await client.get(
        f"{API}/knowledge/cards?limit=1&offset=1", headers=human_headers()
    )
    assert second.json()["items"][0]["id"] != first_body["items"][0]["id"]

    invalid = await client.get(
        f"{API}/knowledge/cards?status=bogus", headers=human_headers()
    )
    assert invalid.status_code == 422


async def test_cards_requires_human(client, app):
    await _seed(client)
    r = await client.get(f"{API}/knowledge/cards", headers=_agent_headers(app))
    assert r.status_code == 403


async def test_task_cards_aggregate_and_status(client, app):
    await _seed(client)
    ws = await _workspace(client, "tasks")
    await _task(client, "T-A", workspace_id=ws["id"])
    await _task(client, "T-B")
    await _task_card(client, "T-A", "任务 A 的发现")
    await _task_card(client, "T-B", "未绑定任务的发现")
    await _task(client, "T-DEL")
    await _task_card(client, "T-DEL", "待删除任务的知识")
    deleted = await client.delete(
        f"{API}/tasks/T-DEL?force=true", headers=human_headers()
    )
    assert deleted.status_code == 204, deleted.text

    all_cards = await client.get(
        f"{API}/knowledge/task-cards?status=all", headers=human_headers()
    )
    assert all_cards.status_code == 200, all_cards.text
    body = all_cards.json()
    assert body["total"] == 3
    by_title = {x["title"]: x for x in body["items"]}
    assert by_title["任务 A 的发现"]["workspace_name"] == "tasks"
    assert by_title["任务 A 的发现"]["task_title"] == "M1 冒烟任务"
    assert by_title["待删除任务的知识"]["status"] == "archived"

    active = await client.get(
        f"{API}/knowledge/task-cards?status=active", headers=human_headers()
    )
    assert active.json()["total"] == 2

    archived = await client.get(
        f"{API}/knowledge/task-cards?status=archived", headers=human_headers()
    )
    assert [x["title"] for x in archived.json()["items"]] == ["待删除任务的知识"]

    by_ws = await client.get(
        f"{API}/knowledge/task-cards?workspace_id={ws['id']}", headers=human_headers()
    )
    assert [x["title"] for x in by_ws.json()["items"]] == ["任务 A 的发现"]

    q = await client.get(
        f"{API}/knowledge/task-cards?q=%E6%9C%AA%E7%BB%91%E5%AE%9A", headers=human_headers()
    )
    assert [x["title"] for x in q.json()["items"]] == ["未绑定任务的发现"]


async def test_task_cards_requires_human(client, app):
    await _seed(client)
    r = await client.get(f"{API}/knowledge/task-cards", headers=_agent_headers(app))
    assert r.status_code == 403


async def test_knowledge_stats_cover_usage_and_coverage(client, app):
    await _seed(client)
    ws = await _workspace(client, "metrics")
    used = await _card(client, ws["id"], "被注入过的约定")
    await _card(client, ws["id"], "零命中的约定")
    proposed = await client.post(
        f"{API}/workspaces/{ws['id']}/knowledge",
        json={
            "title": "待批准约定", "tldr": "提案", "kind": "insight",
            "fields": {
                "observation": "观察", "evidence": "证据", "lesson": "结论",
                "applicability": "测试", "counterexamples": "暂无",
            },
        },
        headers=_agent_headers(app),
    )
    assert proposed.status_code == 201, proposed.text
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "UPDATE workspace_knowledge SET hit_count=2, last_hit_at=? WHERE id=?",
            ("2026-10-25T00:00:00+00:00", used["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    await _task(client, "T-METRIC", workspace_id=ws["id"])
    await _task_card(client, "T-METRIC", "任务知识")

    stats = await client.get(f"{API}/knowledge/stats", headers=human_headers())
    assert stats.status_code == 200, stats.text
    body = stats.json()
    totals = body["totals"]
    assert totals["approved"] == 2
    assert totals["used"] == 1
    assert totals["hits"] == 2
    assert totals["zero_hit"] == 1
    assert totals["proposed"] == 1
    assert totals["hit_rate"] == 0.5
    channel = next(c for c in body["channels"] if c["workspace_id"] == ws["id"])
    assert channel["approved"] == 2
    assert channel["hits"] == 2
    assert channel["tasks"] == 1
    assert body["task_knowledge"]["active"] == 1
    assert body["task_knowledge"]["tasks_with_knowledge"] == 1
    assert body["role_knowledge"]["usage_tracked"] is False
    assert "hit_count" in body["note"]

    denied = await client.get(f"{API}/knowledge/stats", headers=_agent_headers(app))
    assert denied.status_code == 403


async def test_delete_channel_unbinds_tasks(client):
    await _seed(client)
    ws = await _workspace(client, "to-delete")
    await _task(client, "T-UNBIND", workspace_id=ws["id"])
    deleted = await client.delete(f"{API}/workspaces/{ws['id']}", headers=human_headers())
    assert deleted.status_code == 204, deleted.text
    task = await client.get(f"{API}/tasks/T-UNBIND", headers=human_headers())
    assert task.status_code == 200
    assert task.json()["workspace_id"] is None
