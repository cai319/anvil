"""MEMORY.md → PRINCIPLES.md + 经验卡：去重、预览、归档。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import memory


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    agents = data / "agents"
    (agents / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    conn = connect(str(tmp_path / "t.db"))
    yield conn, agents / "dev"
    conn.close()


def test_compact_dry_run_writes_nothing(env):
    conn, home = env
    (home / "MEMORY.md").write_text(
        "## 工作准则\n- pytest 前必须 docker compose up -d db\n"
        "- pytest 前必须 docker compose up -d db\n\n"
        "成功实践：提交前先跑 pytest\n", encoding="utf-8")
    result = memory.migrate_memory_file(conn, "dev", dry_run=True)
    assert result["dry_run"] is True
    assert result["cards"] >= 1
    assert not (home / "PRINCIPLES.md").exists()
    assert list((home / "memory").glob("archive/*")) == []


def test_compact_splits_dedupes_and_archives(env):
    conn, home = env
    (home / "MEMORY.md").write_text(
        "## 工作准则\n"
        "- pytest 前必须 docker compose up -d db\n"
        "- pytest 前必须 docker compose up -d db\n"
        "- 提交前必须跑 pytest\n\n"
        "## 任务记录\n"
        "成功实践：提交前先跑 pytest\n"
        "成功实践：提交前先跑 pytest\n"
        "任务 A 的具体结论：使用 strict 口径。\n", encoding="utf-8")

    result = memory.migrate_memory_file(conn, "dev", dry_run=False)
    principles = (home / "PRINCIPLES.md").read_text(encoding="utf-8")
    assert "docker compose up -d db" in principles
    assert principles.count("提交前必须跑 pytest") == 1
    assert len(principles) <= config.MEMORY_PRINCIPLES_MAX_CHARS
    assert result["cards_created"] >= 1
    assert list((home / "memory" / "archive").glob("MEMORY-*.md"))
    pointer = (home / "MEMORY.md").read_text(encoding="utf-8")
    assert "已迁移" in pointer and "PRINCIPLES.md" in pointer

    cards = list((home / "notes" / "memory" / "cards").glob("*.md"))
    assert cards
    # 角色知识检索能命中迁移出的卡片。
    hits = memory.search_memory(conn, "dev", "strict")
    assert any("strict" in (h.get("content") or "") for h in hits)


def test_append_memory_dedupes_identical_cards(env):
    conn, home = env
    first = memory.append_memory(conn, "dev", "同一条经验：先跑测试")
    second = memory.append_memory(conn, "dev", "同一条经验：先跑测试")
    assert first["deduped"] is False
    assert second["deduped"] is True
    cards = list((home / "notes" / "memory" / "cards").glob("*.md"))
    assert len(cards) == 1


def test_append_durable_writes_principles(env):
    conn, home = env
    out = memory.append_memory(conn, "dev", "必须使用只读 Token", durable=True)
    assert out["status"] == "principles"
    assert "必须使用只读 Token" in (home / "PRINCIPLES.md").read_text(encoding="utf-8")
