"""端点 → 运行图接线集成测试（HTTP 全流程 + fake ProcessManager）。"""
from __future__ import annotations

import httpx
import pytest

from anvil import config
from anvil.main import create_app
from anvil.platform import factory

from tests.conftest import human_headers
from tests.graph.conftest import FakePM

API = "/api/v1"
H = {"X-Anvil-Session": "s", "X-Anvil-Role": "dev"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "agents").mkdir(parents=True, exist_ok=True)
    (data / "repos").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "REPOS_DIR", data / "repos")
    monkeypatch.setattr(factory, "get_platform", lambda: (FakePM(), None))

    app = create_app({"db_url": f"sqlite+aiosqlite:///{tmp_path}/test.db", "test_auth": True})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed(client):
    await client.post(f"{API}/runtimes", json={"id": "claude-code", "display_name": "Claude",
                                               "executable_path": "/bin/echo", "status": "ready",
                                               "capabilities": {}}, headers=H)
    await client.post(f"{API}/roles", json={"slug": "dev", "display_name": "Dev",
                                            "runtime": {"id": "claude-code"},
                                            "workspace": {"home": "agents/dev/", "repos": []},
                                            "tools": {}, "concurrency": {}}, headers=H)
    await client.post(f"{API}/tasks", json={"id": "T-1", "title": "x"}, headers=H)


async def test_endpoint_run_claim_submit_review(client):
    await _seed(client)
    # 手动派发：POST /runs → create_run + pump wake（spawn）
    r = await client.post(f"{API}/runs", json={"role_slug": "dev", "task_id": "T-1"},
                          headers=human_headers())
    assert r.status_code == 201
    rid = r.json()["id"]
    assert (await client.get(f"{API}/runs/{rid}", headers=H)).json()["status"] == "running"

    # 认领 → 提交 → 评审（端点驱动任务 5 态，并 pump/resume 运行图）
    assert (await client.post(f"{API}/tasks/T-1/claim", json={}, headers=H)).status_code == 200
    assert (await client.post(f"{API}/tasks/T-1/submit", json={"summary": "x"}, headers=H)).status_code == 200
    assert (await client.get(f"{API}/runs/{rid}", headers=H)).json()["status"] == "reviewing"

    assert (await client.post(f"{API}/tasks/T-1/review", json={"approve": True}, headers=human_headers())).status_code == 200
    assert (await client.get(f"{API}/tasks/T-1", headers=H)).json()["status"] == "done"
    assert (await client.get(f"{API}/runs/{rid}", headers=H)).json()["status"] == "completed"
