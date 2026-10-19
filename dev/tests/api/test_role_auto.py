"""角色一句话生成：从自然人描述生成能力/定位/职责/系统提示并直接创建。"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import auth_headers  # noqa: E402

GEN = {
    "display_name": "后端工程师",
    "description": "负责后端 API、数据库与性能优化",
    "working_mode": "自主推进，主动同步进展",
    "skill_description": "Python/FastAPI/SQL",
    "capabilities": {"python": 0.9, "sql": 0.85},
    "responsibilities": ["API 设计", "数据库迁移", "性能优化"],
    "system_prompt": "你是后端工程师，主动用 anvil task comment 同步进展。",
}


async def test_auto_create_role_from_brief(client, monkeypatch):
    monkeypatch.setattr("anvil.api.roles.llm.generate", lambda prompt: json.dumps(GEN))
    r = await client.post("/api/v1/roles/auto", headers=auth_headers(),
                          json={"brief": "负责后端 API 和数据库的工程师"})
    assert r.status_code == 201, r.text
    role = r.json()
    assert role["display_name"] == "后端工程师"
    slug = role["slug"]
    got = await client.get(f"/api/v1/roles/{slug}", headers=auth_headers())
    assert got.status_code == 200
    definition = got.json().get("definition") or {}
    assert definition["system_prompt"].startswith("你是后端工程师")
    assert definition["responsibilities"] == GEN["responsibilities"]
    assert definition["capabilities"]["python"] == 0.9


async def test_auto_create_role_requires_brief(client, monkeypatch):
    r = await client.post("/api/v1/roles/auto", headers=auth_headers(), json={"brief": ""})
    assert r.status_code == 422
