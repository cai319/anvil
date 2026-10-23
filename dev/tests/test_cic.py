"""CIC：冷唤醒构造上下文（MEMORY 头 / 最近消息 / 最近动作），不宣布重置。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import memory
from anvil.services import runs as runs_service
from anvil.services import tasks as tasks_service
from anvil.services.daemon import DaemonManager
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


def test_build_cic_blocks_real(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "agents" / "dev").mkdir(parents=True)
    (data / "agents" / "dev" / "MEMORY.md").write_text("# 长记忆\n关键事实：使用 Python", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) VALUES ('T1','t','in_progress',?,?)",
                 (now, now))
    conn.commit()
    tasks_service.comment(conn, "T1", "pm", "接口有变更，请确认")
    tasks_service.mention(conn, "T1", "tester", "dev", "请复核")
    conn.execute("INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                 "VALUES ('dev','T1','submit','{}',?)", (now,))
    conn.commit()

    mgr = DaemonManager(db_path, enabled=False)
    text = mgr._build_cic(conn, "dev", "T1")
    assert "关键事实：使用 Python" in text
    assert "## 最近 25 条消息" in text and "接口有变更" in text
    assert "## 最近动作" in text and "submit" in text
    assert "重置" not in text  # 三负规则：不宣布重置
    conn.close()


def test_build_cic_includes_long_term_knowledge(tmp_path, monkeypatch):
    """CIC 要带长期知识：MEMORY 尾部、任务相关记忆、工作流决策、最近知识快照。"""
    data = tmp_path / "data"
    home = data / "agents" / "dev"
    home.mkdir(parents=True)
    # MEMORY 头部是很久以前的内容；CIC 应取尾部（最近整理过的知识）
    (home / "MEMORY.md").write_text(
        "HEAD-MARK\n" + ("填充" * 3000) + "\nTAIL-MARK\n", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")

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
    conn.commit()
    memory.write_note(conn, "dev", "notes/relevant.md",
                      "OpenSSL audit policy: strict only")
    memory.write_note(conn, "dev", "knowledge/T1/snap.md",
                      "# 任务知识快照\n- 结论：strict 口径\n")

    mgr = DaemonManager(db_path, enabled=False)
    text = mgr._build_cic(conn, "dev", "T1")
    assert "TAIL-MARK" in text and "HEAD-MARK" not in text
    assert "## 相关记忆" in text and "strict only" in text
    assert "## 工作流进度" in text and "S1 通过：72 字段一致" in text
    assert "## 最近知识快照" in text and "strict 口径" in text
    assert "重置" not in text  # 三负规则：CIC 不宣布重置
    conn.close()


def test_extract_usage_fallbacks():
    from anvil.services.daemon import extract_usage
    assert extract_usage({"usage": {"input_tokens": 5, "output_tokens": 2}})["input_tokens"] == 5
    agg = extract_usage({"modelUsage": {
        "m1": {"inputTokens": 10, "outputTokens": 3},
        "m2": {"inputTokens": 2, "outputTokens": 1},
    }})
    assert agg["input_tokens"] == 12 and agg["output_tokens"] == 4
