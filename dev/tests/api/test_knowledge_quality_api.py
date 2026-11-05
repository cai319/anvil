"""知识质量门 API：409、补全、摘要、清洗开关、反馈/复核。"""
from __future__ import annotations

from tests.conftest import API, auth_headers, human_headers


async def _repo(client, name="openssl"):
    r = await client.post(f"{API}/knowledge/repos", json={"repo": name},
                          headers=human_headers())
    assert r.status_code == 201, r.text


async def _card(client, repo="openssl"):
    r = await client.post(
        f"{API}/knowledge/repos/{repo}/cards",
        json={"kind": "insight", "title": "待补全", "tldr": "摘要",
              "body_md": "正文", "fields": {"observation": "观察"}},
        headers=auth_headers())
    assert r.status_code == 201, r.text
    return r.json()


async def test_quality_gate_blocks_then_allows_completed_card(client):
    await _repo(client)
    card = await _card(client)
    blocked = await client.post(
        f"{API}/knowledge/repo-cards/{card['id']}/approve",
        headers=human_headers())
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert "quality" in detail
    assert "use_when" in detail["quality"]["blocking"]

    updated = await client.patch(
        f"{API}/knowledge/repo-cards/{card['id']}",
        json={"fields": {
            "observation": "观察", "evidence": "任务 T1 的评审记录",
            "lesson": "结论", "applicability": "X509 审计", "counterexamples": "无",
            "use_when": "审计 X509 时", "expected_effect": "避免漏报",
        }},
        headers=human_headers())
    assert updated.status_code == 200, updated.text
    assert updated.json()["quality"]["blocking"] == []

    approved = await client.post(
        f"{API}/knowledge/repo-cards/{card['id']}/approve",
        headers=human_headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    summary = (await client.get(
        f"{API}/knowledge/quality/summary?repo=openssl",
        headers=human_headers())).json()
    row = summary["repos"][0]
    assert row["approved"] == 1
    assert row["coverage"]["evidence"] == 1.0


async def test_clean_disabled_and_feedback_review(client, monkeypatch):
    await _repo(client)
    card = await _card(client)
    from anvil import config
    monkeypatch.setattr(config, "KNOWLEDGE_LLM_CLEANING_ENABLED", False)
    clean = await client.post(
        f"{API}/knowledge/repo-cards/{card['id']}/clean",
        json={"dry_run": True}, headers=human_headers())
    assert clean.status_code == 409

    feedback = await client.post(
        f"{API}/knowledge/repo-cards/{card['id']}/feedback",
        json={"signal": "helpful", "note": "测试"},
        headers=auth_headers())
    assert feedback.status_code == 200
    assert feedback.json()["signal"] == "helpful"

    candidates = await client.get(
        f"{API}/knowledge/review-candidates?repo=openssl",
        headers=human_headers())
    assert candidates.status_code == 200
    assert "cards" in candidates.json()
