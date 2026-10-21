"""L2 · 运行时注册 / 发现 / 健康检查（design §2，SE §3 / §10 runtimes 段）。

契约：`GET/POST /api/v1/runtimes`、`POST /runtimes/discover`、`POST /runtimes/{id}/probe`、
`GET /runtimes/{id}/capabilities`、`PUT /runtimes/{id}`。
"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, human_headers, runtime_payload

pytestmark = pytest.mark.usefixtures("client")


async def _post_runtime(client, **kw):
    return await client.post(f"{API}/runtimes", json=runtime_payload(**kw))


async def test_register_runtime_201(client):
    """注册一个 ready 运行时 → 落库并可列表查到。"""
    r = await _post_runtime(client)
    assert r.status_code == 201, r.text
    listed = await client.get(f"{API}/runtimes")
    assert listed.status_code == 200
    ids = [x["id"] for x in listed.json()]
    assert "claude-code" in ids


async def test_list_includes_health_class_capabilities(client):
    """列表项含 health/class/capabilities（design §3.1 旧口径 → 现 capabilities）。"""
    await _post_runtime(client)
    r = await client.get(f"{API}/runtimes")
    items = {x["id"]: x for x in r.json()}
    item = items["claude-code"]
    assert "capabilities" in item
    assert "status" in item  # ready|unverified|unhealthy|missing


async def test_runtime_status_transitions(client):
    """status 四值 ready/unverified/unhealthy/missing（§2.4 绑定流程）。"""
    for st in ("ready", "unverified", "unhealthy", "missing"):
        await _post_runtime(client, id_=f"rt-{st}", status=st)
    r = await client.get(f"{API}/runtimes")
    got = {x["id"]: x["status"] for x in r.json()}
    for st in ("ready", "unverified", "unhealthy", "missing"):
        assert got[f"rt-{st}"] == st


async def test_probe_updates_health(client):
    """probe 刷新 last_probe_at / last_probe_log / status。"""
    await _post_runtime(client)
    r = await client.post(f"{API}/runtimes/claude-code/probe")
    assert r.status_code == 200
    detail = await client.get(f"{API}/runtimes/claude-code")
    body = detail.json()
    assert "last_probe_at" in body


async def test_discover_returns_candidates(client):
    """自动发现扫描返回候选（含未知候选，供 generic 适配）。"""
    r = await client.post(f"{API}/runtimes/discover")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_default_runtime_get_and_set(client):
    """默认运行时可由自然人设置，供 AI 生成画像/角色使用。"""
    await _post_runtime(client)
    r = await client.get(f"{API}/runtimes/default")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "claude-code"

    r = await client.put(f"{API}/runtimes/default", headers=human_headers(),
                         json={"id": "claude-code"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "claude-code"
    assert body["runtime"]["id"] == "claude-code"


async def test_default_runtime_rejects_unknown_id(client):
    await _post_runtime(client)
    r = await client.put(f"{API}/runtimes/default", headers=human_headers(),
                         json={"id": "no-such-runtime"})
    assert r.status_code == 404


async def test_default_runtime_set_requires_human(client):
    await _post_runtime(client)
    r = await client.put(f"{API}/runtimes/default", headers=auth_headers(),
                         json={"id": "claude-code"})
    assert r.status_code == 403
