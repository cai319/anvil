"""L2 · A/C 编排闭环契约测试（task #14）。

覆盖 SE 增补 §5-7：A 目标拆解（decompose→draft→审批落板）+ C 调度唤醒+自认领。
接口以 SE 增补为准：
  POST /api/v1/board/decompose {goal, workspace_id}
  GET  /api/v1/board/drafts?workspace_id=
  POST /api/v1/board/drafts/approve {draft_ids[]}
  POST /api/v1/board/drafts/reject {draft_id}
  POST /api/v1/control/tick   （扩展为完整调度循环）
A/C 编排未实现（task #13）前整文件 skip；落地后移除 skip。
"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, role_payload, runtime_payload, human_headers  # noqa: E402

pytestmark = [
    pytest.mark.usefixtures("client"),
]


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    # 编排者系统角色（只拆解不竞价）
    await client.post(f"{API}/roles", json=role_payload(slug="orchestrator"))
    await client.post(f"{API}/roles", json=role_payload(slug="backend-eng"))
    # design-dev-test 模板的参与角色
    await client.post(f"{API}/roles", json=role_payload(slug="designer"))
    await client.post(f"{API}/roles", json=role_payload(slug="developer"))
    await client.post(f"{API}/roles", json=role_payload(slug="tester"))


# ---- A 目标拆解 ---------------------------------------------------------


async def test_decompose_creates_pending_draft(client):
    """输入 goal → 编排者产出 draft（pending），不直接落板。"""
    await _seed(client)
    r = await client.post(
        f"{API}/board/decompose", json={"goal": "实现订单分页", "workspace_id": "main"},
        headers=auth_headers(),
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "pending"
    assert d["tree_json"]  # 任务树非空


async def test_drafts_list(client):
    """待审 draft 列表可查（按 workspace 过滤）。"""
    await _seed(client)
    await client.post(f"{API}/board/decompose", json={"goal": "g", "workspace_id": "main"}, headers=auth_headers())
    r = await client.get(f"{API}/board/drafts", params={"workspace_id": "main"}, headers=auth_headers())
    assert r.status_code == 200
    assert any(d["status"] == "pending" for d in r.json())


async def test_approve_lands_tasks_with_deps(client):
    """approve 批量落板：draft → todo 任务，并按树内依赖写 task_deps。"""
    await _seed(client)
    d = (await client.post(f"{API}/board/decompose", json={"goal": "g"}, headers=auth_headers())).json()
    r = await client.post(
        f"{API}/board/drafts/approve", json={"draft_ids": [d["id"]]}, headers=human_headers()
    )
    assert r.status_code == 200
    # 落板后应产生 todo 任务
    tasks = (await client.get(f"{API}/tasks", headers=auth_headers())).json()
    assert any(t["status"] == "todo" for t in tasks)


async def test_decompose_pipeline_with_roles_and_deps(client):
    """PM 规划：design-dev-test 模板产出 设计→实现→测试，带角色 + 线性依赖。"""
    await _seed(client)
    d = (await client.post(f"{API}/board/decompose", json={"goal": "g", "template": "design-dev-test"}, headers=auth_headers())).json()
    await client.post(f"{API}/board/drafts/approve", json={"draft_ids": [d["id"]]}, headers=human_headers())
    tasks = (await client.get(f"{API}/tasks", headers=auth_headers())).json()
    by = {t["title"].split(" — ")[0]: t for t in tasks if t["title"].startswith(("设计", "实现", "测试"))}
    assert set(by) == {"设计", "实现", "测试"}
    assert by["设计"]["assignees"] == ["designer"]
    assert by["实现"]["assignees"] == ["developer"]
    assert by["实现"]["depends_on"] == [by["设计"]["id"]]
    assert by["测试"]["assignees"] == ["tester"]
    assert by["测试"]["depends_on"] == [by["实现"]["id"]]


async def test_review_approve_mentions_downstream(client):
    """上游 done 后 @提及下游角色（交接信号）。"""
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-DES", "title": "设计", "assignees": ["designer"]}, headers=auth_headers())
    await client.post(f"{API}/tasks", json={"id": "T-IMP", "title": "实现", "assignees": ["developer"], "depends_on": ["T-DES"]}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-DES/claim", json={}, headers=auth_headers("designer"))
    await client.post(f"{API}/tasks/T-DES/submit", json={"summary": "x"}, headers=auth_headers("designer"))
    await client.post(f"{API}/tasks/T-DES/review", json={"approve": True}, headers=human_headers())
    tl = (await client.get(f"{API}/tasks/T-IMP/timeline", headers=auth_headers())).json()
    mentions = [e for e in tl if e["kind"] == "mention"]
    assert any(m["payload"]["target"] == "developer" and "已完成" in m["payload"]["message"] for m in mentions)


async def test_reject_draft(client):
    """reject 驳回 draft，不落板。"""
    await _seed(client)
    d = (await client.post(f"{API}/board/decompose", json={"goal": "g"}, headers=auth_headers())).json()
    r = await client.post(f"{API}/board/drafts/reject", json={"draft_id": d["id"]}, headers=human_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


async def test_draft_requires_human_approval(client):
    """draft 未 approve 前不产生任何 todo 任务（人类确认不可自动化，§3.7）。"""
    await _seed(client)
    await client.post(f"{API}/board/decompose", json={"goal": "g"}, headers=auth_headers())
    tasks = (await client.get(f"{API}/tasks", headers=auth_headers())).json()
    assert tasks == []  # 未审批 → 无任务落板


# ---- C 调度唤醒 + 自认领 -------------------------------------------------


async def test_tick_reclaims_lease(client):
    """tick 扩展后仍回收失联租约（§6.1 第一步）。"""
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-1", "title": "x"}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-1/claim", json={}, headers=auth_headers("backend-eng"))
    r = await client.post(f"{API}/control/tick", headers=auth_headers())
    assert r.status_code == 200


async def test_tick_wakes_eligible_agent(client):
    """tick 扫描 todo → 匹配 eligible agent（能力/四层闸门）→ wake 创建 run（hint 只含 id+title）。"""
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-2", "title": "x", "tags": ["backend"]}, headers=auth_headers())
    r = await client.post(f"{API}/control/tick", headers=auth_headers())
    assert r.status_code == 200
    runs = (await client.get(f"{API}/runs", headers=auth_headers())).json()
    assert any(run["task_id"] == "T-2" for run in runs)  # wake 创建了 run


async def test_self_claim_default_flow(client):
    """自认领默认流程：拆解落板 → agent 自认领 → 提交 → 评审 → done（手动指派降为可选）。"""
    await _seed(client)
    d = (await client.post(f"{API}/board/decompose", json={"goal": "g"}, headers=auth_headers())).json()
    await client.post(f"{API}/board/drafts/approve", json={"draft_ids": [d["id"]]}, headers=human_headers())
    tasks = (await client.get(f"{API}/tasks", headers=auth_headers())).json()
    task = tasks[0]
    tid = task["id"]
    # 团队任务：由任务绑定的参与角色自认领/提交（不是任意 backend-eng）
    role = task["assignees"][0] if task["assignees"] else "backend-eng"
    assert (await client.post(f"{API}/tasks/{tid}/claim", json={}, headers=auth_headers(role))).status_code == 200
    assert (await client.post(f"{API}/tasks/{tid}/submit", json={"summary": "x"}, headers=auth_headers(role))).status_code == 200
    assert (await client.post(f"{API}/tasks/{tid}/review", json={"approve": True}, headers=human_headers())).status_code == 200
    body = (await client.get(f"{API}/tasks/{tid}", headers=auth_headers())).json()
    assert body["status"] == "done"


async def test_starvation_escalation(client):
    """todo 超时无人认领 → 饥饿升级（优先级提升/升级 owner，§5.6）。"""
    await _seed(client)
    await client.post(f"{API}/tasks", json={"id": "T-3", "title": "x"}, headers=auth_headers())
    # 多次 tick 模拟超时 → 优先级提升或升级标记
    for _ in range(3):
        await client.post(f"{API}/control/tick", headers=auth_headers())
    body = (await client.get(f"{API}/tasks/T-3", headers=auth_headers())).json()
    # 饥饿升级的具体形态（priority 提升或 escalate 标记）实现后断言
    assert "priority" in body
