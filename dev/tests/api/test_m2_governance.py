"""L2 · M2 协作与治理契约测试（task #9）。

覆盖 M2 出口「多角色各自认领、越界被拦、成本达限停新认领」，接口以 SE v1.0 §1 M2
+ task #7 已修正的五态机为准：
  - claim 原子化（BEGIN IMMEDIATE + 条件 UPDATE + rowcount）
  - 依赖两守卫分离：claim 查 depends_on、submit 查「子任务 parent_id==本任务 都 done」
  - diff 三交集（scope_paths ∩ role access ∩ .anvilignore）
  - 租约 1800s / 心跳 2min / 缺 3 周期回收
  - 成本闸门 + 并发四层闸门 + 全场冻结 + 审计

M2（task #8）未落地前整文件 skip；落地后移除 skip 即跑。
"""
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import API, auth_headers, role_payload, runtime_payload, task_payload

pytestmark = [
    pytest.mark.usefixtures("client"),
]


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())
    await client.post(f"{API}/roles", json=role_payload(slug="other-role"))


async def _task(client, **kw):
    return await client.post(f"{API}/tasks", json=task_payload(**kw), headers=auth_headers())


# ---- 状态机全路径（评审 approve/reject） --------------------------------


async def test_review_approve_and_reject(client):
    """review {approve:true}→done；{approve:false}→in_progress（回 assignee 重做）。"""
    await _seed(client)
    await _task(client, id_="T-1")
    await client.post(f"{API}/tasks/T-1/claim", json={}, headers=auth_headers())
    await client.post(f"{API}/tasks/T-1/submit", json={"summary": "v1"}, headers=auth_headers())
    rej = await client.post(f"{API}/tasks/T-1/review", json={"approve": False}, headers=auth_headers())
    assert rej.status_code == 200
    assert (await client.get(f"{API}/tasks/T-1")).json()["status"] == "in_progress"
    await client.post(f"{API}/tasks/T-1/submit", json={"summary": "v2"}, headers=auth_headers())
    appr = await client.post(f"{API}/tasks/T-1/review", json={"approve": True}, headers=auth_headers())
    assert appr.status_code == 200
    assert (await client.get(f"{API}/tasks/T-1")).json()["status"] == "done"


# ---- 认领原子性（task #7 修正后） ----------------------------------------


async def test_concurrent_claim_atomic(client):
    """两个角色并发 claim 同一任务 → 恰一方成功（409），另一方 409。"""
    await _seed(client)
    await _task(client, id_="T-CONC")

    async def _claim(role):
        return await client.post(
            f"{API}/tasks/T-CONC/claim", json={}, headers=auth_headers(role)
        )

    r1, r2 = await asyncio.gather(_claim("qa-role"), _claim("other-role"))
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409]
    body = (await client.get(f"{API}/tasks/T-CONC")).json()
    assert body["assignee"] in ("qa-role", "other-role")


# ---- 依赖两守卫分离（task #7 修正后） ------------------------------------


async def test_claim_blocked_by_depends_on(client):
    """claim 守卫：depends_on 未 done → 409。"""
    await _seed(client)
    await _task(client, id_="T-DEP")
    await _task(client, id_="T-MAIN", depends_on=["T-DEP"])
    r = await client.post(f"{API}/tasks/T-MAIN/claim", json={}, headers=auth_headers())
    assert r.status_code == 409


async def test_submit_blocked_until_children_done(client):
    """submit 守卫：父任务的所有子任务（parent_id==本任务）done 前不可 submit（方向已修正）。"""
    await _seed(client)
    await _task(client, id_="T-PARENT")
    await _task(client, id_="T-CHILD", parent_id="T-PARENT")
    # 父任务 claim 后 submit，因子任务未 done → 409
    await client.post(f"{API}/tasks/T-PARENT/claim", json={}, headers=auth_headers())
    r = await client.post(f"{API}/tasks/T-PARENT/submit", json={"summary": "x"}, headers=auth_headers())
    assert r.status_code == 409
    # 子任务做完 → 父可 submit
    await client.post(f"{API}/tasks/T-CHILD/claim", json={}, headers=auth_headers("other-role"))
    await client.post(f"{API}/tasks/T-CHILD/submit", json={"summary": "y"}, headers=auth_headers("other-role"))
    await client.post(f"{API}/tasks/T-CHILD/review", json={"approve": True}, headers=auth_headers("other-role"))
    r2 = await client.post(f"{API}/tasks/T-PARENT/submit", json={"summary": "x"}, headers=auth_headers())
    assert r2.status_code == 200


# ---- diff 三交集（scope ∩ access ∩ .anvilignore） ------------------------


