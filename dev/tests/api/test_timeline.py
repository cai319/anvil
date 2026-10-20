"""任务会话时间线测试：audit（任务动作）与 activities（评论/运行事件）合并排序。"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, role_payload, runtime_payload, task_payload, human_headers  # noqa: E402

pytestmark = [pytest.mark.usefixtures("client")]


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())
    await client.post(f"{API}/roles", json=role_payload(slug="reviewer"))
    await client.post(f"{API}/tasks", json=task_payload(id_="T-TL"), headers=auth_headers())


async def test_timeline_merges_actions_and_comments(client):
    await _seed(client)
    # claim（audit）→ comment（activity）→ submit（audit）→ review（audit）
    await client.post(f"{API}/tasks/T-TL/claim", json={}, headers=auth_headers("qa-role"))
    await client.post(f"{API}/tasks/T-TL/comment", json={"body": "我来处理"}, headers=auth_headers("qa-role"))
    await client.post(f"{API}/tasks/T-TL/submit", json={"summary": "完成"}, headers=auth_headers("qa-role"))
    await client.post(f"{API}/tasks/T-TL/review", json={"approve": True}, headers=human_headers())

    r = await client.get(f"{API}/tasks/T-TL/timeline", headers=auth_headers())
    assert r.status_code == 200
    tl = r.json()
    kinds = [e["kind"] for e in tl]
    assert kinds == ["claim", "comment", "submit", "review"]  # 按时间排序
    # comment 带 body、review 带 approve
    comment = next(e for e in tl if e["kind"] == "comment")
    assert comment["payload"]["body"] == "我来处理"
    review = next(e for e in tl if e["kind"] == "review")
    assert review["payload"]["approve"] is True
    # 时间线条目带 role / key
    assert all("key" in e and "role" in e for e in tl)


async def test_timeline_404_for_missing_task(client):
    r = await client.get(f"{API}/tasks/NOPE/timeline", headers=auth_headers())
    assert r.status_code == 404


async def test_mention_endpoint_targets_role(client):
    """自然人对角色 @提及：POST /tasks/{id}/mention 注入注意事项/上下文。"""
    await _seed(client)
    r = await client.post(
        f"{API}/tasks/T-TL/mention",
        json={"target": "developer", "message": "注意 limit/offset 边界"},
        headers=auth_headers("human"),
    )
    assert r.status_code == 200
    tl = (await client.get(f"{API}/tasks/T-TL/timeline", headers=auth_headers())).json()
    mention = next(e for e in tl if e["kind"] == "mention")
    assert mention["payload"]["target"] == "developer"
    assert mention["payload"]["message"] == "注意 limit/offset 边界"
    assert mention["role"] == "human"


async def test_mention_wakes_target_role(client):
    """@ 即拉入：@ 任务参与角色会唤醒该角色（创建 run）。"""
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-WAKE", "title": "t", "assignees": ["developer"]}, headers=auth_headers())
    r = await client.post(
        f"{API}/tasks/T-WAKE/mention",
        json={"target": "developer", "message": "请开始"},
        headers=auth_headers("human"),
    )
    assert r.status_code == 200
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert any(run["task_id"] == "T-WAKE" and run["role_slug"] == "developer" for run in runs)
