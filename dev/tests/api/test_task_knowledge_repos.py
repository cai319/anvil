"""任务知识 repo API：创建返回绑定、PATCH 替换+锁、重新识别、权限。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import repo_catalog
from anvil.util import jdumps, now_iso
from tests.conftest import API, auth_headers, human_headers


@pytest.fixture
def binding_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPOS_DIR", tmp_path / "repos")
    monkeypatch.setattr(config, "KNOWLEDGE_BINDING_MODE", "auto")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_ENABLED", True)
    repo_catalog.invalidate_catalog()
    yield
    repo_catalog.invalidate_catalog()


def _seed_lesson(app, repo="openssl-4.0.1", tags=("openssl", "asan")):
    conn = connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO lessons (id, content, tags_json, repo, status, created_at, approved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("L-API", "ASan 误报先确认编译优化级别", jdumps(list(tags)), repo,
             "approved", now_iso(), now_iso()))
        conn.commit()
    finally:
        conn.close()
    repo_catalog.invalidate_catalog()


@pytest.mark.asyncio
async def test_create_returns_auto_binding_and_patch_locks(app, client, binding_mode):
    _seed_lesson(app)
    r = await client.post(f"{API}/tasks", headers=human_headers(), json={
        "title": "OpenSSL ASan 误报复现", "tags": ["openssl", "asan"],
        "description": "核实 ASan 报告",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    tid = body["id"]
    applied = [x for x in body["knowledge_repos"] if x["status"] == "applied"]
    assert [x["repo"] for x in applied] == ["openssl-4.0.1"]
    assert body["knowledge_binding_state"] == "applied"

    r = await client.get(f"{API}/tasks/{tid}/knowledge-repos", headers=human_headers())
    assert r.status_code == 200
    assert r.json()["scope"]["mode"] == "multi"
    assert "shared" in r.json()["scope"]["repos"]

    r = await client.patch(f"{API}/tasks/{tid}", headers=human_headers(),
                           json={"knowledge_repos": ["gomoku"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["knowledge_binding_locked"] is True
    assert [(x["repo"], x["source"]) for x in body["knowledge_repos"]] == [("gomoku", "human")]

    # 重新识别：解除锁定，保留 human 行，auto 行可重新出现。
    r = await client.post(f"{API}/tasks/{tid}/knowledge-repos/auto", headers=human_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] is False
    repos = {x["repo"]: x["source"] for x in body["repos"]}
    assert repos["gomoku"] == "human"
    assert repos["openssl-4.0.1"] == "auto"


@pytest.mark.asyncio
async def test_binding_endpoints_are_human_only(app, client, binding_mode):
    r = await client.post(f"{API}/tasks", headers=human_headers(), json={"title": "T"})
    tid = r.json()["id"]
    agent = auth_headers("developer")
    r = await client.post(f"{API}/tasks/{tid}/knowledge-repos/auto", headers=agent)
    assert r.status_code in (401, 403)
    r = await client.patch(f"{API}/tasks/{tid}", headers=agent,
                           json={"knowledge_repos": ["gomoku"]})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_task_list_includes_bindings(app, client, binding_mode):
    _seed_lesson(app)
    r = await client.post(f"{API}/tasks", headers=human_headers(), json={
        "title": "OpenSSL ASan 复核", "tags": ["openssl", "asan"],
    })
    tid = r.json()["id"]
    r = await client.get(f"{API}/tasks", headers=human_headers())
    assert r.status_code == 200
    row = next(x for x in r.json() if x["id"] == tid)
    assert row["knowledge_repos"]
    assert row["knowledge_binding_state"] == "applied"
