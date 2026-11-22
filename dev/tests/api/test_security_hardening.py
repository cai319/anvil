"""安全加固回归：统一鉴权、SPA 路径穿越、危险入口人类专属、角色 token 任务范围。"""
from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlencode

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


async def test_all_api_requires_valid_token(secure_client, secure_app):
    assert (await secure_client.get("/api/v1/tasks")).status_code == 401
    assert (await secure_client.get(
        "/api/v1/tasks", headers={"X-Anvil-Session": "not-a-real-token"})).status_code == 401
    assert (await secure_client.get(
        "/api/v1/tasks", headers={"X-Anvil-Session": "anvil"})).status_code == 401
    assert (await secure_client.get(
        "/api/v1/tasks", headers={"X-Anvil-Session": "anvil-web"})).status_code == 401
    assert (await secure_client.get(
        "/api/v1/tasks",
        headers={"X-Anvil-Session": secure_app.state.auth_token})).status_code == 200


async def test_shared_token_cannot_impersonate_agent(secure_client, secure_app):
    r = await secure_client.get(
        "/api/v1/me",
        headers={"X-Anvil-Session": secure_app.state.auth_token, "X-Anvil-Role": "cindy"},
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
        headers={"X-Anvil-Session": secure_app.state.auth_token})).status_code == 201


