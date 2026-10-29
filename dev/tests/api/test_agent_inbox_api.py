"""Agent Inbox API：check 只读、commit 推进、定向过滤（只收 @自己 的 mention）。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, task_payload  # noqa: E402


async def test_me_inbox_check_and_commit(client):
    await client.post(f"{API}/tasks", json=task_payload(id="T-IB"), headers=auth_headers())
    await client.post(f"{API}/tasks/T-IB/comment", json={"body": "广播：接口更新"},
                      headers=auth_headers("pm"))
    await client.post(f"{API}/tasks/T-IB/mention",
                      json={"target": "developer", "message": "@dev 请确认"},
                      headers=auth_headers("pm"))
    await client.post(f"{API}/tasks/T-IB/mention",
                      json={"target": "tester", "message": "@tester 与你无关"},
                      headers=auth_headers("pm"))

    r = await client.get(f"{API}/me/inbox?task_id=T-IB",
                         headers=auth_headers("developer"))
    assert r.status_code == 200
    body = r.json()
    texts = [i["text"] for i in body["items"]]
    assert any("广播：接口更新" in t for t in texts)
    assert any("请确认" in t for t in texts)
    assert not any("与你无关" in t for t in texts)  # 定向过滤
    assert body["unread"] == 2

    # check 不推进 cursor
    again = await client.get(f"{API}/me/inbox?task_id=T-IB", headers=auth_headers("developer"))
    assert again.json()["unread"] == 2

    up_to = body["items"][-1]["id"]
    c = await client.post(f"{API}/me/inbox/commit",
                          json={"task_id": "T-IB", "up_to": up_to},
                          headers=auth_headers("developer"))
    assert c.status_code == 200 and c.json()["cursor"] == up_to
    after = await client.get(f"{API}/me/inbox?task_id=T-IB", headers=auth_headers("developer"))
    assert after.json()["unread"] == 0


async def test_inbox_resolves_task_from_active_run(client, app):
    await client.post(f"{API}/tasks", json=task_payload(id="T-IB2", assignees=["developer"]),
                      headers=auth_headers())
    await client.post(f"{API}/tasks/T-IB2/comment", json={"body": "有更新"},
                      headers=auth_headers("pm"))
    # 不带 task_id：按角色活跃 run 解析（Agent 的常驻会话里 ANVIL_TASK_ID 可能为空）
    r = await client.get(f"{API}/runs?task_id=T-IB2", headers=human_headers())
    assert r.status_code == 200
    # 直接插入一个挂起 run，模拟角色的常驻会话
    from anvil.db import open_conn
    conn = open_conn(app.state.db_path)
    conn.execute("INSERT INTO runs (id, role_slug, task_id, status, started_at) "
                 "VALUES ('R-IB2','developer','T-IB2','suspended',?)", ("2026-01-01T00:00:00+00:00",))
    conn.commit(); conn.close()
    r = await client.get(f"{API}/me/inbox", headers=auth_headers("developer"))
    assert r.status_code == 200
    assert r.json()["task_id"] == "T-IB2"
