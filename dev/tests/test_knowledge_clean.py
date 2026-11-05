"""LLM 知识清洗：只预览/应用 proposed，引用校验与失败回退。"""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import knowledge_clean, knowledge_repos, repo_knowledge
from anvil.services import utility


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_DIR", data / "knowledge-repos")
    monkeypatch.setattr(config, "KNOWLEDGE_LLM_CLEANING_ENABLED", True)
    conn = connect(str(tmp_path / "clean.db"))
    knowledge_repos.ensure(conn, "openssl-4.0.1")
    card = repo_knowledge.create(
        conn, "openssl-4.0.1", kind="insight", title="旧标题",
        tldr="旧摘要", body_md="旧正文",
        fields={"observation": "原始观察：先核对版本"},
        status="proposed")
    yield conn, card
    conn.close()


def _llm_json(**fields):
    return json.dumps({
        "title": "先核对版本再下结论",
        "tldr": "审计 OpenSSL 时先核对版本，避免环境差异误判。",
        "fields": {
            "use_when": "审计 OpenSSL 时",
            "expected_effect": "避免环境差异误判",
            "evidence": "原始观察：先核对版本",
            **fields,
        },
        "citations": [
            {"field": "use_when", "quote": "原始观察：先核对版本"},
            {"field": "expected_effect", "quote": "原始观察：先核对版本"},
            {"field": "evidence", "quote": "原始观察：先核对版本"},
        ],
        "warnings": [],
    }, ensure_ascii=False)


def test_preview_does_not_write(env, monkeypatch):
    conn, card = env
    monkeypatch.setattr(utility, "generate", lambda *a, **k: _llm_json())
    preview = knowledge_clean.build_preview(conn, card["id"])
    assert preview["dry_run"] is True
    assert preview["fields"]["use_when"] == "审计 OpenSSL 时"
    # 未提供引用的新增字段被丢弃
    assert preview["fields"]["expected_effect"] == "避免环境差异误判"
    assert repo_knowledge.get(conn, card["id"])["title"] == "旧标题"


def test_preview_drops_uncited_field(env, monkeypatch):
    conn, card = env
    payload = json.loads(_llm_json())
    payload["fields"]["counterexamples"] = "未在来源出现的反例"
    monkeypatch.setattr(utility, "generate", lambda *a, **k: json.dumps(payload))
    preview = knowledge_clean.build_preview(conn, card["id"])
    assert "counterexamples" not in preview["fields"]
    assert any("counterexamples" in warning for warning in preview["warnings"])


def test_apply_keeps_proposed_and_records_llm(env, monkeypatch):
    conn, card = env
    monkeypatch.setattr(utility, "generate", lambda *a, **k: _llm_json())
    preview = knowledge_clean.build_preview(conn, card["id"])
    updated = knowledge_clean.apply_preview(conn, card["id"], preview)
    assert updated["status"] == "proposed"
    assert updated["title"] == "先核对版本再下结论"
    assert updated["quality"]["llm"]["prompt_version"] == knowledge_clean.PROMPT_VERSION


def test_disabled_flag_blocks(env, monkeypatch):
    conn, card = env
    monkeypatch.setattr(config, "KNOWLEDGE_LLM_CLEANING_ENABLED", False)
    with pytest.raises(Exception):
        knowledge_clean.build_preview(conn, card["id"])


def test_invalid_json_raises_cleaning_error(env, monkeypatch):
    conn, card = env
    monkeypatch.setattr(utility, "generate", lambda *a, **k: "not json")
    with pytest.raises(knowledge_clean.CleaningError):
        knowledge_clean.build_preview(conn, card["id"])


def test_utility_failure_propagates(env, monkeypatch):
    conn, card = env

    def fail(*args, **kwargs):
        raise utility.UtilityError("runtime unavailable")

    monkeypatch.setattr(utility, "generate", fail)
    with pytest.raises(utility.UtilityError):
        knowledge_clean.build_preview(conn, card["id"])
