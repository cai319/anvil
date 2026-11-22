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
