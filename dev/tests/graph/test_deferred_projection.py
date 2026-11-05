"""持久会话延迟投影：run 创建时不写 CLAUDE.md，实际投递前才写。"""
from __future__ import annotations

from pathlib import Path

from anvil import config
from anvil.db import connect
from anvil.graph import nodes
from anvil.services import runs as runs_service
from anvil.services import task_workspace
from anvil.util import now_iso


def test_project_node_defers_then_projects_on_delivery(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "agents" / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "DEFERRED_PROJECTION", True)

    db = str(tmp_path / "t.db")
    conn = connect(db)
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,description,status,created_at,updated_at) "
        "VALUES ('T1','延迟投影','实现功能','in_progress',?,?)", (now, now))
    conn.commit()
    run = runs_service.create_run(conn, "dev", "T1", prompt="做 T1")

    # 模拟持久会话：project_node 只标记 projecting，不写文件。
    monkeypatch.setattr(nodes, "_persistent_session",
                        lambda conn, effective: {"ok": True})
    nodes.project_node({}, {"configurable": {"db_path": db, "thread_id": run["id"]}})
    ws = task_workspace.resolve(conn, "T1")
    assert ws and not (Path(ws) / "CLAUDE.md").exists()
    assert runs_service.get_run(conn, run["id"])["status"] == "projecting"

    # 实际投递前投影：文件出现且包含任务正文。
    assert nodes.project_run_by_path(db, run["id"]) is True
    target = Path(ws) / "CLAUDE.md"
    assert target.exists()
    assert "实现功能" in target.read_text(encoding="utf-8")
    conn.close()
