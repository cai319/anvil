"""Phase 1：主题知识库注册表、任务级 local repo、repo 卡片文件真相。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import knowledge_repos, repo_knowledge
from anvil.util import now_iso


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_DIR", data / "knowledge-repos")
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", data / "task-workspaces")
    conn = connect(str(tmp_path / "t.db"))
    yield conn, data
    conn.close()


def _task(conn, tid="T1", **kw):
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,created_at,updated_at) "
        "VALUES (?,?,'todo',?,?,?)",
        (tid, kw.get("title", "任务"), kw.get("tags_json", "[]"), now, now))
    conn.commit()
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def test_registry_ensure_rejects_shared(env):
    conn, data = env
    repo = knowledge_repos.ensure(conn, "OpenSSL-4.0.1", description="x")
    assert repo["id"] == "openssl-4.0.1"
    assert knowledge_repos.get(conn, "openssl-4.0.1")["display_name"] == "OpenSSL-4.0.1"
    assert knowledge_repos.ensure(conn, "OpenSSL-4.0.1")["id"] == repo["id"]
    with pytest.raises(Exception):
        knowledge_repos.ensure(conn, "shared")


def test_local_repo_is_inside_workspace_and_not_registered(env):
    conn, data = env
    task = _task(conn, "T1", title="审计 X509", tags_json='["openssl"]')
    ws = data / "task-workspaces" / "T1"
    out = knowledge_repos.create_local_for_task(
        conn, dict(task), workspace_path=str(ws))
    assert out["scope"] == "local"
    local = Path(out["path"])
    assert local.is_dir() and (local / "cards").is_dir()
    assert (local / "drafts").is_dir()
    row = conn.execute(
        "SELECT scope, repo_id, path, is_primary FROM task_knowledge_repos "
        "WHERE task_id='T1'").fetchone()
    assert row["scope"] == "local" and row["is_primary"] == 1
    assert knowledge_repos.get(conn, out["repo"]) is None
    t = conn.execute("SELECT repo, knowledge_mode FROM tasks WHERE id='T1'").fetchone()
    assert t["knowledge_mode"] == "local" and t["repo"] == out["repo"]


def test_bind_shared_sets_primary_and_registry(env):
    conn, data = env
    _task(conn, "T2", title="OpenSSL")
    knowledge_repos.bind_shared(
        conn, "T2", "openssl-4.0.1", score=0.8, primary=True)
    row = conn.execute(
        "SELECT scope, repo_id, is_primary FROM task_knowledge_repos "
        "WHERE task_id='T2'").fetchone()
    assert row["scope"] == "shared" and row["is_primary"] == 1
    assert row["repo_id"] == "openssl-4.0.1"
    assert knowledge_repos.get(conn, "openssl-4.0.1") is not None
    t = conn.execute("SELECT repo, knowledge_mode FROM tasks WHERE id='T2'").fetchone()
    assert t["repo"] == "openssl-4.0.1" and t["knowledge_mode"] == "bound"


def test_repo_card_file_truth_and_render(env):
    conn, data = env
    knowledge_repos.ensure(conn, "openssl-4.0.1")
    card = repo_knowledge.create(
        conn, "openssl-4.0.1", kind="insight", title="X509 校验",
        tldr="必须校验证书链", body_md="正文", tags=["openssl", "x509"],
        fields={
            "observation": "证书链校验是审计基线",
            "evidence": "任务 T1 的评审记录",
            "lesson": "必须校验证书链",
            "applicability": "X509 审计任务",
            "counterexamples": "无",
            "use_when": "审计 X509 证书链时",
            "expected_effect": "避免漏掉证书链问题，减少返工",
        },
        authority="reference", status="proposed", confidence=0.8)
    root = data / "knowledge-repos" / "openssl-4.0.1"
    assert (root / "proposed" / f"{card['id']}.md").exists()
    assert repo_knowledge.render_for_task(
        conn, {"title": "X509"}, ["openssl-4.0.1"]) == ""

    approved = repo_knowledge.approve(conn, card["id"], actor="human")
    assert approved["status"] == "approved"
    assert (root / "cards" / f"{card['id']}.md").exists()
    assert not (root / "proposed" / f"{card['id']}.md").exists()
    text = repo_knowledge.render_for_task(
        conn, {"title": "X509"}, ["openssl-4.0.1"])
    assert "X509 校验" in text and "必须校验证书链" in text

    # 文件是真相：删掉 DB 行后 reindex 能从文件恢复。
    conn.execute("DELETE FROM repo_knowledge WHERE id=?", (card["id"],))
    conn.commit()
    assert repo_knowledge.get(conn, card["id"]) is None
    repo_knowledge.reindex(conn, "openssl-4.0.1")
    assert repo_knowledge.get(conn, card["id"])["status"] == "approved"


def test_rename_and_merge(env):
    conn, data = env
    knowledge_repos.ensure(conn, "openssl")
    knowledge_repos.ensure(conn, "openssl-x509")
    card = repo_knowledge.create(
        conn, "openssl", kind="insight", title="旧卡", tldr="t",
        body_md="b", status="proposed")
    renamed = knowledge_repos.rename(conn, "openssl", "openssl-4.0.1")
    assert renamed["display_name"] == "openssl-4.0.1"
    assert knowledge_repos.get(conn, "openssl")["id"] == renamed["id"]
    assert repo_knowledge.get(conn, card["id"])["repo"] == "openssl-4.0.1"
    merged = knowledge_repos.merge(conn, "openssl-4.0.1", "openssl-x509")
    assert merged["id"] == "openssl-x509"
    assert repo_knowledge.get(conn, card["id"])["repo_id"] == "openssl-x509"
    # rename 保持稳定 id；merge 把原 id 标为 merged。
    assert conn.execute(
        "SELECT status FROM knowledge_repos WHERE id='openssl'"
    ).fetchone()["status"] == "merged"
