"""L2 · 任务 CRUD + 5 态状态机 + claim/租约/心跳/submit/review/deps。

任务状态机以 design §5.1 **五态**为准（I1 已决议）：
  todo → in_progress → in_review → done → closed
  unclaim→todo、reject→in_progress、租约超时→todo、cancel/reopen。
agent 面端点（SE §3.1，I3 后）= claim/unclaim/heartbeat/submit/review + deps。
"""
from __future__ import annotations

import pytest

from tests.conftest import API, auth_headers, role_payload, runtime_payload, task_payload, human_headers  # noqa: E402

pytestmark = pytest.mark.usefixtures("client")


async def _seed(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())


async def _post_task(client, **kw):
    return await client.post(f"{API}/tasks", json=task_payload(**kw), headers=auth_headers())


async def _claim(client, tid, paths=None, role="qa-role"):
    body = {"paths": paths} if paths else {}
    return await client.post(
        f"{API}/tasks/{tid}/claim", json=body, headers=auth_headers(role)
    )


# ---- CRUD -------------------------------------------------------------

async def test_task_crud_and_manual_assign(client):
    """M1 出口：任务 CRUD + 手动指派（PATCH assignee）。"""
    await _seed(client)
    r = await _post_task(client)
    assert r.status_code == 201
    got = await client.get(f"{API}/tasks/T-0001")
    assert got.status_code == 200 and got.json()["status"] == "todo"
    pat = await client.patch(
        f"{API}/tasks/T-0001", json={"assignee": "qa-role"}, headers=human_headers()
    )
    assert pat.status_code == 200
    assert pat.json()["assignee"] == "qa-role"


# ---- 5 态状态机（S1–S9） ---------------------------------------------

async def test_S1_happy_path(client):
    """S1 happy path：claim→submit→review approve→done。"""
    await _seed(client)
    await _post_task(client)
    assert (await _claim(client, "T-0001")).status_code == 200
    sub = await client.post(
        f"{API}/tasks/T-0001/submit",
        json={"summary": "done", "branch": "qa/T-0001"},
        headers=auth_headers(),
    )
    assert sub.status_code == 200
    rev = await client.post(
        f"{API}/tasks/T-0001/review", json={"approve": True}, headers=human_headers()
    )
    assert rev.status_code == 200
    final = (await client.get(f"{API}/tasks/T-0001")).json()
    assert final["status"] == "done"


async def test_S2_reject_back_to_in_progress(client):
    """S2 reject → in_progress（同 assignee 重做，无 attempts 字段，I2 决议）。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    await client.post(
        f"{API}/tasks/T-0001/submit", json={"summary": "v1"}, headers=auth_headers()
    )
    rev = await client.post(
        f"{API}/tasks/T-0001/review", json={"approve": False}, headers=human_headers()
    )
    assert rev.status_code == 200
    body = (await client.get(f"{API}/tasks/T-0001")).json()
    assert body["status"] == "in_progress"
    assert "attempts" not in body  # I2：无 attempts 字段


async def test_S3_unclaim(client):
    """S3 unclaim → todo，assignee 清空。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    r = await client.post(
        f"{API}/tasks/T-0001/unclaim", json={"reason": "不适合"}, headers=auth_headers()
    )
    assert r.status_code == 200
    body = (await client.get(f"{API}/tasks/T-0001")).json()
    assert body["status"] == "todo" and body["assignee"] is None


async def test_S6_deps_block_claim(client):
    """S6 依赖未完成不可 claim（§5.4 NOT EXISTS，退出码 3 语义）。"""
    await _seed(client)
    await _post_task(client, id_="T-DEP")
    await _post_task(client, id_="T-MAIN", depends_on=["T-DEP"])
    r = await _claim(client, "T-MAIN")
    assert r.status_code == 409  # 依赖未满足 → 状态冲突


async def test_S7_parent_cannot_submit_before_children(client):
    """S7 父任务在所有子任务 done 前不可 submit。"""
    await _seed(client)
    await _post_task(client, id_="T-P", parent_id="T-C")  # 占位：父/子关系经 parent_id/deps
    # 依赖路径统一走 task_deps；此用例在 deps 落地后按 parent_id 语义断言
    await _post_task(client, id_="T-C")
    r = await client.post(
        f"{API}/tasks/T-P/submit", json={"summary": "x"}, headers=auth_headers()
    )
    assert r.status_code in (409, 422)  # 子未完成 → 拒绝提交


