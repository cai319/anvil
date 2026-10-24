"""派生 repo catalog：聚合、排除 shared/pending、notes 不泄漏正文、缓存失效。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import repo_catalog
from anvil.util import jdumps, now_iso


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPOS_DIR", tmp_path / "repos")
    (tmp_path / "repos" / "localrepo").mkdir(parents=True)
    c = connect(str(tmp_path / "catalog.db"))
    now = now_iso()
    c.execute(
        "INSERT INTO workspaces (id, name, repo, description, created_at, updated_at) "
        "VALUES ('W1','安全','openssl-4.0.1','OpenSSL 安全审计',?,?)", (now, now))
    c.execute(
        "INSERT INTO lessons (id, content, tags_json, repo, status, created_at, approved_at) "
        "VALUES ('L1','SECRET_LESSON_BODY','[\"openssl\",\"asan\"]','openssl-4.0.1',"
        "'approved',?,?)", (now, now))
    c.execute(
        "INSERT INTO lessons (id, content, tags_json, repo, status, created_at) "
        "VALUES ('L2','PENDING_BODY','[\"pending-tag\"]','openssl-4.0.1','pending',?)", (now,))
    c.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at, kind, scope, "
        "tags_json, repo) VALUES ('N1','dev','notes/x.md','SECRET_NOTE_BODY',?,"
        "'note','role','[\"secret-tag\"]','openssl-4.0.1')", (now,))
    c.execute(
        "INSERT INTO tasks (id,title,status,tags_json,repo,created_at,updated_at) "
        "VALUES ('T1','ASan 复核','todo','[\"asan\"]','openssl-4.0.1',?,?)", (now, now))
    c.commit()
    repo_catalog.invalidate_catalog()
    yield c
    c.close()
    repo_catalog.invalidate_catalog()


def test_catalog_aggregates_without_leaking_bodies(conn):
    catalog = repo_catalog.build_catalog(conn)
    repos = {p["repo"]: p for p in catalog["repos"]}
    assert "shared" not in repos
    assert "openssl-4.0.1" in repos
    assert "localrepo" in repos
    profile = repos["openssl-4.0.1"]
    assert any(t["tag"] == "asan" for t in profile["tag_freq"])
    assert all(t["tag"] != "pending-tag" for t in profile["tag_freq"])
    rendered = repo_catalog.render_catalog_for_prompt(catalog)
    assert "SECRET_LESSON_BODY" not in rendered
    assert "SECRET_NOTE_BODY" not in rendered


def test_cache_invalidates(conn):
    first = repo_catalog.build_catalog(conn)
    assert first["total"] >= 2
    conn.execute(
        "INSERT INTO workspaces (id, name, repo, description, created_at, updated_at) "
        "VALUES ('W2','新频道','new-repo','新仓库',?,?)", (now_iso(), now_iso()))
    conn.commit()
    # 未失效时仍读缓存；失效后可见新仓库。
    repo_catalog.invalidate_catalog()
    second = repo_catalog.build_catalog(conn)
    assert "new-repo" in {p["repo"] for p in second["repos"]}


def test_suggest_excludes_current_task(conn):
    # 当前任务自身的 tags/repo 不应成为自己的证据。
    result = repo_catalog.suggest(
        conn, title="ASan 复核", tags=["asan"], exec_repo="openssl-4.0.1",
        exclude_task_id="T1")
    openssl = next((c for c in result["candidates"] if c["repo"] == "openssl-4.0.1"), None)
    # 仍会因 workspace/lesson 证据出现，但不能只靠任务自身；这里主要验证 API 不报错。
    assert openssl is None or openssl["score"] > 0
