"""任务 → 知识 repo 绑定：阈值、人工锁、继承、草稿校验、检索范围。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import board, knowledge_binding, tasks as tasks_service
from anvil.util import ConflictError, jdumps, now_iso


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPOS_DIR", tmp_path / "repos")
    monkeypatch.setattr(config, "KNOWLEDGE_BINDING_MODE", "auto")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_ENABLED", True)
    c = connect(str(tmp_path / "binding.db"))
    yield c
    c.close()


def _lesson(conn, repo: str, tags: list[str], content: str) -> None:
    conn.execute(
        "INSERT INTO lessons (id, content, tags_json, repo, status, created_at, approved_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"L-{content[:8]}", content, jdumps(tags), repo, "approved",
         now_iso(), now_iso()),
    )
    conn.commit()


def test_auto_bind_uses_other_evidence_not_new_task_itself(conn):
    task = tasks_service.create(
        conn, title="OpenSSL ASan 误报复现", description="核实 ASan 报告",
        tags=["openssl", "asan"], repo="openssl-4.0.1")
    # 只有任务自身、没有 lesson/workspace/文件系统证据：不能自证绑定。
    assert task["knowledge_repos"] == []
    assert task["knowledge_binding_state"] == "fallback"


def test_auto_bind_applies_strong_tag_match(conn):
    _lesson(conn, "openssl-4.0.1", ["openssl", "asan"], "ASan 误报需要看编译选项")
    task = tasks_service.create(
        conn, title="OpenSSL ASan 误报复现", description="核实堆缓冲区溢出",
        tags=["openssl", "asan"], repo="other-app")
    applied = [b for b in task["knowledge_repos"] if b["status"] == "applied"]
    assert [b["repo"] for b in applied] == ["openssl-4.0.1"]
    assert applied[0]["source"] == "auto"
    assert "openssl" in applied[0]["matched_tags"]
    assert task["knowledge_binding_state"] == "applied"


def test_manual_lock_is_preserved_when_auto_reruns(conn):
    _lesson(conn, "openssl-4.0.1", ["openssl", "asan"], "ASan 误报需要看编译选项")
    task = tasks_service.create(conn, title="临时任务", tags=["other"])
    knowledge_binding.set_manual(conn, task["id"], ["gomoku"])
    conn.execute("UPDATE tasks SET knowledge_repos_locked=0 WHERE id=?", (task["id"],))
    conn.commit()
    knowledge_binding.auto_bind(conn, tasks_service.get(conn, task["id"]))
    rows = knowledge_binding.list_bindings(conn, task["id"])
    assert any(r["repo"] == "gomoku" and r["source"] == "human" for r in rows)


def test_child_inherits_parent_when_no_strong_match(conn):
    parent = tasks_service.create(conn, title="父任务", knowledge_repos=["gomoku"])
    child = tasks_service.create(conn, title="子任务", parent_id=parent["id"])
    assert [(b["repo"], b["source"]) for b in child["knowledge_repos"]] == [
        ("gomoku", "inherited")]


def test_draft_strict_rejects_unknown_and_shared(conn):
    _lesson(conn, "openssl-4.0.1", ["openssl"], "经验")
    with pytest.raises(ConflictError):
        knowledge_binding.normalize_draft_tree(
            conn, {"knowledge_repos": [{"repo": "not-exist"}]}, strict=True)
    with pytest.raises(ConflictError):
        knowledge_binding.normalize_draft_tree(
            conn, {"knowledge_repos": [{"repo": "shared"}]}, strict=True)
    tree = knowledge_binding.normalize_draft_tree(
        conn, {"knowledge_repos": [{"repo": "openssl-4.0.1"}]}, strict=True)
    assert tree["knowledge_repos"][0]["repo"] == "openssl-4.0.1"


def test_board_landing_persists_tags_repo_and_cindy_binding(conn):
    _lesson(conn, "openssl-4.0.1", ["openssl", "asan"], "ASan 误报经验")
    draft = board.create_draft(
        conn, goal="OpenSSL 审计", tree={
            "tags": ["openssl", "asan"],
            "repo": "openssl-4.0.1",
            "base_branch": "main",
            "knowledge_repos": [{"repo": "openssl-4.0.1", "reason": "命中标签"}],
            "children": [{"title": "ASan 复核子任务", "role": "qa-role"}],
        }, created_by="agent:cindy")
    board.approve(conn, [draft["id"]])
    child = conn.execute(
        "SELECT * FROM tasks WHERE title='ASan 复核子任务'").fetchone()
    assert child["repo"] == "openssl-4.0.1"
    assert child["base_branch"] == "main"
    assert child["tags_json"] == '["openssl", "asan"]'
    rows = knowledge_binding.list_bindings(conn, child["id"])
    assert [(r["repo"], r["source"], r["status"]) for r in rows] == [
        ("openssl-4.0.1", "cindy", "applied")]


def test_off_mode_rolls_back_to_old_scope(conn, monkeypatch):
    task = tasks_service.create(conn, title="绑定任务", knowledge_repos=["gomoku"])
    assert knowledge_binding.retrieval_scope(conn, task)["mode"] == "multi"
    monkeypatch.setattr(config, "KNOWLEDGE_BINDING_MODE", "off")
    scope = knowledge_binding.retrieval_scope(conn, task)
    assert scope["mode"] == "any"
    assert scope["repos"] is None
    from anvil.services import knowledge as knowledge_service
    assert knowledge_service.scope_repos(conn, task["id"], None) is None


def test_retrieval_scope_modes(conn):
    t1 = tasks_service.create(conn, title="无仓库")
    assert knowledge_binding.retrieval_scope(conn, t1)["mode"] == "shared"
    t2 = tasks_service.create(conn, title="执行仓库", repo="app")
    scope = knowledge_binding.retrieval_scope(conn, t2)
    assert scope["mode"] == "legacy"
    assert scope["repos"] == ["app", "shared"]
    t3 = tasks_service.create(conn, title="绑定", knowledge_repos=["gomoku"])
    scope = knowledge_binding.retrieval_scope(conn, t3)
    assert scope["mode"] == "multi"
    assert scope["repos"][0] == "gomoku"
    assert "shared" in scope["repos"]
