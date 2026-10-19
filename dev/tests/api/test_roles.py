"""L2 · 角色 CRUD / home / 工具预算 / 配置快照（design §3，SE §3 / §10 roles 段）。

契约：`GET/POST /api/v1/roles`、`GET/PUT/DELETE /roles/{id}`、`GET /roles/{id}/home`、
`GET /roles/{id}/home/file?path=`（只读）、`GET /roles/{id}/effective-config`、
`GET/PUT /roles/{id}/tools`、`POST /tools/estimate`、`POST /roles/{id}/clone`。
"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, role_payload, runtime_payload

pytestmark = pytest.mark.usefixtures("client")


async def _seed_runtime(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())


async def _post_role(client, **kw):
    return await client.post(f"{API}/roles", json=role_payload(**kw))


async def test_role_crud(client):
    """创建→查→改→删全链路。"""
    await _seed_runtime(client)
    r = await _post_role(client)
    assert r.status_code == 201, r.text
    got = await client.get(f"{API}/roles/qa-role")
    assert got.status_code == 200
    upd = await client.put(f"{API}/roles/qa-role", json=role_payload(display_name="QA2"))
    assert upd.status_code == 200
    assert upd.json()["display_name"] == "QA2"
    dele = await client.delete(f"{API}/roles/qa-role")
    assert dele.status_code in (200, 204)
    gone = await client.get(f"{API}/roles/qa-role")
    assert gone.status_code == 404


async def test_home_readonly(client):
    """home 目录树可读；home/file 只读展示（design §10 明确只读，不提供 PUT）。"""
    await _seed_runtime(client)
    await _post_role(client)
    home = await client.get(f"{API}/roles/qa-role/home")
    assert home.status_code == 200
    # 只读约束：不存在写端点 → 断言 PUT 返回 405/404（由路由未注册决定）
    write = await client.put(f"{API}/roles/qa-role/home/file?path=MEMORY.md", json={"body": "x"})
    assert write.status_code in (404, 405)


async def test_tools_budget_over_limit_rejected(client):
    """超 max_tool_tokens 的 tools 写入返回 422 并附估算明细（§10）。"""
    await _seed_runtime(client)
    await _post_role(client, tools={"preset": "backend-default", "max_tool_tokens": 10})
    over = {
        "preset": "backend-default",
        "allowlist": [{"server": "git", "tools": ["status", "diff", "add", "commit", "log", "branch"]}],
    }
    r = await client.put(f"{API}/roles/qa-role/tools", json=over)
    assert r.status_code == 422
    assert "estimate" in r.json() or "detail" in r.json()


async def test_effective_config_preview(client):
    """effective-config 预览实际投影结果，不改角色定义。"""
    await _seed_runtime(client)
    await _post_role(client)
    r = await client.get(f"{API}/roles/qa-role/effective-config")
    assert r.status_code == 200
    assert "projection" in r.json() or "effective" in r.json()


async def test_me_endpoint(client):
    """GET /api/v1/me 返回身份 + 并发额度 + 已用成本（SE §3.3，M1 必选，QA #1 回归）。"""
    await _seed_runtime(client)
    await _post_role(client)
    r = await client.get(f"{API}/me", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["role_slug"] == "qa-role"
    assert "concurrency_quota" in body
    assert "cost_used_usd" in body


async def test_config_snapshot_frozen(client):
    """§3.3：run 创建瞬间冻结 profile 快照，改配置不影响进行中 run（M2 快照冻结）。"""
    await _seed_runtime(client)
    await _post_role(client)
    snap = await client.get(f"{API}/roles/qa-role/effective-config")
    before = snap.json()
    # 改角色工具 → 快照应保持创建时版本（此处只验证读取端点存在，冻结逻辑 M2 断言）
    await client.put(f"{API}/roles/qa-role/tools", json={"preset": "backend-default"})
    after = await client.get(f"{API}/roles/qa-role/effective-config")
    assert after.status_code == 200
    assert after.json() != before  # 当前配置变了；快照冻结在 run 层断言（见 test_tasks S-config）
