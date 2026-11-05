"""Task Workspace：每任务数据目录、生命周期、与知识库解耦。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import task_workspace
from anvil.util import now_iso


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "data" / "task-workspaces")
    c = connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def test_ensure_creates_isolated_directories(conn):
    a = task_workspace.ensure(conn, "TA")
    b = task_workspace.ensure(conn, "TB")

    pa, pb = Path(a["path"]), Path(b["path"])
    assert pa != pb
    assert pa.is_dir() and pb.is_dir()
    for sub in ("work", "knowledge", "drafts", ".anvil"):
        assert (pa / sub).is_dir()
    meta = (pa / ".anvil" / "workspace.json").read_text(encoding="utf-8")
    assert '"task_id": "TA"' in meta


def test_ensure_is_idempotent_and_keeps_source_repo(conn):
    first = task_workspace.ensure(conn, "T1", source_repo="openssl")
    second = task_workspace.ensure(conn, "T1")
    assert first["path"] == second["path"]
    assert second["source_repo"] == "openssl"
    assert second["kind"] == "git_worktree"
    rows = conn.execute("SELECT COUNT(*) AS n FROM task_workspaces").fetchone()
    assert rows["n"] == 1


def test_resolve_lazily_creates(conn):
    assert task_workspace.get(conn, "T9") is None
    path = task_workspace.resolve(conn, "T9")
    assert path and Path(path).is_dir()
    assert task_workspace.get(conn, "T9")["status"] == "active"


def test_archive_and_cleanup_skip_busy_slot(conn):
    task_workspace.ensure(conn, "TA")
    task_workspace.ensure(conn, "TB")
    task_workspace.archive(conn, "TA")
    task_workspace.archive(conn, "TB")
    # TA 无活跃槽，TB 有 busy 槽 → 只清理 TA。
    now = now_iso()
    conn.execute(
        "INSERT INTO role_task_sessions "
        "(role_slug, task_key, session_id, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("dev", "TB", "s1", "busy", now, now),
    )
    conn.execute("UPDATE task_workspaces SET updated_at='2000-01-01T00:00:00+00:00'")
    conn.commit()

    result = task_workspace.cleanup_stale(conn, retention_days=0)
    assert "TA" in result["removed"]
    assert "TB" in result["skipped"]
    assert not Path(task_workspace.get(conn, "TA")["path"]).exists()
    assert Path(task_workspace.get(conn, "TB")["path"]).exists()
    assert task_workspace.get(conn, "TA")["status"] == "deleted"


def test_disabled_mode_falls_back(conn, monkeypatch):
    monkeypatch.setattr(config, "TASK_WORKSPACE_ENABLED", False)
    path = task_workspace.resolve(conn, "TX")
    assert path == ""
    assert task_workspace.get(conn, "TX") is None