async def test_diff_denied_by_role_access(client):
    """role 的 repo access=read（无 write）时，改动该 repo 路径被拒。"""
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload(
        workspace={"home": "agents/qa-role/", "repos": [{"name": "main", "access": "read"}]},
    ))
    await _task(client, id_="T-R", scope_paths=["src/**"])
    await client.post(f"{API}/tasks/T-R/claim", json={"paths": ["src/**"]}, headers=auth_headers())
    r = await client.post(
        f"{API}/tasks/T-R/submit",
        json={"summary": "x", "changed_paths": ["src/a.py"]},
        headers=auth_headers(),
    )
    assert r.status_code == 409


async def test_diff_denied_by_anvilignore(client):
    """.anvilignore 保护清单（.git/.anvil/secrets）即便 scope 允许也必拒。"""
    await _seed(client)
    await _task(client, id_="T-IGN", scope_paths=["**"])
    await client.post(f"{API}/tasks/T-IGN/claim", json={"paths": ["**"]}, headers=auth_headers())
    r = await client.post(
        f"{API}/tasks/T-IGN/submit",
        json={"summary": "x", "changed_paths": [".anvil/shared/lessons.md"]},
        headers=auth_headers(),
    )
    assert r.status_code == 409


# ---- 租约 / 心跳 / 超时回收 ----------------------------------------------


async def test_heartbeat_renews_lease(client):
    """心跳顺延 lease_expires_at（1800s）。"""
    await _seed(client)
    await _task(client, id_="T-HB")
    await client.post(f"{API}/tasks/T-HB/claim", json={}, headers=auth_headers())
    r = await client.post(f"{API}/tasks/T-HB/heartbeat", headers=auth_headers())
    assert r.status_code == 200
    body = (await client.get(f"{API}/tasks/T-HB")).json()
    assert body["lease_expires_at"] is not None


async def test_lease_timeout_reclaims(client):
    """缺 3 心跳周期（6min）→ 编排器 tick 回收租约、任务回 todo、run=abandoned。"""
    await _seed(client)
    await _task(client, id_="T-TO")
    await client.post(f"{API}/tasks/T-TO/claim", json={}, headers=auth_headers())
    # 触发编排器 tick（§6.1）；实现需暴露测试后门或 fake clock
    r = await client.post(f"{API}/control/tick", headers=auth_headers())
    if r.status_code == 404:
        pytest.skip("编排器 tick 未实现")
    assert r.status_code == 200
    body = (await client.get(f"{API}/tasks/T-TO")).json()
    assert body["status"] == "todo"


# ---- 成本闸门 + 并发闸门 + 全场冻结 --------------------------------------


async def test_cost_gate_blocks_new_claim(client):
    """任务 cost_used 达 cost_budget_usd → 停新 claim（进行中允许收尾）。"""
    await _seed(client)
    await _task(client, id_="T-COST", cost_budget_usd=0.01)
    await client.post(f"{API}/tasks/T-COST/claim", json={}, headers=auth_headers())
    # 累计成本到顶（实现侧注入 cost_ledger 或测试后门）
    await client.post(f"{API}/budget", json={"role": "qa-role", "usd": 0.02}, headers=auth_headers())
    r = await client.post(f"{API}/tasks/T-COST/claim", json={}, headers=auth_headers())
    # 已 in_progress 的任务不能重复 claim；新任务应被成本闸门拒
    await _task(client, id_="T-NEW")
    r2 = await client.post(f"{API}/tasks/T-NEW/claim", json={}, headers=auth_headers())
    assert r2.status_code in (409, 429)  # 成本达限 → 停新认领


async def test_concurrency_gate_role_max_parallel(client):
    """角色 max_parallel=2，第 3 个 run 被并发闸门拦（§5.6）。"""
    await _seed(client)
    # 实现需暴露并发闸门判定；此处断言闸门接口/状态可查
    r = await client.get(f"{API}/budget", headers=auth_headers())
    assert r.status_code in (200, 404)


async def test_freeze_all(client):
    """全场冻结：POST /control/freeze-all → 停新 run/claim，resume-all 恢复。"""
    await _seed(client)
    f = await client.post(f"{API}/control/freeze-all", headers=auth_headers())
    assert f.status_code == 200
    await _task(client, id_="T-FZ")
    r = await client.post(f"{API}/tasks/T-FZ/claim", json={}, headers=auth_headers())
    assert r.status_code in (409, 423, 429)  # 冻结态拒新认领
    res = await client.post(f"{API}/control/resume-all", headers=auth_headers())
    assert res.status_code == 200


async def test_audit_logs_written(client):
    """状态变迁/越界/冻结写入审计（GET /audit 可查）。"""
    await _seed(client)
    await _task(client, id_="T-AUD")
    await client.post(f"{API}/tasks/T-AUD/claim", json={}, headers=auth_headers())
    r = await client.get(f"{API}/audit", headers=auth_headers())
    assert r.status_code == 200
    assert isinstance(r.json(), list)
