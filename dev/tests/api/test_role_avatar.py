"""角色头像：创建持久化、独立更新、校验与旧库迁移。"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("anvil.main")
from anvil.db import open_conn  # noqa: E402
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload  # noqa: E402

GEN = {
    "display_name": "头像测试角色",
    "description": "测试头像",
    "working_mode": "自主",
    "skill_description": "测试",
    "capabilities": {},
    "responsibilities": ["测试"],
    "system_prompt": "你是测试角色。",
}


async def test_create_role_with_avatar(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    r = await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    got = await client.get(f"{API}/roles/dev")
    assert got.status_code == 200
    assert got.json()["avatar"] == "big-smile:ace"
    assert got.json()["definition"]["avatar"] == "big-smile:ace"

    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT avatar FROM roles WHERE slug='dev'").fetchone()
        assert row is not None
        assert row["avatar"] == "big-smile:ace"
    finally:
        conn.close()


async def test_auto_create_role_with_avatar(client, app, monkeypatch):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    monkeypatch.setattr(
        "anvil.api.roles.utility.generate",
        lambda conn, prompt, **kwargs: json.dumps(GEN, ensure_ascii=False),
    )
    r = await client.post(
        f"{API}/roles/auto",
        json={"brief": "头像测试", "avatar": "big-smile:bee"},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["avatar"] == "big-smile:bee"

    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT avatar FROM roles WHERE slug=?", (body["slug"],)).fetchone()
        assert row is not None
        assert row["avatar"] == "big-smile:bee"
    finally:
        conn.close()


async def test_create_role_rejects_invalid_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    r = await client.post(
        f"{API}/roles",
        json=role_payload("bad-avatar", avatar="javascript:alert(1)"),
        headers=human_headers(),
    )
    assert r.status_code == 422, r.text


async def test_create_role_accepts_initials_and_null(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    r1 = await client.post(
        f"{API}/roles",
        json=role_payload("dev-initials", avatar="initials"),
        headers=human_headers(),
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["avatar"] == "initials"

    r2 = await client.post(
        f"{API}/roles",
        json=role_payload("dev-null"),
        headers=human_headers(),
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["avatar"] is None

    conn = open_conn(app.state.db_path)
    try:
        initials = conn.execute(
            "SELECT avatar FROM roles WHERE slug='dev-initials'").fetchone()
        assert initials["avatar"] == "initials"
        null_avatar = conn.execute(
            "SELECT avatar FROM roles WHERE slug='dev-null'").fetchone()
        assert null_avatar["avatar"] is None
    finally:
        conn.close()


async def test_auto_create_role_rejects_invalid_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    r = await client.post(
        f"{API}/roles/auto",
        json={"brief": "非法头像", "avatar": "javascript:alert(1)"},
        headers=human_headers(),
    )
    assert r.status_code == 422, r.text


async def test_auto_create_role_accepts_initials(client, monkeypatch):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    monkeypatch.setattr(
        "anvil.api.roles.utility.generate",
        lambda conn, prompt, **kwargs: json.dumps(GEN, ensure_ascii=False),
    )
    r = await client.post(
        f"{API}/roles/auto",
        json={"brief": "头像 initials", "avatar": "initials"},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "initials"


async def test_update_role_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "big-smile:cid"},
        headers=human_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == "big-smile:cid"

    got = await client.get(f"{API}/roles/dev")
    assert got.json()["avatar"] == "big-smile:cid"


async def test_avatar_accepts_initials_and_null(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar", json={"avatar": "initials"}, headers=human_headers(),
    )
    assert r.status_code == 200
    assert r.json()["avatar"] == "initials"

    r = await client.put(
        f"{API}/roles/dev/avatar", json={"avatar": None}, headers=human_headers(),
    )
    assert r.status_code == 200
    assert r.json()["avatar"] is None


async def test_invalid_avatar_is_rejected(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "javascript:alert(1)"},
        headers=human_headers(),
    )
    assert r.status_code == 422


async def test_avatar_update_requires_human(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "big-smile:ace"},
        headers=auth_headers(role="developer"),
    )
    assert r.status_code == 403


async def test_clone_role_preserves_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )
    r = await client.post(f"{API}/roles/dev/clone", headers=human_headers())
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    got = await client.get(f"{API}/roles/dev-clone")
    assert got.json()["avatar"] == "big-smile:ace"


async def test_rollback_keeps_avatar_consistent(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )
    # 第二次配置更新会生成 version 2；随后头像改为 bee。
    await client.put(
        f"{API}/roles/dev",
        json=role_payload("dev", display_name="Dev 2"),
        headers=human_headers(),
    )
    await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "big-smile:bee"},
        headers=human_headers(),
    )
    # 回滚到 version 1 应把 definition 里的 ace 同步回 avatar 列。
    r = await client.post(f"{API}/roles/dev/rollback/1", headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    got = await client.get(f"{API}/roles/dev")
    assert got.json()["avatar"] == "big-smile:ace"


async def test_recreate_role_preserves_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )

    # 同一个 slug 再次创建、但不带头像：保留原头像。
    r = await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    # 再次创建并显式换头像：更新为新值。
    r = await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:bee"),
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:bee"


def test_legacy_roles_table_gets_avatar_column(tmp_path):
    from anvil.db import connect

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE roles ("
        "slug TEXT PRIMARY KEY, display_name TEXT NOT NULL, avatar_color TEXT, "
        "home_path TEXT NOT NULL, definition_json TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1)"
    )
    raw.execute(
        "INSERT INTO roles (slug, display_name, avatar_color, home_path, "
        "definition_json, created_at, updated_at, enabled) "
        "VALUES ('legacy-role', 'Legacy Role', '#123456', '/tmp/legacy', "
        "'{}', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00', 1)"
    )
    raw.commit()
    raw.close()

    conn = connect(str(path))
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(roles)")}
        assert "avatar" in cols
        row = conn.execute(
            "SELECT slug, avatar FROM roles WHERE slug='legacy-role'").fetchone()
        assert row is not None
        assert row["avatar"] is None
    finally:
        conn.close()
