"""知识快照：上下文被清理前把任务的决策/进度写入 notes 并建立 FTS 索引。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import memory
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


@pytest.fixture
def env(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,created_at,updated_at) "
        "VALUES ('T1','OpenSSL audit','in_progress','[\"security\"]',?,?)", (now, now))
    conn.execute(
        "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
        "VALUES ('W1','T1','{}','running',?,?)", (now, now))
    conn.execute(
        "INSERT INTO workflow_steps "
        "(workflow_id,step_id,title,role_slug,status,summary,updated_at) "
        "VALUES ('W1','S1','基线核查','dev','approved','S1 通过：72 字段一致',?)", (now,))
    conn.execute(
        "INSERT INTO workflow_steps "
        "(workflow_id,step_id,title,role_slug,status,summary,feedback,updated_at) "
        "VALUES ('W1','S2','PoC 验证','dev','running',NULL,NULL,?)", (now,))
    conn.commit()
    tasks_service.comment(conn, "T1", "dev", "结论：采用 strict 口径，禁止 RSS-only")
    tasks_service.mention(conn, "T1", "pm", "dev", "请确认 S2 的验收口径")
    yield conn, agents
    conn.close()


def test_consolidate_context_writes_indexed_snapshot(env):
    conn, agents = env
    snap = memory.consolidate_context(conn, "dev", "T1", "context-high",
                                      run_id="run-12345678")
    assert snap is not None
    assert snap["path"].startswith("knowledge/")
    body = (agents / "dev" / "notes" / snap["path"]).read_text(encoding="utf-8")
    assert "OpenSSL audit" in body
    assert "S1 通过：72 字段一致" in body  # 已批准步骤的决策摘要
    assert "strict 口径" in body           # 角色自己的结论
    assert "请确认 S2" in body             # 其他角色的交接
    assert "context-high" in body          # 触发原因可追溯

    # 写入 notes/FTS，清理上下文后可以检索到
    hits = memory.search_memory(conn, "dev", "strict", limit=5)
    assert any("strict 口径" in h["content"] for h in hits)
    audit = conn.execute(
        "SELECT 1 FROM audit WHERE action='session.context_digest' AND role_slug='dev'"
    ).fetchone()
    assert audit is not None


def test_role_long_term_memory_is_isolated(env):
    """角色私有长期记忆互不可见；任务会话/共享 lesson 是显式共享输入。"""
    from anvil.services.daemon import DaemonManager

    conn, agents = env
    memory.append_memory(conn, "dev", "OpenSSL audit devprivatemarker")
    memory.write_note(conn, "tester", "notes/t.md",
                      "OpenSSL audit testerprivatemarker")

    # FTS 检索按 role_slug 隔离
    dev_hits = memory.search_memory(conn, "dev", "testerprivatemarker")
    assert dev_hits == []
    tester_hits = memory.search_memory(conn, "tester", "devprivatemarker")
    assert tester_hits == []

    # 文件也是各角色 home 独立
    assert "devprivatemarker" in (agents / "dev" / "MEMORY.md").read_text()
    tester_memory = agents / "tester" / "MEMORY.md"
    if tester_memory.exists():
        assert "devprivatemarker" not in tester_memory.read_text()

    mgr = DaemonManager(str(agents.parent / "t.db"), enabled=False)
    dev_cic = mgr._build_cic(conn, "dev", "T1")
    tester_cic = mgr._build_cic(conn, "tester", "T1")
    assert "devprivatemarker" in dev_cic
    assert "devprivatemarker" not in tester_cic
    assert "testerprivatemarker" in tester_cic
    assert "testerprivatemarker" not in dev_cic


def test_search_memory_excludes_shared_rows(env):
    """FTS 检索严格按角色隔离；共享知识只走人工批准的 lessons 通道。"""
    conn, _ = env
    conn.execute(
        "INSERT INTO memory_fts (role_slug, content) VALUES ('shared','sharedprivatemarker')")
    conn.commit()
    assert memory.search_memory(conn, "dev", "sharedprivatemarker") == []


def test_consolidate_context_requires_task(env):
    conn, _ = env
    assert memory.consolidate_context(conn, "dev", None, "threshold") is None
    assert memory.consolidate_context(conn, "dev", "missing", "threshold") is None
