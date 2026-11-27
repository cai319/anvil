"""沙箱实例单写者与迁移（M0）。"""
from __future__ import annotations

from anvil.db import connect, init_schema
from anvil.services import sandbox


def _connect(tmp_path):
    return connect(str(tmp_path / "t.db"))


def test_instance_lifecycle(tmp_path):
    conn = _connect(tmp_path)
    try:
        iid = sandbox.create_instance(
            conn, role_slug="qa-role", scope_kind="role_session",
            scope_id="task-1", backend="host",
            capabilities={"process_tree": True},
        )
        assert iid
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "starting"
        assert row["generation"] == 1
        assert row["role_slug"] == "qa-role"

        sandbox.bind_instance(conn, iid, pid=123, native_handle="pg:123")
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "running"
        assert row["pid"] == 123
        assert row["native_handle"] == "pg:123"

        second = sandbox.create_instance(
            conn, role_slug="qa-role", scope_kind="role_session",
            scope_id="task-1", backend="host",
        )
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (second,)).fetchone()
        assert row["generation"] == 2

        sandbox.finish_instance(conn, second, exit_reason="exit_0")
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (second,)).fetchone()
        assert row["status"] == "exited"
        assert row["exit_reason"] == "exit_0"
        assert row["ended_at"]

        # 启动自愈只清理仍活跃的实例；已退出实例保持不变。
        sandbox.reconcile_startup(conn)
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "stale"
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (second,)).fetchone()
        assert row["status"] == "exited"
    finally:
        conn.close()


def test_migration_is_idempotent_and_adds_columns(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    try:
        init_schema(conn)  # 二次执行必须幂等
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "sandbox_instances" in tables
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert "sandbox_instance_id" in run_cols
        rs_cols = {r[1] for r in conn.execute("PRAGMA table_info(role_sessions)")}
        assert "sandbox_instance_id" in rs_cols
        rts_cols = {r[1] for r in conn.execute("PRAGMA table_info(role_task_sessions)")}
        assert "sandbox_instance_id" in rts_cols
    finally:
        conn.close()


def test_create_instance_tolerates_missing_table(tmp_path):
    """观测表缺失时返回空串，不抛异常（避免把角色执行带崩）。"""
    conn = _connect(tmp_path)
    try:
        conn.execute("DROP TABLE IF EXISTS sandbox_instances")
        conn.commit()
        assert sandbox.create_instance(
            conn, role_slug="qa", scope_kind="role_session", scope_id="k",
            backend="host") == ""
    finally:
        conn.close()