async def test_role_token_cannot_touch_unrelated_bound_task(secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
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


def _session_token(app, token: str, role: str, task_id: str | None = None) -> dict:
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, task_id, now_iso(), "2099-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def test_agent_task_list_and_read_are_scoped(secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
    for tid, role in (("T-DEV", "dev"), ("T-QA", "qa-role")):
        assert (await secure_client.post(
            "/api/v1/tasks", json={"id": tid, "title": tid, "assignees": [role]},
            headers=human)).status_code == 201
    headers = _session_token(secure_app, "list-scope", "qa-role")
    listing = await secure_client.get("/api/v1/tasks", headers=headers)
    assert listing.status_code == 200
    ids = {t["id"] for t in listing.json()}
    assert "T-QA" in ids and "T-DEV" not in ids
    assert (await secure_client.get("/api/v1/tasks/T-DEV", headers=headers)).status_code == 403
    assert (await secure_client.get("/api/v1/tasks/T-QA", headers=headers)).status_code == 200


async def test_repo_catalog_hides_task_samples_from_agents(
        secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
    await secure_client.post(
        "/api/v1/tasks",
        json={"id": "T-REPO-SAMPLE", "title": "SECRET_REPO_TASK_TITLE",
              "repo": "secret-topic", "assignees": ["dev"]},
        headers=human)
    human_catalog = await secure_client.get(
        "/api/v1/knowledge/repos", headers=human)
    assert human_catalog.status_code == 200
    profile = next(
        p for p in human_catalog.json()["repos"] if p["key"] == "secret-topic")
    assert any("SECRET_REPO_TASK_TITLE" in sample["title"]
               for sample in profile["samples"]["tasks"])

    agent = _session_token(secure_app, "catalog-agent", "qa-role")
    agent_catalog = await secure_client.get(
        "/api/v1/knowledge/repos", headers=agent)
    assert agent_catalog.status_code == 200
    profile = next(
        p for p in agent_catalog.json()["repos"] if p["key"] == "secret-topic")
    assert profile["samples"] == {"lessons": [], "tasks": []}
    assert "SECRET_REPO_TASK_TITLE" not in agent_catalog.text


async def test_agent_cannot_read_other_tasks_board_drafts(secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
    await secure_client.post(
        "/api/v1/tasks", json={"id": "T-DRAFT-DEV", "title": "dev",
                               "assignees": ["dev"]}, headers=human)
    draft = await secure_client.post(
        "/api/v1/board/drafts", headers=human, json={
            "goal": "dev plan", "source_task": "T-DRAFT-DEV", "kind": "workflow",
            "tree": {"steps": [{"id": "S1", "title": "x", "role": "dev"}]},
        })
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]

    agent = _session_token(secure_app, "draft-agent", "qa-role")
    listing = await secure_client.get("/api/v1/board/drafts", headers=agent)
    assert listing.status_code == 200
    assert all(item.get("source_task") != "T-DRAFT-DEV" for item in listing.json())
    assert (await secure_client.get(
        f"/api/v1/board/drafts/{draft_id}", headers=agent)).status_code == 403


async def test_run_token_cannot_read_another_task_inbox(secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
    for tid in ("T-IN-A", "T-IN-B"):
        await secure_client.post(
            "/api/v1/tasks", json={"id": tid, "title": tid, "assignees": ["qa-role"]},
            headers=human)
    headers = _session_token(secure_app, "inbox-bound", "qa-role", task_id="T-IN-B")
    assert (await secure_client.get(
        "/api/v1/me/inbox", params={"task_id": "T-IN-A"}, headers=headers)).status_code == 403
    assert (await secure_client.get(
        "/api/v1/me/inbox", params={"task_id": "T-IN-B"}, headers=headers)).status_code == 200


async def test_mcp_env_never_returned_and_reads_are_human_only(
        secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
    created = await secure_client.post(
        "/api/v1/mcp/servers",
        json={"name": "secret-mcp", "command": "echo",
              "env": {"API_KEY": "super-secret"}},
        headers=human)
    assert created.status_code == 201, created.text
    assert created.json()["env"] == {}
    listed = await secure_client.get("/api/v1/mcp/servers", headers=human)
    assert listed.status_code == 200
    assert all(server["env"] == {} for server in listed.json())
    assert "super-secret" not in listed.text

    agent = _session_token(secure_app, "mcp-agent", "qa-role")
    assert (await secure_client.get(
        "/api/v1/mcp/servers", headers=agent)).status_code == 403


async def test_runtime_definition_and_events_are_human_only(
        secure_client, secure_app):
    human = {"X-Anvil-Session": secure_app.state.auth_token}
    body = runtime_payload()
    assert (await secure_client.post(
        "/api/v1/runtimes", json=body, headers=human)).status_code == 201
    agent = _session_token(secure_app, "rt-agent", "qa-role")
    assert (await secure_client.get(
        "/api/v1/runtimes/claude-code/definition", headers=human)).status_code == 200
    assert (await secure_client.get(
        "/api/v1/runtimes/claude-code/definition", headers=agent)).status_code == 403
    assert (await secure_client.get(
        "/api/v1/events", headers=agent)).status_code == 403


async def _asgi_first_chunk(app, query: dict[str, str] | None = None,
                            headers: dict[str, str] | None = None,
                            timeout: float = 2.0):
    """驱动 ASGI app 到首个非空响应体 chunk 后取消（SSE 流不会自然结束）。

    httpx.ASGITransport / Starlette TestClient 会等待完整 body，不能直接用于 SSE 测试。
    """
    header_list = [(b"host", b"testserver")]
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": urlencode(query or {}).encode(),
        "root_path": "",
        "headers": header_list,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "app": app,
    }
    sent: list[dict] = []
    first_body = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start, body


async def test_events_query_token_auth(secure_app):
    """EventSource 无法带头：/events 必须接受 query token，且返回 connected 握手。"""
    start, body = await _asgi_first_chunk(
        secure_app, {"token": secure_app.state.auth_token})
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers.get("cache-control") == "no-store"
    assert b"event: connected" in body


async def test_events_query_token_is_scoped_to_events(secure_client, secure_app):
    """query token 只能用于 events，其他路由继续 401。"""
    r = await secure_client.get(
        f"/api/v1/tasks?token={secure_app.state.auth_token}")
    assert r.status_code == 401


async def test_events_missing_or_invalid_query_token(secure_client):
    assert (await secure_client.get("/api/v1/events")).status_code == 401
    assert (await secure_client.get("/api/v1/events?token=bogus")).status_code == 401


async def test_events_agent_query_token_forbidden(secure_app):
    _session_token(secure_app, "sse-agent", "qa-role")
    start, _ = await _asgi_first_chunk(secure_app, {"token": "sse-agent"})
    assert start["status"] == 403


async def test_events_expired_session_query_token(secure_app):
    conn = open_conn(secure_app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            ("expired-sse", "human", None, None, now_iso(),
             "2000-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    start, _ = await _asgi_first_chunk(secure_app, {"token": "expired-sse"})
    assert start["status"] == 401


async def test_events_header_takes_precedence_over_query(secure_app):
    start, _ = await _asgi_first_chunk(
        secure_app, {"token": "bogus"},
        headers={"X-Anvil-Session": secure_app.state.auth_token})
    assert start["status"] == 200


async def test_events_test_auth_bypass_still_human_only(client):
    """ANVIL_TEST_AUTH 旁路只代表 qa-role，events 仍必须 403。"""
    assert (await client.get("/api/v1/events?token=whatever")).status_code == 403


def test_generated_auth_token_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "")
    token_file = tmp_path / "auth-token"
    token = config.resolve_auth_token(token_file)
    assert len(token) >= 16
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert (token_file.stat().st_mode & 0o777) == 0o600
    assert config.resolve_auth_token(token_file) == token


def test_weak_explicit_auth_token_rejected(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "anvil-web")
    with pytest.raises(RuntimeError):
        config.resolve_auth_token()


async def test_security_headers_present(secure_client):
    r = await secure_client.get("/api/v1/tasks")
    assert r.status_code == 401
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["x-content-type-options"] == "nosniff"
