"""安全加固回归：统一鉴权、SPA 路径穿越、危险入口人类专属、角色 token 任务范围。"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("anvil.main")
from anvil import config  # noqa: E402
from anvil.db import open_conn  # noqa: E402
from anvil.main import create_app  # noqa: E402
from anvil.util import now_iso  # noqa: E402
from tests.conftest import runtime_payload  # noqa: E402


@pytest.fixture
def secure_app(tmp_path, monkeypatch):
    monkeypatch.delenv("ANVIL_TEST_AUTH", raising=False)
    data = tmp_path / "data"
    (data / "agents").mkdir(parents=True)
    (data / "repos").mkdir()
    dist = tmp_path / "frontend"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("SECRET_OUTSIDE_DIST", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "REPOS_DIR", data / "repos")
    monkeypatch.setattr(config, "USER_RUNTIMES_DIR", data / "runtimes")
    monkeypatch.setattr(config, "FRONTEND_DIST", dist)
    monkeypatch.setattr(config, "AUTH_TOKEN", "")
    return create_app({"db_url": f"sqlite+aiosqlite:///{tmp_path}/secure.db", "test_auth": False})


@pytest.fixture
async def secure_client(secure_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=secure_app), base_url="http://test"
    ) as c:
        yield c


async def test_all_api_requires_valid_token(secure_client):
    assert (await secure_client.get("/api/v1/tasks")).status_code == 401
    assert (await secure_client.get(
        "/api/v1/tasks", headers={"X-Anvil-Session": "not-a-real-token"})).status_code == 401
    assert (await secure_client.get(
        "/api/v1/tasks", headers={"X-Anvil-Session": "anvil"})).status_code == 200


async def test_shared_token_cannot_impersonate_agent(secure_client):
    r = await secure_client.get(
        "/api/v1/me",
        headers={"X-Anvil-Session": "anvil", "X-Anvil-Role": "cindy"},
    )
    assert r.status_code == 403


async def test_spa_fallback_blocks_traversal(secure_client):
    r = await secure_client.get("/%2E%2E/secret.txt")
    assert r.status_code == 404
    assert b"SECRET_OUTSIDE_DIST" not in r.content


async def test_runtime_registration_is_human_only(secure_client, secure_app):
    token = "agent-token"
    conn = open_conn(secure_app.state.db_path)
    conn.execute(
        "INSERT INTO sessions (token, role_slug, run_id, task_id, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        (token, "qa-role", None, None, now_iso(), "2099-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    body = runtime_payload()
    assert (await secure_client.post("/api/v1/runtimes", json=body)).status_code == 401
    assert (await secure_client.post(
        "/api/v1/runtimes", json=body,
        headers={"X-Anvil-Session": token})).status_code == 403
    assert (await secure_client.post(
        "/api/v1/runtimes", json=body,
        headers={"X-Anvil-Session": "anvil"})).status_code == 201


async def test_role_token_cannot_touch_unrelated_bound_task(secure_client, secure_app):
    human = {"X-Anvil-Session": "anvil"}
    await secure_client.post(
        "/api/v1/tasks", json={"id": "T-BOUND", "title": "bound", "assignees": ["dev"]},
        headers=human)
    await secure_client.post(
        "/api/v1/tasks", json={"id": "T-OPEN", "title": "open"}, headers=human)

    conn = open_conn(secure_app.state.db_path)
    conn.execute(
        "INSERT INTO sessions (token, role_slug, run_id, task_id, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        ("role-token", "qa-role", None, None, now_iso(), "2099-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    h = {"X-Anvil-Session": "role-token"}
    assert (await secure_client.post(
        "/api/v1/tasks/T-BOUND/comment", json={"body": "x"}, headers=h)).status_code == 403
    assert (await secure_client.post(
        "/api/v1/tasks/T-OPEN/comment", json={"body": "x"}, headers=h)).status_code == 200
