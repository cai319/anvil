"""切片上下文：context key、DB 迁移、静态切片 key 与 CIC 隔离。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil import db as db_module
from anvil.db import connect
from anvil.services import daemon as daemon_service
from anvil.services import tasks as tasks_service
from anvil.services import workflow as workflow_service
from anvil.util import jdumps, now_iso

pytest.importorskip("anvil.graph")


def test_context_key_roundtrip():
    assert daemon_service.context_key(None) == "__global__"
    assert daemon_service.context_key("T1") == "T1"
    assert daemon_service.context_key("T1", "A") == "T1::slice::A"
    assert daemon_service.context_parts("T1::slice::A") == ("T1", "A")
    assert daemon_service.context_parts("T1") == ("T1", None)
    assert daemon_service.context_parts("__global__") == (None, None)
    assert daemon_service.task_id_of("T1::slice::A") == "T1"
    assert daemon_service.slice_key_of("T1::slice::A") == "A"
    assert daemon_service.slice_key_of("T1") is None


def test_task_id_cannot_collide_with_context_separator(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = connect(str(tmp_path / "t.db"))
    try:
        with pytest.raises(Exception):
            tasks_service.create(conn, id="T::slice::A", title="bad")
    finally:
        conn.close()


def test_static_slices_are_isolated_without_dynamic_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = connect(str(tmp_path / "t.db"))
    try:
        definition = {
            "slices": [
                {
                    "id": "A",
                    "title": "模块 A",
                    "execute_role": "dev",
                    "verify_role": "qa",
                    "decision_role": "lead",
                },
                {
                    "id": "B",
                    "title": "模块 B",
                    "execute_role": "dev",
                    "verify_role": "qa",
                    "decision_role": "lead",
                },
            ]
        }
        expanded = workflow_service._expand_slices(definition)
        assert expanded["steps"]
        for step in expanded["steps"]:
            assert step["slice_key"] in ("A", "B")
            # source_step_id 只用于识别运行期动态切片，静态切片不能设置。
            assert not step.get("source_step_id")
        a_steps = {s["slice_key"] for s in expanded["steps"] if s["slice_key"] == "A"}
        assert a_steps == {"A"}
        assert {s["id"] for s in expanded["steps"] if s["slice_key"] == "A"} == {
            "A.scope", "A.execute", "A.verify", "A.fix", "A.verify2", "A.decide"}
    finally:
        conn.close()


def test_static_slice_rejects_invalid_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = connect(str(tmp_path / "t.db"))
    try:
        with pytest.raises(Exception):
            workflow_service._expand_slices({"slices": [{
                "id": "A::slice::B",
                "execute_role": "dev", "verify_role": "qa",
                "decision_role": "lead",
            }]})
    finally:
        conn.close()


def test_migration_backfills_slice_context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "data" / "agents")
    conn = connect(str(tmp_path / "t.db"))
    try:
        now = now_iso()
        task = tasks_service.create(conn, title="migration task")
        tid = task["id"]
        conn.execute(
            "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
            "VALUES ('W1',?,'{}','running',?,?)", (tid, now, now))
        conn.execute(
            "INSERT INTO workflow_steps "
            "(workflow_id,step_id,title,role_slug,status,updated_at) "
            "VALUES ('W1','A.scope','范围','dev','pending',?)", (now,))
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,workflow_id,step_id,started_at) "
            "VALUES ('R1','dev',?,'suspended','W1','A.scope',?)", (tid, now))
        conn.execute(
            "INSERT INTO role_task_sessions "
            "(role_slug,task_key,session_id,status,created_at,updated_at) "
            "VALUES ('dev',?,'s1','idle',?,?)", (tid, now, now))
        conn.execute(
            "INSERT INTO role_task_sessions "
            "(role_slug,task_key,session_id,status,created_at,updated_at) "
            "VALUES ('dev',?,'s2','idle',?,?)",
            (f"{tid}::slice::B", now, now))
        conn.commit()

        # 回到升级前状态，再跑一次迁移。
        conn.execute("DELETE FROM settings WHERE key='schema.slice_context_v1'")
        conn.execute("UPDATE workflow_steps SET slice_key=NULL, source_step_id=NULL")
        conn.execute("UPDATE runs SET slice_key=NULL")
        conn.execute("UPDATE role_task_sessions SET task_id=NULL, slice_key=NULL")
        conn.commit()
        db_module._migrate_slice_context_v1(conn)

        step = conn.execute(
            "SELECT slice_key FROM workflow_steps WHERE step_id='A.scope'").fetchone()
        assert step["slice_key"] == "A"
        run = conn.execute("SELECT slice_key FROM runs WHERE id='R1'").fetchone()
        assert run["slice_key"] == "A"
        rows = {
            r["task_key"]: (r["task_id"], r["slice_key"])
            for r in conn.execute(
                "SELECT task_key, task_id, slice_key FROM role_task_sessions").fetchall()
        }
        assert rows[tid] == (tid, None)
        assert rows[f"{tid}::slice::B"] == (tid, "B")

        # 幂等：重复迁移不改变结果。
        db_module._migrate_slice_context_v1(conn)
        rows2 = {
            r["task_key"]: (r["task_id"], r["slice_key"])
            for r in conn.execute(
                "SELECT task_key, task_id, slice_key FROM role_task_sessions").fetchall()
        }
        assert rows2 == rows
    finally:
        conn.close()


def test_restart_clears_only_slice_slots(tmp_path, monkeypatch):
    """新 execution 不复用旧 slice 会话，但保留 task 级上下文。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "data" / "agents")
    conn = connect(str(tmp_path / "t.db"))
    try:
        now = now_iso()
        tid = tasks_service.create(conn, title="restart task")["id"]
        conn.execute(
            "INSERT INTO role_task_sessions "
            "(role_slug,task_key,task_id,slice_key,session_id,status,created_at,updated_at) "
            "VALUES ('dev',? ,?,NULL,'s-task','idle',?,?)", (tid, tid, now, now))
        conn.execute(
            "INSERT INTO role_task_sessions "
            "(role_slug,task_key,task_id,slice_key,session_id,status,created_at,updated_at) "
            "VALUES ('dev',?,?,'A','s-a','idle',?,?)",
            (f"{tid}::slice::A", tid, now, now))
        conn.execute(
            "INSERT INTO role_sessions "
            "(role_slug,session_id,status,current_task_id,current_context_key,"
            " current_slice_key,updated_at) VALUES ('dev','s-a','busy',?,?,?,?)",
            (tid, f"{tid}::slice::A", "A", now))
        conn.commit()

        workflow_service._clear_restarted_slice_contexts(conn, tid)

        rows = {
            r["task_key"]: r["session_id"]
            for r in conn.execute(
                "SELECT task_key, session_id FROM role_task_sessions "
                "WHERE role_slug='dev'").fetchall()
        }
        assert rows == {tid: "s-task"}
        mirror = conn.execute(
            "SELECT session_id, current_context_key, current_slice_key "
            "FROM role_sessions WHERE role_slug='dev'").fetchone()
        assert mirror["session_id"] == ""
        assert mirror["current_context_key"] is None
        assert mirror["current_slice_key"] is None
    finally:
        conn.close()


