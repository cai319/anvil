"""Phase 4：主题知识库管理/审批队列 API。"""
from __future__ import annotations

from tests.conftest import API, auth_headers, human_headers


async def test_topic_repo_queue_and_approval(client):
    created = await client.post(
        f"{API}/knowledge/repos",
        json={"repo": "openssl", "description": "OpenSSL 审计主题"},
        headers=human_headers(),
    )
    assert created.status_code == 201

    proposed = await client.post(
        f"{API}/knowledge/repos/openssl/cards",
        json={
            "kind": "insight", "title": "先验证版本再下结论",
            "tldr": "不同版本的默认配置不同。", "body_md": "核对版本号。",
            "fields": {
                "observation": "版本差异会影响默认配置",
                "evidence": "任务 T1 的输出",
                "lesson": "先核对版本",
                "applicability": "OpenSSL 审计",
                "counterexamples": "无",
                "use_when": "审计 OpenSSL 时",
                "expected_effect": "避免版本差异误判",
            },
            "tags": ["openssl"], "authority": "reference",
        },
        headers=auth_headers(),
    )
    assert proposed.status_code == 201
    card = proposed.json()
    assert card["status"] == "proposed"

    filtered = (await client.get(
        f"{API}/knowledge/repos/openssl/cards?q=版本",
        headers=human_headers())).json()
    assert [item["id"] for item in filtered] == [card["id"]]
    filtered = (await client.get(
        f"{API}/knowledge/repos/openssl/cards?q=不存在",
        headers=human_headers())).json()
    assert filtered == []

    repos = (await client.get(
        f"{API}/knowledge/topic-repos", headers=human_headers())).json()["repos"]
    row = next(item for item in repos if item["display_name"] == "openssl")
    assert row["card_count"] == 1
    assert row["pending"] == 1

    queue = (await client.get(
        f"{API}/knowledge/repo-cards?status=proposed",
        headers=human_headers())).json()["cards"]
    assert [item["id"] for item in queue] == [card["id"]]

    approved = await client.post(
        f"{API}/knowledge/repo-cards/{card['id']}/approve",
        headers=human_headers(),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    queue = (await client.get(
        f"{API}/knowledge/repo-cards?status=proposed",
        headers=human_headers())).json()["cards"]
    assert queue == []
    all_cards = (await client.get(
        f"{API}/knowledge/repo-cards?status=all",
        headers=human_headers())).json()["cards"]
    assert [item["id"] for item in all_cards] == [card["id"]]

    # 任务级草稿队列也要允许 proposed 过滤（管理页审批入口）。
    task_cards = await client.get(
        f"{API}/knowledge/task-cards?status=proposed",
        headers=human_headers(),
    )
    assert task_cards.status_code == 200
