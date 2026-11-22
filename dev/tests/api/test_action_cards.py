"""Action Cards：Agent 只能 prepare；自然人批准后以人的身份执行。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload, task_payload  # noqa: E402


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("pm"), headers=auth_headers())
    await client.post(f"{API}/tasks", json=task_payload(id="T-ACT"), headers=auth_headers())


async def test_agent_prepares_human_executes(client, seeded):
    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.cancel", "task_id": "T-ACT", "reason": "需求已撤销"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # 收件箱出现操作申请
    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    assert any(i["kind"] == "action" for i in inbox["items"])

    # Agent 不能批准
    assert (await client.post(f"{API}/action-cards/{cid}/approve",
                              headers=auth_headers("pm"))).status_code == 403
    # 自然人批准 → 执行（任务 closed）
    r = await client.post(f"{API}/action-cards/{cid}/approve", headers=human_headers())
    assert r.status_code == 200 and r.json()["status"] == "executed"
    task = (await client.get(f"{API}/tasks/T-ACT", headers=auth_headers())).json()
    assert task["status"] == "closed"
    inbox = (await client.get(f"{API}/inbox", headers=auth_headers())).json()
    assert not any(i["kind"] == "action" for i in inbox["items"])


async def test_action_card_mutations_publish_refresh_signal(
        client, seeded, monkeypatch):
    """动作卡是收件箱待办：prepare/approve 必须广播刷新信号。"""
    from anvil.api import events

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        events, "publish", lambda topic, payload=None: published.append((topic, payload or {})))

    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.cancel", "task_id": "T-ACT", "reason": "取消"})
    assert r.status_code == 201, r.text
    assert any(p.get("kind") == "action_card" and p.get("status") == "pending"
               for topic, p in published if topic == "task"), published

    cid = r.json()["id"]
    assert (await client.post(
        f"{API}/action-cards/{cid}/approve", headers=human_headers())).status_code == 200
    assert any(p.get("kind") == "action_card" and p.get("status") == "executed"
               for topic, p in published if topic == "task"), published


async def test_reject_action_does_not_execute(client, seeded):
    # reopen 只能针对已终止任务；先人工终止。
    closed = await client.post(f"{API}/tasks/T-ACT/cancel", headers=human_headers())
    assert closed.json()["status"] == "closed"
    r = await client.post(f"{API}/action-cards", headers=auth_headers("pm"), json={
        "kind": "task.reopen", "task_id": "T-ACT", "reason": "再开一下"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = await client.post(f"{API}/action-cards/{cid}/reject", headers=human_headers())
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    task = (await client.get(f"{API}/tasks/T-ACT", headers=auth_headers())).json()
    assert task["status"] == "closed"
