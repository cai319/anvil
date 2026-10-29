"""M4 / M2 补全：reminder、记忆检索、lessons 升格、proposal 生效、reset、版本回滚等。

这些用例覆盖本轮「未实现需求」补全的闭环行为，使用隔离的数据目录（tmp_path），
不会写到真实的 data/agents。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil import config
from tests.conftest import API, app_factory, auth_headers, role_payload, runtime_payload, human_headers  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    data = tmp_path / "data"
    agents = data / "agents"
    repos = data / "repos"
    runtimes = data / "runtimes"
    for d in (agents, repos, runtimes):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "REPOS_DIR", repos)
    monkeypatch.setattr(config, "USER_RUNTIMES_DIR", runtimes)
    return app_factory.create_app(
        {"db_url": f"sqlite+aiosqlite:///{tmp_path}/test.db", "test_auth": True}
    )


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(executable_path="/bin/echo"),
                      headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload(), headers=auth_headers())


# ---- Reminder（§6.4） -----------------------------------------------------


async def test_reminder_at_fires_and_creates_run(client):
    await _seed(client)
    r = await client.post(f"{API}/reminders", json={
        "role_slug": "qa-role", "kind": "at",
        "fire_at": "2020-01-01T00:00:00+00:00", "prompt": "回顾本周任务",
    }, headers=auth_headers())
    assert r.status_code == 201

    await client.post(f"{API}/control/tick", headers=auth_headers())
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert any(run["prompt"] == "回顾本周任务" for run in runs)

    reminders = (await client.get(f"{API}/reminders", headers=auth_headers())).json()
    assert reminders[0]["status"] == "fired"


async def test_role_create_attaches_daily_review_reminder(client):
    await _seed(client)
    reminders = (await client.get(f"{API}/reminders", headers=auth_headers())).json()
    daily = [r for r in reminders if r["role_slug"] == "qa-role" and "回顾最近" in (r["prompt"] or "")]
    assert len(daily) == 1
    assert daily[0]["kind"] == "cron"
    assert daily[0]["cron_expr"] == "0 9 * * *"


async def test_reminder_cron_crud(client):
    await _seed(client)
    r = await client.post(f"{API}/reminders", json={
        "role_slug": "qa-role", "kind": "cron",
        "cron_expr": "0 9 * * 1", "prompt": "周复盘",
    }, headers=auth_headers())
    assert r.status_code == 201
    reminder = r.json()
    assert reminder["next_fire_at"]

    upd = await client.put(f"{API}/reminders/{reminder['id']}",
                           json={"prompt": "新的复盘提示"}, headers=auth_headers())
    assert upd.status_code == 200
    assert upd.json()["prompt"] == "新的复盘提示"

    listed = await client.get(f"{API}/reminders", headers=auth_headers())
    assert any(x["id"] == reminder["id"] for x in listed.json())

    deleted = await client.delete(f"{API}/reminders/{reminder['id']}", headers=human_headers())
    assert deleted.status_code == 204


async def test_reminder_when_condition_fires(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-COND", "title": "条件任务"},
                      headers=auth_headers())
    await client.post(f"{API}/reminders", json={
        "role_slug": "qa-role", "kind": "when", "when_condition": "T-COND",
        "prompt": "任务完成了，检查后续",
    }, headers=auth_headers())

    # 未完成 → 不触发
    await client.post(f"{API}/control/tick", headers=auth_headers())
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert not any(run.get("prompt") == "任务完成了，检查后续" for run in runs)

    await client.post(f"{API}/tasks/T-COND/claim", json={}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-COND/submit", json={}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-COND/review", json={"approve": True}, headers=human_headers())
    await client.post(f"{API}/control/tick", headers=auth_headers())
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert any(run.get("prompt") == "任务完成了，检查后续" for run in runs)


# ---- 记忆 / shared lessons（§8） ------------------------------------------


async def test_memory_append_and_fts_search(client):
    await _seed(client)
    await client.post(f"{API}/me/memory", json={"content": "alembic migration gotcha"},
                      headers=auth_headers())
    hits = (await client.get(f"{API}/me/memory/search", params={"q": "alembic"},
                             headers=auth_headers())).json()
    assert any("alembic" in h["content"] for h in hits)
    memory_file = Path(config.AGENTS_DIR) / "qa-role" / "MEMORY.md"
    assert "alembic migration gotcha" in memory_file.read_text(encoding="utf-8")


async def test_lesson_approval_writes_shared_file_and_respects_cap(client, monkeypatch):
    from anvil.services import memory as memory_service
    await _seed(client)
    monkeypatch.setattr(memory_service, "LESSON_CAP", 1)

    first = (await client.post(f"{API}/lessons/pending", json={
        "content": "所有金额字段使用 Decimal", "tags": ["backend"], "repo": "shared",
    }, headers=auth_headers())).json()
    assert (await client.post(f"{API}/lessons/{first['id']}/approve",
                              headers=human_headers())).status_code == 200
    shared_file = Path(config.DATA_DIR) / "shared" / "lessons.md"
    assert "Decimal" in shared_file.read_text(encoding="utf-8")

    second = (await client.post(f"{API}/lessons/pending", json={
        "content": "第二条经验", "tags": ["backend"],
    }, headers=auth_headers())).json()
    over_cap = await client.post(f"{API}/lessons/{second['id']}/approve", headers=human_headers())
    assert over_cap.status_code == 409

    rejected = await client.post(f"{API}/lessons/{second['id']}/reject", headers=human_headers())
    assert rejected.json()["status"] == "rejected"


async def test_proposal_approval_applies_field(client):
    await _seed(client)
    p = (await client.post(f"{API}/proposals", json={
        "field": "responsibilities", "proposed": ["api-design", "code-review"],
        "rationale": "近期任务以 API 为主", "risk": "可能忽略测试职责",
    }, headers=auth_headers())).json()
    approved = await client.post(f"{API}/proposals/{p['id']}/approve", headers=human_headers())
    assert approved.status_code == 200
    profile = (await client.get(f"{API}/roles/qa-role/profile", headers=auth_headers())).json()
    assert profile["responsibilities"] == ["api-design", "code-review"]

    listed = await client.get(f"{API}/proposals", params={"status": "approved"},
                              headers=auth_headers())
    assert any(x["id"] == p["id"] for x in listed.json())


async def test_self_instruction_writes_file(client):
    await _seed(client)
    r = await client.post(f"{API}/me/instruction", json={"content": "你是负责 API 的工程师"},
                          headers=auth_headers())
    assert r.status_code == 200
    path = Path(config.AGENTS_DIR) / "qa-role" / "instruction.md"
    assert "负责 API" in path.read_text(encoding="utf-8")


# ---- 任务 / 运行生命周期补全 ---------------------------------------------


async def test_task_delete_cascades(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-DEL", "title": "删除我"}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-DEL/claim", json={}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-DEL/comment", json={"body": "评论"},
                      headers=auth_headers())
    deleted = await client.delete(f"{API}/tasks/T-DEL", headers=human_headers())
    assert deleted.status_code == 204
    assert (await client.get(f"{API}/tasks/T-DEL", headers=auth_headers())).status_code == 404
    assert (await client.get(f"{API}/tasks/T-DEL/timeline",
                             headers=auth_headers())).status_code == 404


async def test_task_delete_requires_force_with_active_run(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-DEL2", "title": "活动任务"},
                      headers=auth_headers())
    await client.post(f"{API}/runs", json={"role_slug": "qa-role", "task_id": "T-DEL2"},
                      headers=auth_headers())
    blocked = await client.delete(f"{API}/tasks/T-DEL2", headers=human_headers())
    assert blocked.status_code == 409
    forced = await client.delete(f"{API}/tasks/T-DEL2", params={"force": "true"},
                                 headers=human_headers())
    assert forced.status_code == 204
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert not any(r["task_id"] == "T-DEL2" for r in runs)


async def test_task_cancel_and_reopen(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-CX", "title": "取消我"}, headers=auth_headers())
    closed = await client.post(f"{API}/tasks/T-CX/cancel", headers=human_headers())
    assert closed.json()["status"] == "closed"
    reopened = await client.post(f"{API}/tasks/T-CX/reopen", headers=human_headers())
    assert reopened.json()["status"] == "todo"


async def test_run_restart_full_reset_and_release(client):
    await _seed(client)
    await client.post(f"{API}/me/memory", json={"content": "会被清掉的记忆"}, headers=auth_headers())
    run = (await client.post(f"{API}/runs", json={"role_slug": "qa-role"}, headers=auth_headers())).json()

    restarted = await client.post(f"{API}/runs/{run['id']}/restart", headers=human_headers())
    assert restarted.status_code == 200
    assert restarted.json()["reset"] == "restart"

    reset = await client.post(f"{API}/runs/{run['id']}/full-reset", headers=human_headers())
    assert reset.status_code == 200
    assert reset.json()["reset"] == "full"
    assert reset.json()["backup"]
    assert (Path(config.AGENTS_DIR) / "qa-role" / "MEMORY.md").read_text(encoding="utf-8") == ""

    released = await client.delete(f"{API}/runs/{run['id']}", headers=human_headers())
    assert released.status_code == 204
    body = (await client.get(f"{API}/runs/{run['id']}", headers=auth_headers())).json()
    assert body["status"] == "abandoned"


async def test_role_versions_and_rollback(client):
    await _seed(client)
    await client.put(f"{API}/roles/qa-role", json=role_payload(system_prompt="v2 提示"),
                     headers=human_headers())
    versions = (await client.get(f"{API}/roles/qa-role/versions", headers=auth_headers())).json()
    assert len(versions) >= 2

    rolled = await client.post(f"{API}/roles/qa-role/rollback/1", headers=human_headers())
    assert rolled.status_code == 200
    profile = (await client.get(f"{API}/roles/qa-role/profile", headers=auth_headers())).json()
    assert not profile["system_prompt"]


async def test_role_home_file_read_and_traversal_guard(client):
    await _seed(client)
    await client.post(f"{API}/me/memory", json={"content": "可见内容"}, headers=auth_headers())
    ok = await client.get(f"{API}/roles/qa-role/home/file", params={"path": "MEMORY.md"},
                          headers=auth_headers())
    assert ok.status_code == 200
    assert "可见内容" in ok.json()["content"]

    denied = await client.get(f"{API}/roles/qa-role/home/file", params={"path": "../profile.yaml"},
                              headers=auth_headers())
    assert denied.status_code == 404


# ---- 治理补全 -------------------------------------------------------------


async def test_budget_limits_block_claim(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-LIM", "title": "限流任务"}, headers=auth_headers())
    await client.put(f"{API}/budget", json={"max_parallel_system": 0}, headers=human_headers())
    blocked = await client.post(f"{API}/tasks/T-LIM/claim", json={}, headers=auth_headers())
    assert blocked.status_code == 409

    await client.put(f"{API}/budget", json={"max_parallel_system": 6}, headers=human_headers())
    allowed = await client.post(f"{API}/tasks/T-LIM/claim", json={}, headers=auth_headers())
    assert allowed.status_code == 200


async def test_token_aggregation(client, app):
    await _seed(client)
    from anvil.db import open_conn
    from anvil.services import tokens
    conn = open_conn(app.state.db_path)
    try:
        tokens.record(conn, "qa-role", "T1", "r1", {
            "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10,
        })
        tokens.record(conn, "developer", "T1", "r2", {
            "input_tokens": 50, "output_tokens": 5,
        })
    finally:
        conn.close()
    data = (await client.get(f"{API}/tokens", headers=auth_headers())).json()
    assert data["total_tokens"] == 215
    assert data["input_tokens"] == 190
    assert data["output_tokens"] == 25
    assert any(r["role"] == "qa-role" and r["total_tokens"] == 160
               for r in data["by_role"])


# ---- 本地 token 轻校验（写操作必须带 X-Anvil-Session） --------------------


async def test_write_requires_session_header(tmp_path, monkeypatch):
    import httpx
    monkeypatch.delenv("ANVIL_TEST_AUTH", raising=False)
    auth_app = app_factory.create_app(
        {"db_url": f"sqlite+aiosqlite:///{tmp_path}/auth.db", "test_auth": False}
    )
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        missing = await c.post(f"{API}/tasks", json={"id": "T-A", "title": "x"})
        assert missing.status_code == 401
        ok = await c.post(f"{API}/tasks", json={"id": "T-A", "title": "x"},
                          headers={"X-Anvil-Session": "anvil-web"})
        assert ok.status_code == 201


# ---- 多角色团队任务：并行认领 / 各自提交 / 全部提交后评审 -----------------


async def _seed_team(client):
    await _seed(client)
    await client.post(f"{API}/roles", json=role_payload(slug="dev"), headers=auth_headers())


async def test_team_task_parallel_claims_and_all_submit(client):
    await _seed_team(client)
    await client.post(f"{API}/tasks", json={
        "id": "T-TEAM", "title": "并行协作", "assignees": ["dev", "qa-role"],
    }, headers=auth_headers())

    # 两个参与角色都能认领（不再排他）
    first = await client.post(f"{API}/tasks/T-TEAM/claim", json={}, headers=auth_headers("dev"))
    assert first.status_code == 200
    second = await client.post(f"{API}/tasks/T-TEAM/claim", json={}, headers=auth_headers("qa-role"))
    assert second.status_code == 200
    task = (await client.get(f"{API}/tasks/T-TEAM", headers=auth_headers())).json()
    assert task["status"] == "in_progress"
    assert {c["role_slug"]: c["status"] for c in task["role_claims"]} == {
        "dev": "working", "qa-role": "working",
    }

    # 第一个角色提交后任务仍在进行中
    r1 = await client.post(f"{API}/tasks/T-TEAM/submit", json={"summary": "dev 完成"},
                           headers=auth_headers("dev"))
    assert r1.status_code == 200
    assert r1.json()["status"] == "in_progress"

    # 全部参与角色提交后才进入评审
    r2 = await client.post(f"{API}/tasks/T-TEAM/submit", json={"summary": "qa 完成"},
                           headers=auth_headers("qa-role"))
    assert r2.status_code == 200
    assert r2.json()["status"] == "in_review"
    task = r2.json()
    assert {c["role_slug"]: c["status"] for c in task["role_claims"]} == {
        "dev": "submitted", "qa-role": "submitted",
    }

    approved = await client.post(f"{API}/tasks/T-TEAM/review", json={"approve": True},
                                 headers=human_headers())
    assert approved.status_code == 200
    assert approved.json()["status"] == "done"
    assert all(c["status"] == "approved" for c in approved.json()["role_claims"])


async def test_team_task_rejects_non_assignee_claim(client):
    await _seed_team(client)
    await client.post(f"{API}/roles", json=role_payload(slug="other-role"),
                      headers=auth_headers())
    await client.post(f"{API}/tasks", json={
        "id": "T-TEAM2", "title": "非成员", "assignees": ["dev"],
    }, headers=auth_headers())
    r = await client.post(f"{API}/tasks/T-TEAM2/claim", json={},
                          headers=auth_headers("other-role"))
    assert r.status_code in (403, 409)


async def test_team_task_unclaim_keeps_other_roles_working(client):
    await _seed_team(client)
    await client.post(f"{API}/tasks", json={
        "id": "T-TEAM3", "title": "放弃", "assignees": ["dev", "qa-role"],
    }, headers=auth_headers())
    await client.post(f"{API}/tasks/T-TEAM3/claim", json={}, headers=auth_headers("dev"))
    await client.post(f"{API}/tasks/T-TEAM3/claim", json={}, headers=auth_headers("qa-role"))

    released = await client.post(f"{API}/tasks/T-TEAM3/unclaim", headers=auth_headers("dev"))
    assert released.json()["status"] == "in_progress"
    claims = {c["role_slug"]: c["status"] for c in released.json()["role_claims"]}
    assert claims == {"dev": "released", "qa-role": "working"}

    last = await client.post(f"{API}/tasks/T-TEAM3/unclaim", headers=auth_headers("qa-role"))
    assert last.json()["status"] == "todo"


# ---- 运行级会话令牌（design §10 末段） -----------------------------------


async def test_run_session_token_binds_role_and_task(client, app):
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-S1", "title": "绑定任务"}, headers=auth_headers())
    await client.post(f"{API}/tasks", json={"id": "T-S2", "title": "别的任务"}, headers=auth_headers())
    await client.post(f"{API}/runs", json={"role_slug": "qa-role", "task_id": "T-S1"},
                      headers=auth_headers())

    from anvil.db import open_conn
    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE task_id='T-S1'").fetchone()
    finally:
        conn.close()
    assert row is not None
    token = row["token"]

    # 冒充 other-role：会话表优先，实际角色仍是 qa-role
    claimed = await client.post(f"{API}/tasks/T-S1/claim", json={},
                                headers={"X-Anvil-Session": token, "X-Anvil-Role": "other-role"})
    assert claimed.status_code == 200
    assert claimed.json()["assignee"] == "qa-role"

    # 绑定任务之外的操作被拒
    other = await client.post(f"{API}/tasks/T-S2/claim", json={},
                              headers={"X-Anvil-Session": token})
    assert other.status_code == 403


# ---- 运行时健康检查 / diff 硬校验 ----------------------------------------


async def test_task_list_filters_and_audit_filters(client):
    await _seed(client)
    await client.post(f"{API}/tasks", json={
        "id": "T-F1", "title": "后端任务", "type": "feature", "tags": ["backend"],
    }, headers=auth_headers())
    await client.post(f"{API}/tasks", json={
        "id": "T-F2", "title": "前端任务", "type": "bugfix", "tags": ["frontend"],
    }, headers=auth_headers())

    by_type = (await client.get(f"{API}/tasks", params={"type": "feature"},
                                headers=auth_headers())).json()
    assert [t["id"] for t in by_type] == ["T-F1"]
    by_tag = (await client.get(f"{API}/tasks", params={"tag": "frontend"},
                               headers=auth_headers())).json()
    assert [t["id"] for t in by_tag] == ["T-F2"]

    await client.post(f"{API}/tasks/T-F1/claim", json={}, headers=auth_headers())
    claims = (await client.get(f"{API}/audit", params={"action": "claim"},
                               headers=human_headers())).json()
    assert any(a["task_id"] == "T-F1" for a in claims)


async def test_role_message_creates_run(client):
    await _seed(client)
    r = await client.post(f"{API}/roles/qa-role/message",
                          json={"message": "检查一下最近的记忆是否需要整理"},
                          headers=auth_headers())
    assert r.status_code == 200
    run = r.json()
    assert run["prompt"] == "检查一下最近的记忆是否需要整理"
    assert run["task_id"] is None


async def test_lesson_update(client):
    await _seed(client)
    lesson = (await client.post(f"{API}/lessons/pending", json={
        "content": "旧内容", "tags": ["a"],
    }, headers=auth_headers())).json()
    updated = await client.put(f"{API}/lessons/{lesson['id']}",
                               json={"content": "新内容", "tags": ["b"]},
                               headers=auth_headers())
    assert updated.status_code == 200
    assert updated.json()["content"] == "新内容"
    assert updated.json()["tags"] == ["b"]


async def test_lesson_creation_requires_cap_eviction(client):
    """80 条上限：达到上限时必须先删除再批准。"""
    from anvil.services import memory as memory_service
    await _seed(client)
    original = memory_service.LESSON_CAP
    memory_service.LESSON_CAP = 0
    try:
        lesson = (await client.post(f"{API}/lessons/pending", json={"content": "x"},
                                    headers=auth_headers())).json()
        r = await client.post(f"{API}/lessons/{lesson['id']}/approve", headers=human_headers())
        assert r.status_code == 409
    finally:
        memory_service.LESSON_CAP = original


async def test_runtime_health_probe_with_manual_definition(client):
    r = await client.post(f"{API}/runtimes", json={
        "id": "fake", "display_name": "Fake", "executable": "/bin/echo",
        "invocation": {"args": ["{{prompt}}"]},
        "health": {"probe_prompt": "ANVIL_OK", "expect_contains": "ANVIL_OK",
                   "args": ["{{probe_prompt}}"], "timeout_seconds": 5},
    }, headers=auth_headers())
    assert r.status_code == 201
    result = await client.post(f"{API}/runtimes/fake/health", headers=auth_headers())
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert result.json()["status"] == "ready"


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "qa@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "QA"], check=True)
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


async def test_workspace_merge_with_guard(client):
    repo = Path(config.REPOS_DIR) / "main"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "feature"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)

    ws = (await client.post(f"{API}/workspaces", json={"name": "main", "repo": "main"},
                            headers=auth_headers())).json()
    merged = await client.post(f"{API}/workspaces/{ws['id']}/merge",
                               json={"branch": "feature", "base_branch": "main"},
                               headers=auth_headers())
    assert merged.status_code == 200, merged.text
    assert (repo / "src" / "feature.py").is_file()


async def test_submit_checks_real_git_diff(client):
    await _seed(client)
    _init_repo(Path(config.REPOS_DIR) / "main")
    role_resp = await client.put(f"{API}/roles/qa-role", json=role_payload(
        workspace={"home": "agents/qa-role/", "repos": [{"name": "main", "access": "write"}]},
    ), headers=human_headers())
    assert role_resp.status_code == 200, role_resp.text
    role_check = await client.get(f"{API}/roles/qa-role", headers=auth_headers())
    assert role_check.status_code == 200, role_check.text
    task_resp = await client.post(f"{API}/tasks", json={
        "id": "T-DIFF", "title": "真实 diff", "repo": "main", "base_branch": "main",
        "scope_paths": ["src/**"],
    }, headers=auth_headers())
    assert task_resp.status_code == 201, task_resp.text
    run_resp = await client.post(f"{API}/runs", json={
        "role_slug": "qa-role", "task_id": "T-DIFF",
    }, headers=auth_headers())
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["worktree_path"]
    worktree = Path(run["worktree_path"])
    assert worktree.exists()

    await client.post(f"{API}/tasks/T-DIFF/claim", json={}, headers=auth_headers())

    (worktree / "outside.txt").write_text("out of scope\n", encoding="utf-8")
    rejected = await client.post(f"{API}/tasks/T-DIFF/submit", json={"summary": "x"},
                                 headers=auth_headers())
    assert rejected.status_code == 409

    (worktree / "outside.txt").unlink()
    (worktree / "src").mkdir(exist_ok=True)
    (worktree / "src" / "a.py").write_text("print('ok')\n", encoding="utf-8")
    accepted = await client.post(f"{API}/tasks/T-DIFF/submit", json={"summary": "x"},
                                 headers=auth_headers())
    assert accepted.status_code == 200

    diff = (await client.get(f"{API}/tasks/T-DIFF/diff", headers=auth_headers())).json()
    assert "src/a.py" in diff["changed"]
    assert diff["violations"] == []
    snap = (await client.get(f"{API}/tasks/T-DIFF/config-snapshot", headers=auth_headers())).json()
    assert snap["run_id"] == run["id"]
    assert snap["snapshot"]["projection"]["runtime"]["id"] == "claude-code"