def test_cic_excludes_other_slice_raw_messages(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "agents" / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    conn = connect(str(tmp_path / "t.db"))
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T1','t','in_progress',?,?)", (now, now))
        for kind, body, slice_key in (
            ("comment", "A-only-message", "A"),
            ("comment", "B-secret-message", "B"),
            ("comment", "TASK-WIDE-MESSAGE", None),
        ):
            conn.execute(
                "INSERT INTO activities "
                "(task_id,role_slug,kind,payload_json,slice_key,created_at) "
                "VALUES ('T1','dev',?,?,?,?)",
                (kind, jdumps({"body": body}), slice_key, now))
        conn.commit()

        mgr = daemon_service.DaemonManager(str(tmp_path / "t.db"), enabled=False)
        text = mgr._build_cic(conn, "dev", "T1", slice_key="A")
        assert "当前切片：A" in text
        assert "A-only-message" in text
        assert "TASK-WIDE-MESSAGE" in text
        assert "B-secret-message" not in text

        b_text = mgr._build_cic(conn, "dev", "T1", slice_key="B")
        assert "B-secret-message" in b_text
        assert "A-only-message" not in b_text
    finally:
        conn.close()


def test_migration_v2_adds_note_slice_and_inbox_scope(tmp_path, monkeypatch):
    """v1 已跑过的旧库也必须补 notes.slice_key / inbox slice 维度。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = connect(str(tmp_path / "t.db"))
    try:
        # 模拟 v2 之前的旧表结构。
        conn.execute("DELETE FROM settings WHERE key='schema.slice_context_v2'")
        conn.execute("DROP TABLE IF EXISTS role_inbox_cursors")
        conn.execute(
            "CREATE TABLE role_inbox_cursors ("
            " role_slug TEXT NOT NULL, task_id TEXT NOT NULL,"
            " last_activity_id INTEGER NOT NULL DEFAULT 0,"
            " updated_at TEXT NOT NULL, PRIMARY KEY (role_slug, task_id))")
        conn.execute(
            "INSERT INTO role_inbox_cursors "
            "(role_slug,task_id,last_activity_id,updated_at) VALUES ('dev','T1',7,?)",
            (now_iso(),))
        conn.commit()
        db_module._migrate_slice_context_v2(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
        assert "slice_key" in cols
        cols = {r[1] for r in conn.execute("PRAGMA table_info(role_inbox_cursors)")}
        assert "slice_key" in cols
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert {"recovery_count", "recovered_by", "recovery_of_run_id",
                "recovery_exhausted"} <= run_cols
        row = conn.execute(
            "SELECT last_activity_id FROM role_inbox_cursors "
            "WHERE role_slug='dev' AND task_id='T1' AND slice_key=''").fetchone()
        assert row["last_activity_id"] == 7
        # 幂等
        db_module._migrate_slice_context_v2(conn)
    finally:
        conn.close()


def test_delivery_priority_wires_return_step_findings_mention(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = connect(str(tmp_path / "t.db"))
    try:
        from anvil.graph import nodes as graph_nodes

        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T1','t','in_progress',?,?)", (now, now))
        row = {"id": "R1", "step_id": "S1", "origin": None,
               "prompt": None, "wake_note": None, "dispatch_id": None}
        assert graph_nodes._delivery_priority(conn, row) == 20
        row = {"id": "R1", "step_id": None, "origin": "mention",
               "prompt": None, "wake_note": None, "dispatch_id": None}
        assert graph_nodes._delivery_priority(conn, row) == 50
        row = {"id": "R1", "step_id": None, "origin": "findings",
               "prompt": "[anvil:findings] x", "wake_note": None,
               "dispatch_id": None}
        assert graph_nodes._delivery_priority(conn, row) == 40

        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,started_at,wake_note) "
            "VALUES ('R1','dev','T1','suspended',?,'result')", (now,))
        conn.execute(
            "INSERT INTO task_dispatches "
            "(id,source_task_id,target_task_id,target_role,return_run_id,"
            " reason_code,reason_json,status,created_at,updated_at) "
            "VALUES ('D1','T1','T2','dev','R1','auto','{}','returning',?,?)",
            (now, now))
        conn.commit()
        row = {"id": "R1", "step_id": "S1", "origin": "step",
               "prompt": None, "wake_note": "result", "dispatch_id": None}
        assert graph_nodes._delivery_priority(conn, row) == 10
    finally:
        conn.close()
