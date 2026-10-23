"""物理删除任务：先为参与角色固化知识快照，再清理该任务的运行时资源。"""
from __future__ import annotations

from anvil import config
from anvil.db import connect
from anvil.services import daemon as daemon_service
from anvil.services import tasks as tasks_service
from anvil.util import now_iso


def test_delete_task_snapshots_knowledge_before_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(daemon_service, "get_manager", lambda: None)
    conn = connect(str(tmp_path / "delete.db"))
    task = tasks_service.create(conn, title="即将删除的任务")
    tid = task["id"]
    now = now_iso()
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) "
        "VALUES ('R-DEL', 'dev', ?, 'running', ?)", (tid, now))
    conn.execute(
        "INSERT INTO role_task_sessions "
        "(role_slug, task_key, session_id, status, created_at, updated_at) "
        "VALUES ('dev', ?, 'sid-dev', 'idle', ?, ?)", (tid, now, now))
    conn.commit()
    tasks_service.comment(conn, tid, "dev", "关键结论：删除前必须保留")

    assert tasks_service.delete(conn, tid, force=True) is True

    snapshots = list(
        (config.AGENTS_DIR / "dev" / "notes" / "knowledge" / tid)
        .glob("*task-deleted*.md"))
    assert len(snapshots) == 1
    body = snapshots[0].read_text(encoding="utf-8")
    assert "关键结论：删除前必须保留" in body
    assert "task-deleted" in body

    # 角色本任务资源清理：槽、run、claim、inbox cursor 都不应残留
    assert conn.execute(
        "SELECT 1 FROM role_task_sessions WHERE role_slug='dev' AND task_key=?",
        (tid,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM runs WHERE task_id=?", (tid,)).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM task_role_claims WHERE task_id=?", (tid,)).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM role_inbox_cursors WHERE task_id=?", (tid,)).fetchone() is None
    audit = conn.execute(
        "SELECT payload_json FROM audit WHERE task_id=? AND action='delete'",
        (tid,)).fetchone()
    assert audit is not None
    assert '"consolidated_roles": ["dev"]' in audit["payload_json"]
    conn.close()
