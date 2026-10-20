"""L2 · B 多频道契约测试（task #16）。

覆盖 SE 增补 §8：频道 CRUD + workspace_members 多对多绑定 + 作用域隔离 + 完整链路。
接口：
  GET/POST /api/v1/workspaces
  GET/POST /api/v1/workspaces/{id}/members
  PATCH/DELETE /api/v1/workspaces/{id}/members/{role}
  claim 守卫：角色须是该任务频道成员且 access ∈ {write,admin}
B 多频道（task #15）未实现前整文件 skip；落地后移除 skip。
"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, role_payload, runtime_payload, human_headers  # noqa: E402

pytestmark = [
    pytest.mark.usefixtures("client"),
]


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload(slug="backend-eng"))
    await client.post(f"{API}/roles", json=role_payload(slug="frontend-eng"))
    # design-dev-test 模板的参与角色
    await client.post(f"{API}/roles", json=role_payload(slug="designer"))
    await client.post(f"{API}/roles", json=role_payload(slug="developer"))
    await client.post(f"{API}/roles", json=role_payload(slug="tester"))


async def _ws(client, **kw):
    return await client.post(f"{API}/workspaces", json={"name": "main", **kw}, headers=auth_headers())


# ---- 频道 CRUD + 成员绑定 -------------------------------------------------


async def test_channel_crud(client):
    """建频道 → 列表可见。"""
    await _seed(client)
    r = await _ws(client, name="backend")
    assert r.status_code == 201
    listed = await client.get(f"{API}/workspaces", headers=auth_headers())
    assert listed.status_code == 200
    assert any(w["name"] == "backend" for w in listed.json())


async def test_bind_member(client):
    """绑定角色到频道（access=write），成员列表可见。"""
    await _seed(client)
    ws = (await _ws(client, name="c1")).json()
    r = await client.post(
        f"{API}/workspaces/{ws['id']}/members",
        json={"role_slug": "backend-eng", "access": "write"},
        headers=auth_headers(),
    )
    assert r.status_code == 201
    members = (await client.get(f"{API}/workspaces/{ws['id']}/members", headers=auth_headers())).json()
    assert any(m["role_slug"] == "backend-eng" and m["access"] == "write" for m in members)


async def test_patch_member_access(client):
    """改成员权限 write→admin。"""
    await _seed(client)
    ws = (await _ws(client, name="c2")).json()
    await client.post(f"{API}/workspaces/{ws['id']}/members", json={"role_slug": "backend-eng", "access": "write"}, headers=auth_headers())
    r = await client.patch(
        f"{API}/workspaces/{ws['id']}/members/backend-eng",
        json={"access": "admin"}, headers=auth_headers(),
    )
    assert r.status_code == 200
    members = (await client.get(f"{API}/workspaces/{ws['id']}/members", headers=auth_headers())).json()
    assert any(m["role_slug"] == "backend-eng" and m["access"] == "admin" for m in members)


async def test_unbind_member(client):
    """解绑角色。"""
    await _seed(client)
    ws = (await _ws(client, name="c3")).json()
    await client.post(f"{API}/workspaces/{ws['id']}/members", json={"role_slug": "backend-eng", "access": "write"}, headers=auth_headers())
    r = await client.delete(f"{API}/workspaces/{ws['id']}/members/backend-eng", headers=human_headers())
    assert r.status_code in (200, 204)
    members = (await client.get(f"{API}/workspaces/{ws['id']}/members", headers=auth_headers())).json()
    assert not any(m["role_slug"] == "backend-eng" for m in members)


# ---- 作用域隔离 ----------------------------------------------------------


async def test_claim_rejects_non_member(client):
    """非频道成员角色 claim 该频道任务 → 409。"""
    await _seed(client)
    ws = (await _ws(client, name="c4")).json()
    await client.post(f"{API}/workspaces/{ws['id']}/members", json={"role_slug": "backend-eng", "access": "write"}, headers=auth_headers())
    # 任务属于 c4 频道，frontend-eng 非成员
    task = (await client.post(f"{API}/tasks", json={"title": "x", "workspace_id": ws["id"]}, headers=auth_headers())).json()
    r = await client.post(f"{API}/tasks/{task['id']}/claim", json={}, headers=auth_headers("frontend-eng"))
    assert r.status_code == 409


async def test_claim_rejects_readonly_member(client):
    """access=read 的成员 claim → 409（须 write/admin）。"""
    await _seed(client)
    ws = (await _ws(client, name="c5")).json()
    await client.post(f"{API}/workspaces/{ws['id']}/members", json={"role_slug": "backend-eng", "access": "read"}, headers=auth_headers())
    task = (await client.post(f"{API}/tasks", json={"title": "x", "workspace_id": ws["id"]}, headers=auth_headers())).json()
    r = await client.post(f"{API}/tasks/{task['id']}/claim", json={}, headers=auth_headers("backend-eng"))
    assert r.status_code == 409


async def test_role_multi_channel_different_access(client):
    """同一角色加入两个频道、不同 access。"""
    await _seed(client)
    ws1 = (await _ws(client, name="c6")).json()
    ws2 = (await _ws(client, name="c7")).json()
    await client.post(f"{API}/workspaces/{ws1['id']}/members", json={"role_slug": "backend-eng", "access": "write"}, headers=auth_headers())
    await client.post(f"{API}/workspaces/{ws2['id']}/members", json={"role_slug": "backend-eng", "access": "read"}, headers=auth_headers())
    # 频道 1 可认领、频道 2 不可
    t1 = (await client.post(f"{API}/tasks", json={"title": "a", "workspace_id": ws1["id"]}, headers=auth_headers())).json()
    t2 = (await client.post(f"{API}/tasks", json={"title": "b", "workspace_id": ws2["id"]}, headers=auth_headers())).json()
    assert (await client.post(f"{API}/tasks/{t1['id']}/claim", json={}, headers=auth_headers("backend-eng"))).status_code == 200
    assert (await client.post(f"{API}/tasks/{t2['id']}/claim", json={}, headers=auth_headers("backend-eng"))).status_code == 409


async def test_task_filtered_by_channel(client):
    """任务列表按频道过滤。"""
    await _seed(client)
    ws1 = (await _ws(client, name="c8")).json()
    ws2 = (await _ws(client, name="c9")).json()
    await client.post(f"{API}/tasks", json={"title": "a", "workspace_id": ws1["id"]}, headers=auth_headers())
    await client.post(f"{API}/tasks", json={"title": "b", "workspace_id": ws2["id"]}, headers=auth_headers())
    r = await client.get(f"{API}/tasks", params={"workspace_id": ws1["id"]}, headers=auth_headers())
    assert all(t["workspace_id"] == ws1["id"] for t in r.json())


# ---- 频道内完整链路 ------------------------------------------------------


async def test_channel_full_flow(client):
    """建频道→绑定→拆解→派发→自认领→评审→done（锚定频道作用域）。"""
    await _seed(client)
    ws = (await _ws(client, name="c10")).json()
    d = (await client.post(f"{API}/board/decompose", json={"goal": "g", "workspace_id": ws["id"]}, headers=auth_headers())).json()
    await client.post(f"{API}/board/drafts/approve", json={"draft_ids": [d["id"]]}, headers=human_headers())
    tasks = (await client.get(f"{API}/tasks", params={"workspace_id": ws["id"]}, headers=auth_headers())).json()
    task = tasks[0]
    tid = task["id"]
    # 频道作用域 + 团队任务：绑定并让所有参与角色各自认领/提交
    for role in task["assignees"]:
        await client.post(f"{API}/workspaces/{ws['id']}/members", json={"role_slug": role, "access": "write"}, headers=auth_headers())
        assert (await client.post(f"{API}/tasks/{tid}/claim", json={}, headers=auth_headers(role))).status_code == 200
        assert (await client.post(f"{API}/tasks/{tid}/submit", json={"summary": "x"}, headers=auth_headers(role))).status_code == 200
    assert (await client.post(f"{API}/tasks/{tid}/review", json={"approve": True}, headers=human_headers())).status_code == 200
    body = (await client.get(f"{API}/tasks/{tid}", headers=auth_headers())).json()
    assert body["status"] == "done"
