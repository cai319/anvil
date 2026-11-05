"""Phase 2：失败/返工/完成自动草稿（proposed）+ 人工批准 + 幂等。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import (experience_capture, knowledge_repos,
                            repo_knowledge, task_knowledge)
from anvil.util import now_iso


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_DIR", data / "knowledge-repos")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", data / "task-workspaces")
    monkeypatch.setattr(config, "EXPERIENCE_CAPTURE_ENABLED", True)
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,repo,created_at,updated_at) "
        "VALUES ('T1','审计 X509','in_progress','[\"openssl\"]','openssl',?,?)",
        (now, now))
    conn.commit()
    task = dict(conn.execute("SELECT * FROM tasks WHERE id='T1'").fetchone())
    knowledge_repos.create_local_for_task(
        conn, task, workspace_path=str(data / "task-workspaces" / "T1"))
    yield conn, data
    conn.close()


def test_failure_capture_is_proposed_and_idempotent(env):
    conn, data = env
    card = experience_capture.capture_failure(
        conn, "T1", run_id="r1", step_id="S2", failure_class="test_failure",
        actor="dev")
    assert card is not None
    row = conn.execute("SELECT * FROM knowledge_captures WHERE task_id='T1'").fetchone()
    assert row["trigger"] == "failure" and row["card_id"]
    stored = task_knowledge.get(conn, row["card_id"])
    assert stored["status"] == "proposed"
    assert "drafts" in stored["path"]
    # proposed 不进入注入。
    assert task_knowledge.render_for_task(conn, "T1") == ""
    # 同一事件重复捕获 → 不重复建卡。
    assert experience_capture.capture_failure(
        conn, "T1", run_id="r1", step_id="S2", failure_class="test_failure",
        actor="dev") is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_captures WHERE task_id='T1'"
    ).fetchone()["n"] == 1
    # 人工批准后进入 active/cards。
    approved = task_knowledge.approve(conn, stored["id"], actor="human")
    assert approved["status"] == "active"
    assert "cards" in approved["path"]
    assert "失败" in task_knowledge.render_for_task(conn, "T1")


def test_completion_with_shared_binding_creates_repo_card(env):
    conn, data = env
    knowledge_repos.bind_shared(
        conn, "T1", "openssl-4.0.1", score=0.9, primary=True)
    card = experience_capture.capture_task_completion(
        conn, "T1", run_id="r2", actor="dev")
    assert card is not None
    assert card["status"] == "proposed"
    assert card["repo_id"] == "openssl-4.0.1"
    assert repo_knowledge.render_for_task(
        conn, {"title": "X509"}, ["openssl-4.0.1"]) == ""
    repo_knowledge.approve(conn, card["id"], actor="human")
    assert "完成" in repo_knowledge.render_for_task(
        conn, {"title": "X509"}, ["openssl-4.0.1"])


def test_capture_disabled_by_default(env, monkeypatch):
    monkeypatch.setattr(config, "EXPERIENCE_CAPTURE_ENABLED", False)
    assert experience_capture.capture_failure(
        conn=env[0], task_id="T1", run_id="r3") is None