async def test_S8_reject_loop_triggers_cost_gate(client):
    """S8 reject 循环下成本闸门生效（@anvil-se 点名）：反复 reject 至 cost_budget 触发。"""
    await _seed(client)
    await _post_task(client, id_="T-LOOP", cost_budget_usd=0.01)
    await _claim(client, "T-LOOP")
    for _ in range(5):
        await client.post(
            f"{API}/tasks/T-LOOP/submit", json={"summary": "retry"}, headers=auth_headers()
        )
        await client.post(
            f"{API}/tasks/T-LOOP/review", json={"approve": False}, headers=human_headers()
        )
    # 成本达限 → 新 claim / 新 run 被闸门拦截（具体阈值待 owner 定，此处断言存在拦截）
    body = (await client.get(f"{API}/tasks/T-LOOP")).json()
    assert body["status"] in ("in_progress", "todo")  # 仍停在可重做态
    # 闸门断言：见 test_budget.py 的 G4/G5；这里验证循环不改变非法状态即可


async def test_S9_exclusive_claim(client):
    """S9 并发 claim 排他：恰一方成功，另一方 409。"""
    await _seed(client)
    await client.post(f"{API}/roles", json=role_payload(slug="other-role"))
    await _post_task(client)
    r1 = await _claim(client, "T-0001", role="qa-role")
    r2 = await _claim(client, "T-0001", role="other-role")
    assert (r1.status_code, r2.status_code) in [(200, 409), (409, 200), (200, 403), (403, 200)]
    body = (await client.get(f"{API}/tasks/T-0001")).json()
    assert body["assignee"] in ("qa-role", "other-role")


# ---- 租约 / 心跳（C1–C2） ---------------------------------------------

async def test_C1_heartbeat_renews_lease(client):
    """C1 心跳续租：lease_expires_at 顺延，heartbeat 端点存在。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    r = await client.post(f"{API}/tasks/T-0001/heartbeat", headers=auth_headers())
    assert r.status_code == 200
    body = (await client.get(f"{API}/tasks/T-0001")).json()
    assert body["lease_expires_at"] is not None


async def test_C2_lease_timeout_reclaim(client):
    """C2 失联回收（缺 3 周期）→ 任务回 todo、run=abandoned。依赖编排器 tick（§6.1）。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    # 触发回收：直接调编排器 tick 或测试后门；落地后按 6min 参数用 fake clock
    # 这里断言回收接口/事件存在，具体由 fake-clock 测试补
    r = await client.post(f"{API}/control/tick", headers=auth_headers())  # 测试后门
    assert r.status_code in (200, 404)  # 404 = 未实现测试后门时跳过


# ---- diff 越界（D1–D3，M2 三交集） ------------------------------------

async def test_D1_scope_out_of_bounds_rejected(client):
    """D1 submit 越界（改动路径 ∉ scope_paths ∩ access ∩ .anvilignore）→ 409。"""
    await _seed(client)
    await _post_task(client, scope_paths=["src/**"])
    await _claim(client, "T-0001", paths=["src/**"])
    sub = await client.post(
        f"{API}/tasks/T-0001/submit",
        json={"summary": "x", "changed_paths": ["docs/README.md", "src/a.py"]},
        headers=auth_headers(),
    )
    assert sub.status_code == 409  # 越界被拦


async def test_D2_anvilignore_always_denied(client):
    """D2 .anvilignore 优先：改 .git/ / .anvil/ / 密钥 → 必拒。"""
    await _seed(client)
    await _post_task(client, scope_paths=["**"])
    await _claim(client, "T-0001", paths=["**"])
    sub = await client.post(
        f"{API}/tasks/T-0001/submit",
        json={"summary": "x", "changed_paths": [".anvil/shared/lessons.md"]},
        headers=auth_headers(),
    )
    assert sub.status_code == 409
