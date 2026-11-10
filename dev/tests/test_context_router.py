"""上下文路由：同切片复用、跨切片切换、无关工作派遣。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import context_router
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


def _set_current(conn, role: str, task_id: str, slice_key: str | None):
    key = context_router._context_key(task_id, slice_key)
    conn.execute(
        "INSERT INTO role_sessions "
        "(role_slug,session_id,status,current_task_id,current_context_key,"
        " current_slice_key,updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(role_slug) DO UPDATE SET session_id=excluded.session_id, "
        "status=excluded.status, current_task_id=excluded.current_task_id, "
        "current_context_key=excluded.current_context_key, "
        "current_slice_key=excluded.current_slice_key, updated_at=excluded.updated_at",
        (role, "sid", "busy", task_id, key, slice_key, now_iso()))
    conn.commit()


def _add_slot(conn, role: str, task_id: str, slice_key: str | None = None):
    key = context_router._context_key(task_id, slice_key)
    now = now_iso()
    conn.execute(
        "INSERT INTO role_task_sessions "
        "(role_slug,task_key,task_id,slice_key,session_id,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'idle',?,?)",
        (role, key, task_id, slice_key, "sid-old", now, now))
    conn.commit()


def _add_slice_role(conn, task_id: str, slice_key: str, role: str):
    now = now_iso()
    wid = f"W-{task_id}"
    conn.execute(
        "INSERT OR IGNORE INTO workflows "
        "(id,task_id,definition_json,status,created_at,updated_at) "
        "VALUES (?,?,'{}','running',?,?)", (wid, task_id, now, now))
    conn.execute(
        "INSERT INTO workflow_steps "
        "(workflow_id,step_id,title,role_slug,status,slice_key,updated_at) "
        "VALUES (?,?,?,?,'pending',?,?)",
        (wid, f"{slice_key}.execute", f"{slice_key} 执行", role, slice_key, now))
    conn.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "data" / "agents")
    from anvil.services import daemon as daemon_service
    monkeypatch.setattr(daemon_service, "get_manager", lambda: None)
    conn = connect(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def test_same_task_slice_reuses_current_context(env):
    conn = env
    task = tasks_service.create(conn, title="模块 B 问题挖掘")
    _add_slice_role(conn, task["id"], "B", "dev")
    _set_current(conn, "dev", task["id"], "B")

    d = context_router.route(
        conn, target_role="dev", source_task_id=task["id"],
        source_slice_key="B", message="B 模块这条 finding 需要复核")
    assert d["mode"] == "reuse"
    assert d["context_key"] == f"{task['id']}::slice::B"
    assert "same_task_slice" in d["reasons"]


def test_known_slice_switches_back(env):
    conn = env
    task = tasks_service.create(conn, title="模块 A/B 挖掘")
    _add_slice_role(conn, task["id"], "A", "dev")
    _add_slice_role(conn, task["id"], "B", "dev")
    _add_slot(conn, "dev", task["id"], "A")
    _set_current(conn, "dev", task["id"], "B")

    d = context_router.route(
        conn, target_role="dev", source_task_id=task["id"],
        source_slice_key="A", message="A 模块上一轮结论有异常")
    assert d["mode"] == "switch"
    assert d["target_slice_key"] == "A"
    assert "known_slice_slot" in d["reasons"]


def test_unrelated_cross_task_dispatches(env):
    conn = env
    source = tasks_service.create(conn, title="OpenSSL 证书审计")
    current = tasks_service.create(conn, title="调试网关超时")
    _add_slice_role(conn, source["id"], "A", "qa")
    _set_current(conn, "dev", current["id"], None)

    d = context_router.route(
        conn, target_role="dev", source_task_id=source["id"],
        source_slice_key="A", message="请帮忙修一下 Redis 缓存击穿问题")
    assert d["mode"] == "dispatch"
    assert "low_relevance_no_owner" in d["reasons"]


def test_slice_role_without_slot_gets_new_slice(env):
    conn = env
    task = tasks_service.create(conn, title="模块 A/B 挖掘")
    _add_slice_role(conn, task["id"], "A", "dev")
    _add_slice_role(conn, task["id"], "B", "dev")
    _set_current(conn, "dev", task["id"], "B")

    d = context_router.route(
        conn, target_role="dev", source_task_id=task["id"],
        source_slice_key="A", message="A 模块上一轮结论有异常")
    assert d["mode"] == "new_slice"
    assert d["target_slice_key"] == "A"
    assert "participant_new_slice" in d["reasons"]


def test_participant_low_relevance_may_dispatch(env):
    conn = env
    source = tasks_service.create(conn, title="OpenSSL 证书审计")
    current = tasks_service.create(conn, title="调试网关超时")
    conn.execute(
        "INSERT INTO task_assignees (task_id,role_slug,joined_at) VALUES (?,?,?)",
        (source["id"], "dev", now_iso()))
    conn.commit()
    _set_current(conn, "dev", current["id"], None)

    d = context_router.route(
        conn, target_role="dev", source_task_id=source["id"],
        message="请帮忙修一下 Redis 缓存击穿问题")
    assert d["mode"] == "dispatch"
    assert "participant_low_relevance" in d["reasons"]


def test_explicit_dispatch_overrides_identity(env):
    conn = env
    task = tasks_service.create(conn, title="模块 B")
    _add_slice_role(conn, task["id"], "B", "dev")
    _set_current(conn, "dev", task["id"], "B")

    d = context_router.route(
        conn, target_role="dev", source_task_id=task["id"],
        source_slice_key="B", message="这条也顺手看下", explicit_mode="dispatch")
    assert d["mode"] == "dispatch"
    assert "explicit:dispatch" in d["reasons"]
