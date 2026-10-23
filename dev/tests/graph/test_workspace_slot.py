"""常驻运行的任务工作区复用：sqlite3.Row 读取不能使用 .get()。"""
from __future__ import annotations

from anvil.db import connect
from anvil.graph import nodes
from anvil.util import now_iso


def test_run_workspace_path_prefers_task_slot(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "ws.db"))
    ws = tmp_path / "stable-ws"
    ws.mkdir()
    now = now_iso()
    conn.execute(
        "INSERT INTO role_task_sessions "
        "(role_slug, task_key, session_id, workspace_path, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("dev", "T1", "sid", str(ws), "idle", now, now),
    )
    conn.commit()
    # 只验证槽读取路径，不依赖 daemon/runtime 是否启用。
    monkeypatch.setattr(nodes, "_persistent_session", lambda conn, effective: {"ok": True})
    run = {"role_slug": "dev", "task_id": "T1",
           "worktree_path": str(tmp_path / "new-worktree")}
    role = {"home_path": str(tmp_path / "home")}

    assert nodes._run_workspace_path(conn, run, role, {}) == str(ws)
    conn.close()


def test_run_workspace_path_falls_back_without_slot(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "ws2.db"))
    monkeypatch.setattr(nodes, "_persistent_session", lambda conn, effective: {"ok": True})
    run = {"role_slug": "dev", "task_id": "T2",
           "worktree_path": str(tmp_path / "new-worktree")}
    role = {"home_path": str(tmp_path / "home")}

    assert nodes._run_workspace_path(conn, run, role, {}) == str(tmp_path / "home")
    conn.close()
