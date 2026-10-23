"""角色头像：创建持久化、独立更新、校验与旧库迁移。"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("anvil.main")
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


async def test_create_role_with_avatar(client):
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


async def test_auto_create_role_with_avatar(client, monkeypatch):
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
    raw.commit()
    raw.close()

    conn = connect(str(path))
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(roles)")}
        assert "avatar" in cols
    finally:
        conn.close()
