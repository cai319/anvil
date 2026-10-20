"""Held Draft（简化）：Agent 基于旧版本发送时返回 409 + 新内容；force 可越过。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from tests.conftest import API, auth_headers, task_payload  # noqa: E402


async def test_held_draft_blocks_and_allows_force(client, app):
    await client.post(f"{API}/tasks", json=task_payload(id="T-HD"), headers=auth_headers())

    # 先有别人的一条消息
    await client.post(f"{API}/tasks/T-HD/comment", json={"body": "设计已更新"},
                      headers=auth_headers("pm"))
    conn = open_conn(app.state.db_path)
    latest = conn.execute("SELECT MAX(id) AS m FROM activities WHERE task_id='T-HD'").fetchone()["m"]
    conn.close()

    # Agent 基于旧版本（expect=0）发送 → held
    r = await client.post(f"{API}/tasks/T-HD/comment",
                          json={"body": "我基于旧版本", "expect_activity_id": 0},
                          headers=auth_headers("developer"))
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "held"
    assert body["latest_activity_id"] == latest
    assert any("设计已更新" in i["text"] for i in body["items"])

    # 基于最新版本发送 → 通过
    r = await client.post(f"{API}/tasks/T-HD/comment",
                          json={"body": "我看过最新版本", "expect_activity_id": latest},
                          headers=auth_headers("developer"))
    assert r.status_code == 200

    # 知情越过
    r = await client.post(f"{API}/tasks/T-HD/comment",
                          json={"body": "我知道有更新", "expect_activity_id": 0, "force": True},
                          headers=auth_headers("developer"))
    assert r.status_code == 200
