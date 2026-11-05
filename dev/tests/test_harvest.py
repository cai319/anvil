"""Phase 2：任务级 → 主题知识库 → Channel → Shared 的晋升审批链。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import (experience_capture, harvest, knowledge_repos,
                            memory, repo_knowledge, task_knowledge)
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
    conn.execute(
        "INSERT INTO workspaces (id,name,repo,description,knowledge_repo,created_at,updated_at) "
        "VALUES ('C1','安全审计','openssl','','openssl-4.0.1',?,?)",
        (now, now))
    conn.commit()
    task = dict(conn.execute("SELECT * FROM tasks WHERE id='T1'").fetchone())
    knowledge_repos.create_local_for_task(
        conn, task, workspace_path=str(data / "task-workspaces" / "T1"))
    yield conn, data
    conn.close()


def test_task_to_repo_to_channel_to_shared(env):
    conn, data = env
    draft = experience_capture.capture_failure(
        conn, "T1", run_id="r1", step_id="S2", failure_class="test_failure",
        actor="dev")
    assert draft is not None
    task_knowledge.approve(conn, draft["id"], actor="human")

    repo_card = harvest.task_card_to_repo(
        conn, draft["id"], "openssl-4.0.1", actor="human")
    assert repo_card["status"] == "proposed"
    assert task_knowledge.get(conn, draft["id"])["status"] == "archived"
    # 未批准的主题卡不能继续晋升。
    with pytest.raises(Exception):
        harvest.repo_card_to_channel(conn, repo_card["id"], "C1")

    repo_knowledge.approve(conn, repo_card["id"], actor="human")
    channel = harvest.repo_card_to_channel(
        conn, repo_card["id"], "C1", actor="human")
    assert channel["status"] == "proposed"
    assert channel["workspace_id"] == "C1"

    lesson = harvest.repo_card_to_shared(conn, repo_card["id"], actor="human")
    assert lesson["status"] == "pending"
    assert lesson["repo"] == "shared"
    # 直接写 data/shared 只能由 approve_lesson 完成，harvest 不直接落文件。
    assert not (data / "shared" / "lessons.md").exists()
