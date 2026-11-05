"""Phase 3：knowledge explain 与 L0 能力清单。"""
from __future__ import annotations

from anvil import config
from anvil.db import connect
from anvil.services import context_explain, knowledge_repos, roles
from anvil.util import now_iso


def test_explain_task_reports_scope_and_cards(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_DIR", data / "knowledge-repos")
    monkeypatch.setattr(config, "KNOWLEDGE_BINDING_MODE", "auto")
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,created_at,updated_at) "
        "VALUES ('T1','OpenSSL audit','todo','[\"openssl\"]',?,?)", (now, now))
    conn.commit()
    knowledge_repos.bind_shared(conn, "T1", "openssl-4.0.1", score=0.9, primary=True)
    out = context_explain.explain_task(conn, "T1", role="dev")
    assert out["primary_repo"] == "openssl-4.0.1"
    assert out["retrieval_scope"]["mode"] == "multi"
    assert out["bindings"][0]["scope"] == "shared"
    assert "topic_repo_cards" in out
    assert "principles_chars" in out and "capabilities_chars" in out
    conn.close()


def test_capability_summary_includes_working_mode_and_tools(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    conn = connect(str(tmp_path / "t.db"))
    roles.create(conn, slug="dev", display_name="Dev", definition={
        "system_prompt": "x", "working_mode": "先验证再结论",
        "capabilities": {"audit": 0.9},
        "runtime": {"id": "claude-code"}, "tools": {}, "concurrency": {},
    })
    roles.update_catalog(conn, "dev", ["demo-skill"], [])
    text = roles.capability_summary(conn, "dev")
    assert "工作方式" in text and "先验证再结论" in text
    assert "skill: demo-skill" in text
    conn.close()
