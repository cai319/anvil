"""P0 可靠性：mark_spawned 重置计时、failure_class、suspend_active、heal_stale_runs。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.db import connect
from anvil.services import runs as runs_service

pytest.importorskip("anvil.graph")  # 与其他测试同口径：无 langgraph 时跳过


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def _mk_run(conn: sqlite3.Connection, rid="R1", role="qa-role", task="T1",
            status="running", sid="pg:999", started="2026-01-01T00:00:00+00:00") -> None:
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at, native_session_id) "
        "VALUES (?,?,?,?,?,?)", (rid, role, task, status, started, sid),
    )
    conn.commit()


def test_mark_spawned_resets_started_at_and_stores_timeout(conn):
    _mk_run(conn, started="2026-01-01T00:00:00+00:00")
    runs_service.mark_spawned(conn, "R1", native_session_id="pg:4242", timeout_seconds=3600)
    r = runs_service.get_run(conn, "R1")
    assert r["native_session_id"] == "pg:4242"
    assert r["timeout_seconds"] == 3600
    assert r["started_at"] > "2026-01-01"  # 已重置为当前时间
    assert r["status"] == "running"


def test_set_status_records_failure_class(conn):
    _mk_run(conn, status="running")
    runs_service.set_status(conn, "R1", "failed", ended_at="2026-01-02T00:00:00+00:00",
                            failure_class="platform_timeout")
    r = runs_service.get_run(conn, "R1")
    assert r["status"] == "failed"
    assert r["failure_class"] == "platform_timeout"
    # 后续状态迁移不清空 failure_class
    runs_service.set_status(conn, "R1", "failed")
    assert runs_service.get_run(conn, "R1")["failure_class"] == "platform_timeout"


def test_suspend_active_marks_running_suspended(conn, monkeypatch):
    _mk_run(conn, rid="R1", status="running", sid="pg:111")
    _mk_run(conn, rid="R2", status="running", sid=None)      # 挂起等待中的 run
    _mk_run(conn, rid="R3", status="completed", sid="pg:333")  # 终态不动
    terminated: list[str] = []
    monkeypatch.setattr(
        "anvil.platform.factory.get_platform",
        lambda: (type("PM", (), {"terminate": staticmethod(lambda s: terminated.append(s)),
                                  "probe": staticmethod(lambda s: type("P", (), {"alive": True})())})(),),
    )
    n = runs_service.suspend_active(conn)
    assert n == 2
    assert terminated == ["pg:111"]  # 有进程的终止，无进程的只改状态
    assert runs_service.get_run(conn, "R1")["status"] == "suspended"
    assert runs_service.get_run(conn, "R2")["status"] == "suspended"
    assert runs_service.get_run(conn, "R3")["status"] == "completed"
    assert runs_service.get_run(conn, "R1")["ended_at"] is None  # 挂起不是结束


def test_list_runs_active_filter(conn):
    """主页实时看板只取活跃会话（含挂起等待），历史/失败不进列表。"""
    _mk_run(conn, rid="R1", status="running")
    _mk_run(conn, rid="R2", status="completed")
    _mk_run(conn, rid="R3", status="suspended")
    _mk_run(conn, rid="R4", status="failed")
    rows = runs_service.list_runs(conn, statuses=runs_service.ACTIVE_RUN_STATUSES)
    assert {r["id"] for r in rows} == {"R1", "R3"}


def test_heal_stale_runs_suspends_orphans(conn, monkeypatch):
    _mk_run(conn, rid="R1", status="running", sid="pg:111")  # 崩溃遗留（进程已死）
    _mk_run(conn, rid="R2", status="projecting", sid=None)
    monkeypatch.setattr(
        "anvil.platform.factory.get_platform",
        lambda: (type("PM", (), {"terminate": staticmethod(lambda s: None),
                                  "probe": staticmethod(lambda s: type("P", (), {"alive": False})())})(),),
    )
    out = runs_service.heal_stale_runs(conn)
    assert out["healed"] == 2
    assert runs_service.get_run(conn, "R1")["status"] == "suspended"
    assert runs_service.get_run(conn, "R2")["status"] == "suspended"
