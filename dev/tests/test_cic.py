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


def test_cic_isolates_task_scoped_knowledge(tmp_path, monkeypatch):
    """任务 A 的快照/决策不进入任务 B 或无任务 CIC；role 知识跨任务可见。"""
    data = tmp_path / "data"
    (data / "agents" / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    now = now_iso()
    for tid, title in (("TA", "Auth JWT audit"), ("TB", "Admin JWT console")):
        conn.execute(
            "INSERT INTO tasks (id,title,status,tags_json,repo,created_at,updated_at) "
            "VALUES (?,?,'in_progress','[\"jwt\"]','openssl',?,?)",
            (tid, title, now, now))
    conn.commit()
    memory.write_note(
        conn, "dev", "knowledge/TA/snapshot.md", "TASNAPSHOTUNIQUE jwt strict",
        kind="task_snapshot", scope="task", task_id="TA")
    memory.write_note(
        conn, "dev", "knowledge/TA/decision.md", "TADECISIONUNIQUE jwt 采用 strict",
        kind="decision", scope="task", task_id="TA")
    memory.write_note(
        conn, "dev", "vulns/jwt.md", "ROLEPATTERNUNIQUE jwt algorithm confusion",
        kind="vulnerability_pattern", scope="role", tags=["jwt"], repo="openssl")
    conn.execute(
        "INSERT INTO lessons (id, content, tags_json, source_task, repo, status, created_at) "
        "VALUES ('L1','LESSONUNIQUE 先验证算法白名单','[\"jwt\"]','TA','openssl','approved',?)",
        (now,))
    conn.commit()

    mgr = DaemonManager(db_path, enabled=False)
    cic_a = mgr._build_cic(conn, "dev", "TA")
    cic_b = mgr._build_cic(conn, "dev", "TB")
    cic_global = mgr._build_cic(conn, "dev", None)

    assert "TASNAPSHOTUNIQUE" in cic_a and "TADECISIONUNIQUE" in cic_a
    assert "TASNAPSHOTUNIQUE" not in cic_b and "TADECISIONUNIQUE" not in cic_b
    assert "TASNAPSHOTUNIQUE" not in cic_global and "TADECISIONUNIQUE" not in cic_global
    assert "ROLEPATTERNUNIQUE" in cic_a
    assert "ROLEPATTERNUNIQUE" in cic_b
    assert "ROLEPATTERNUNIQUE" in cic_global
    assert "## 共享经验（已批准）" in cic_a and "LESSONUNIQUE" in cic_a
    assert "## 知识库索引" in cic_a
    assert "重置" not in cic_a and "重置" not in cic_b  # 三负规则
    conn.close()


def test_cic_budget_keeps_memory_snapshot_and_messages(tmp_path, monkeypatch):
    """CIC 总长受限；预算紧张时仍保留 MEMORY、任务快照与最近消息。"""
    data = tmp_path / "data"
    home = data / "agents" / "dev"
    home.mkdir(parents=True)
    (home / "MEMORY.md").write_text(
        ("填充" * 4000) + "\nMEMORYTAILUNIQUE\n", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','预算任务','in_progress',?,?)", (now, now))
    conn.commit()
    memory.write_note(
        conn, "dev", "knowledge/T1/snap.md",
        "SNAPSHOTHEADUNIQUE\n" + ("快照" * 8000) + "\nSNAPSHOTTAIL",
        kind="task_snapshot", scope="task", task_id="T1")
    tasks_service.comment(conn, "T1", "pm", "MSG0:" + "长消息" * 200)
    for i in range(1, 40):
        tasks_service.comment(conn, "T1", "pm", f"MSG{i}:" + "长消息" * 200)

    mgr = DaemonManager(db_path, enabled=False)
    cic = mgr._build_cic(conn, "dev", "T1")
    assert len(cic) <= 32000
    assert "MEMORYTAILUNIQUE" in cic
    assert "SNAPSHOTHEADUNIQUE" in cic
    assert "## 最近 25 条消息" in cic and "MSG39:" in cic
    assert "## 最近动作" in cic or "## 最近 25 条消息" in cic
    conn.close()


def test_pack_blocks_priority_order_and_tail_truncation():
    """低优先级块先丢；输出按 order；最近消息用 tail 截断保留最新。"""
    from anvil.services.knowledge import pack_blocks

    blocks = [
        {"priority": 10, "order": 10, "cap": 20, "min": 5, "text": "MEMORY"},
        {"priority": 20, "order": 30, "cap": 20, "min": 5, "text": "SNAPSHOT"},
        {"priority": 30, "order": 70, "cap": 20, "min": 5, "text": "MESSAGES"},
        {"priority": 80, "order": 80, "cap": 37, "min": 30, "text": "ACTIONS" + "A" * 30},
        {"priority": 70, "order": 20, "cap": 35, "min": 30, "text": "INDEX" + "I" * 30},
        {"priority": 60, "order": 60, "cap": 37, "min": 30, "text": "LESSONS" + "L" * 30},
    ]
    out = pack_blocks(blocks, 35)
    assert "MEMORY" in out and "SNAPSHOT" in out and "MESSAGES" in out
    assert "ACTIONS" not in out and "INDEX" not in out and "LESSONS" not in out

    ordered = pack_blocks(blocks, 200)
    assert ordered.index("MEMORY") < ordered.index("INDEX") < ordered.index("SNAPSHOT")
    assert ordered.index("SNAPSHOT") < ordered.index("MESSAGES")

    tail = pack_blocks([{
        "priority": 10, "order": 10, "cap": 14, "min": 1,
        "truncate": "tail", "text": "## MSG\nOLD\nNEWEST",
    }], 14)
    assert tail.startswith("## MSG")
    assert "NEWEST" in tail and "OLD" not in tail


def test_cic_knowledge_index_lists_up_to_eight_titles(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "agents" / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','索引任务','in_progress',?,?)", (now, now))
    conn.commit()
    for i in range(8):
        memory.write_note(
            conn, "dev", f"notes/idx{i}.md", f"INDEXNOTE{i}",
            kind="note", scope="role")
    cic = DaemonManager(db_path, enabled=False)._build_cic(conn, "dev", "T1")
    assert "## 知识库索引" in cic
    assert "idx7.md" in cic and "idx0.md" in cic
    conn.close()


def test_extract_usage_fallbacks():
    from anvil.services.daemon import extract_turn_usage, extract_usage
    assert extract_usage({"usage": {"input_tokens": 5, "output_tokens": 2}})["input_tokens"] == 5
    agg = extract_usage({"modelUsage": {
        "m1": {"inputTokens": 10, "outputTokens": 3},
        "m2": {"inputTokens": 2, "outputTokens": 1},
    }})
    assert agg["input_tokens"] == 12 and agg["output_tokens"] == 4
    # result 事件里的 since-last-result 才带真实 output；assistant usage 的 output 为 0。
    turn = extract_turn_usage({
        "usage": {"input_tokens": 999, "output_tokens": 0},
        "model_usage_since_last_result": {
            "m1": {"inputTokens": 100, "outputTokens": 7,
                   "cacheReadInputTokens": 3, "cacheCreationInputTokens": 1},
            "m2": {"inputTokens": 50, "outputTokens": 2,
                   "cacheReadInputTokens": 4, "cacheCreationInputTokens": 0},
        },
    })
    assert turn == {
        "input_tokens": 150, "output_tokens": 9,
        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1,
    }
    assert extract_turn_usage({"usage": {"input_tokens": 5, "output_tokens": 2}})["output_tokens"] == 2
