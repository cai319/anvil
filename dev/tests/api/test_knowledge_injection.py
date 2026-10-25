"""v1.30 频道/任务知识注入：绑定频道约束 vs 只引用 repo 参考。"""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.services import task_knowledge, workspace_knowledge

from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload

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
    await client.post(f"{API}/roles", json=role_payload())


async def _workspace(client):
    r = await client.post(
        f"{API}/workspaces",
        json={"name": "main", "repo": "repo-a", "knowledge_repo": "kb-a"},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _card(client, ws_id: str, *, kind: str, title: str, fields: dict,
                expires_at: str | None = None):
    r = await client.post(
        f"{API}/workspaces/{ws_id}/knowledge",
        json={
            "title": title, "tldr": f"{title} 的摘要", "kind": kind,
            "fields": fields, "expires_at": expires_at,
        },
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _render(app, task: dict, monkeypatch) -> str:
    monkeypatch.setattr(config, "CHANNEL_KNOWLEDGE_ENABLED", True)
    conn = db.open_conn(app.state.db_path)
    try:
        return workspace_knowledge.render_for_task(conn, task)
    finally:
        conn.close()


async def test_bound_channel_convention_is_normative(client, app, monkeypatch):
    await _seed(client)
    ws = await _workspace(client)
    await _card(client, ws["id"], kind="convention", title="API 文档约定", fields=CONVENTION)
    await _card(
        client, ws["id"], kind="decision", title="选择 SQLite WAL",
        fields={
            "context": "单机部署", "decision": "使用 WAL",
            "alternatives": ["Postgres"], "consequences": "简单、单机",
        },
    )
    task = (
        await client.post(
            f"{API}/tasks",
            json=task_payload(id_="T-BOUND", workspace_id=ws["id"], repo="other-repo"),
            headers=human_headers(),
        )
    ).json()
    text = _render(app, task, monkeypatch)
    assert "- [约束（必须遵守）] API 文档约定" in text
    assert "- [参考] 选择 SQLite WAL" in text
    assert text.index("API 文档约定") < text.index("选择 SQLite WAL")


async def test_unbound_repo_reference_only(client, app, monkeypatch):
    await _seed(client)
    ws = await _workspace(client)
    await _card(client, ws["id"], kind="convention", title="API 文档约定", fields=CONVENTION)
    task = (
        await client.post(
            f"{API}/tasks",
            json=task_payload(id_="T-REF", repo="kb-a"),
            headers=human_headers(),
        )
    ).json()
    text = _render(app, task, monkeypatch)
    assert "- [参考] API 文档约定" in text
    assert "- [约束（必须遵守）] API 文档约定" not in text


async def test_expired_channel_knowledge_is_excluded(client, app, monkeypatch):
    await _seed(client)
    ws = await _workspace(client)
    await _card(
        client, ws["id"], kind="convention", title="过期约定", fields=CONVENTION,
        expires_at="2000-01-01T00:00:00+00:00",
    )
    task = (
        await client.post(
            f"{API}/tasks",
            json=task_payload(id_="T-EXPIRED", workspace_id=ws["id"]),
            headers=human_headers(),
        )
    ).json()
    assert "过期约定" not in _render(app, task, monkeypatch)


async def test_task_shared_knowledge_marks_subtask(client, app, monkeypatch):
    await _seed(client)
    await client.post(
        f"{API}/tasks", json=task_payload(id_="T-P"), headers=human_headers()
    )
    await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-C", parent_id="T-P", assignees=["qa-role"]),
        headers=human_headers(),
    )
    created = await client.post(
        f"{API}/tasks/T-C/knowledge",
        json={
            "title": "子任务发现", "tldr": "A 方案可行", "kind": "task_finding",
            "fields": {
                "task_context": "子任务", "finding": "A 方案可行",
                "verification": "测试通过", "reuse_scope": "当前任务",
            },
        },
        headers=auth_headers(),
    )
    assert created.status_code == 201, created.text
    monkeypatch.setattr(config, "TASK_KNOWLEDGE_ENABLED", True)
    conn = db.open_conn(app.state.db_path)
    try:
        text = task_knowledge.render_for_task(conn, "T-P")
    finally:
        conn.close()
    assert "任务共享知识" in text
    assert "来自子任务 T-C" in text
