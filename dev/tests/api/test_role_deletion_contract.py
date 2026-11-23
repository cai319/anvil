"""角色删除契约：活跃 run 拦截、历史快照、角色/任务/run 接口无 500。

这些用例按 TDD 先写：当前实现尚未提供 role_snapshots、include_deleted、
活跃 run 拦截和启动回填，预期先红。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from tests.conftest import (
    API,
    auth_headers,
    human_headers,
    role_payload,
    runtime_payload,
    task_payload,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _seed_runtime_and_role(client, slug: str = "doomed") -> None:
    runtime = await client.post(
        f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    assert runtime.status_code in (200, 201), runtime.text
    role = await client.post(
        f"{API}/roles", json=role_payload(slug), headers=auth_headers())
    assert role.status_code == 201, role.text


def _insert_run(app, rid: str, role: str, status: str = "suspended",
                task_id: str | None = None) -> None:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            (rid, role, task_id, status, _now()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_delete_role_blocks_active_runs_then_snapshots(client, app):
    """有活跃 run 时 409；释放后删除 204，并保留显示快照。"""
    await _seed_runtime_and_role(client)
    _insert_run(app, "R-ACTIVE", "doomed", "suspended")

    blocked = await client.delete(f"{API}/roles/doomed", headers=human_headers())
    assert blocked.status_code == 409, blocked.text
    assert (await client.get(f"{API}/roles/doomed")).status_code == 200

    released = await client.delete(f"{API}/runs/R-ACTIVE", headers=human_headers())
    assert released.status_code == 204, released.text

    deleted = await client.delete(f"{API}/roles/doomed", headers=human_headers())
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"{API}/roles/doomed")).status_code == 404

    conn = sqlite3.connect(app.state.db_path)
    try:
        snap = conn.execute(
            "SELECT slug, display_name, avatar_color, avatar, deleted_at "
            "FROM role_snapshots WHERE slug='doomed'").fetchone()
        assert snap is not None
        assert snap[1] == "QA 角色"
        assert snap[4]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_role_list_include_deleted_returns_snapshot(client, app):
    """默认角色列表不含已删除角色；include_deleted 返回 deleted=true 的快照。"""
    await _seed_runtime_and_role(client)
    deleted = await client.delete(f"{API}/roles/doomed", headers=human_headers())
    assert deleted.status_code == 204, deleted.text

    active = await client.get(f"{API}/roles")
    assert active.status_code == 200
    assert all(r["slug"] != "doomed" for r in active.json())

    historical = await client.get(f"{API}/roles?include_deleted=true")
    assert historical.status_code == 200
    row = next((r for r in historical.json() if r["slug"] == "doomed"), None)
    assert row is not None
    assert row["deleted"] is True
    assert row["display_name"] == "QA 角色"


@pytest.mark.asyncio
async def test_deleted_role_interfaces_do_not_500(client):
    """已删除角色的所有角色接口返回明确 4xx/历史 200，不能出现 500。"""
    await _seed_runtime_and_role(client)
    deleted = await client.delete(f"{API}/roles/doomed", headers=human_headers())
    assert deleted.status_code == 204, deleted.text

    # 需要角色存在的接口：404（不允许 500）
    role_calls = [
        ("GET", "", None),
        ("PUT", "", role_payload("doomed")),
        ("PUT", "/avatar", {"avatar": "initials"}),
        ("PUT", "/runtime", {"id": "claude-code"}),
        ("POST", "/session-reset", {}),
        ("DELETE", "", None),
        ("GET", "/home", None),
        ("GET", "/home/file?path=MEMORY.md", None),
        ("GET", "/home/history", None),
        ("GET", "/versions", None),
        ("POST", "/rollback/1", {}),
        ("POST", "/message", {"message": "hello"}),
        ("GET", "/metrics", None),
        ("GET", "/runtime", None),
        ("GET", "/tools", None),
        ("PUT", "/tools", {}),
        ("GET", "/effective-config", None),
        ("GET", "/catalog", None),
        ("PUT", "/catalog", {}),
        ("GET", "/profile", None),
        ("PUT", "/profile", {}),
        ("POST", "/generate", {"brief": "x"}),
        ("POST", "/clone", {}),
    ]
    for method, suffix, body in role_calls:
        kwargs = {"headers": human_headers()}
        if body is not None:
            kwargs["json"] = body
        resp = await client.request(
            method, f"{API}/roles/doomed{suffix}", **kwargs)
        assert resp.status_code < 500, (method, suffix, resp.status_code, resp.text)
        assert resp.status_code == 404, (method, suffix, resp.status_code, resp.text)

    # 历史记录/列表接口仍可用
    subagents = await client.get(
        f"{API}/roles/doomed/subagents", headers=human_headers())
    assert subagents.status_code == 200, subagents.text
    runtimes = await client.get(f"{API}/roles/runtime", headers=human_headers())
    assert runtimes.status_code == 200, runtimes.text


@pytest.mark.asyncio
async def test_task_and_run_interfaces_after_role_deletion(client, app):
    """任务/运行相关接口在角色删除后要么正常工作，要么明确 4xx。"""
    await _seed_runtime_and_role(client)
    task = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-DELETED", assignees=["doomed"]),
        headers=human_headers(),
    )
    assert task.status_code == 201, task.text
    source = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-SOURCE", assignees=["doomed"]),
        headers=human_headers(),
    )
    assert source.status_code == 201, source.text

    _insert_run(app, "R-GHOST", "ghost", "suspended", task_id=None)
    _insert_run(app, "R-DONE", "doomed", "completed", task_id="T-DELETED")
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE id='T-DELETED'")
        conn.commit()
    finally:
        conn.close()

    deleted = await client.delete(f"{API}/roles/doomed", headers=human_headers())
    assert deleted.status_code == 204, deleted.text

    # 创建/绑定已删除角色应 4xx
    create_with_deleted = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-NEW", assignees=["doomed"]),
        headers=human_headers(),
    )
    assert 400 <= create_with_deleted.status_code < 500, create_with_deleted.text

    assign_deleted = await client.post(
        f"{API}/tasks/T-DELETED/assign",
        json={"roles": ["doomed"]},
        headers=human_headers(),
    )
    assert 400 <= assign_deleted.status_code < 500, assign_deleted.text

    start_deleted = await client.post(
        f"{API}/runs",
        json={"role_slug": "doomed"},
        headers=human_headers(),
    )
    assert 400 <= start_deleted.status_code < 500, start_deleted.text

    dispatch_deleted = await client.post(
        f"{API}/tasks/T-DELETED/dispatch", headers=human_headers())
    assert 400 <= dispatch_deleted.status_code < 500, dispatch_deleted.text

    dispatch_child = await client.post(
        f"{API}/tasks/T-SOURCE/dispatches",
        json={"target_role": "doomed", "message": "hello"},
        headers=human_headers(),
    )
    assert 400 <= dispatch_child.status_code < 500, dispatch_child.text

    before_mention = _count_runs(app, "doomed")
    mention = await client.post(
        f"{API}/tasks/T-DELETED/mention",
        json={"target": "doomed", "message": "hello"},
        headers=human_headers(),
    )
    assert mention.status_code == 200, mention.text
    assert _count_runs(app, "doomed") == before_mention

    # 任务读取接口仍可工作
    for path in (
        f"{API}/tasks",
        f"{API}/tasks/T-DELETED",
        f"{API}/tasks/status-summary?task_ids=T-DELETED",
        f"{API}/tasks/role-states?task_ids=T-DELETED",
    ):
        resp = await client.get(path, headers=human_headers())
        assert resp.status_code == 200, (path, resp.status_code, resp.text)
    # 没有 workflow 的任务返回明确 404，而不是 500。
    workflow = await client.get(
        f"{API}/tasks/T-DELETED/workflow", headers=human_headers())
    assert workflow.status_code in (200, 404), workflow.text

    # active 列表过滤缺失角色；历史 run 仍可读取
    active = await client.get(f"{API}/runs?active=true", headers=human_headers())
    assert active.status_code == 200
    assert all(r["id"] != "R-GHOST" for r in active.json())
    history = await client.get(f"{API}/runs", headers=human_headers())
    assert history.status_code == 200
    assert any(r["id"] == "R-GHOST" for r in history.json())
    run = await client.get(f"{API}/runs/R-DONE", headers=human_headers())
    assert run.status_code == 200


def _count_runs(app, role: str) -> int:
    conn = sqlite3.connect(app.state.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE role_slug=?", (role,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_startup_repair_backfills_snapshot_and_abandons_orphans(client, app):
    """启动清理从 role_versions 回填快照，并把无角色活跃 run 标 abandoned。"""
    await _seed_runtime_and_role(client)
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO role_versions "
            "(slug, version, display_name, definition_json, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("ghost", 1, "幽灵角色",
             '{"avatar_color":"#abc","avatar":"initials"}', "bootstrap", _now()),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("T-GHOST", "历史任务", "done", "ghost", _now(), _now()),
        )
        conn.execute(
            "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            ("R-GHOST", "ghost", "T-GHOST", "suspended", _now()),
        )
        conn.commit()
    finally:
        conn.close()

    from anvil.db import open_conn
    from anvil.main import _cleanup_orphans
    conn = open_conn(app.state.db_path)
    try:
        _cleanup_orphans(conn)
        snap = conn.execute(
            "SELECT display_name, avatar_color, avatar FROM role_snapshots "
            "WHERE slug='ghost'").fetchone()
        assert tuple(snap) == ("幽灵角色", "#abc", "initials")
        run = conn.execute(
            "SELECT status, ended_at, failure_class FROM runs WHERE id='R-GHOST'"
        ).fetchone()
        assert run[0] == "abandoned"
        assert run[1]
        assert run[2] == "role_missing"
    finally:
        conn.close()

    historical = await client.get(f"{API}/roles?include_deleted=true")
    assert historical.status_code == 200
    assert any(r["slug"] == "ghost" and r["deleted"] for r in historical.json())
