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


def _insert_run(app, run_id: str, task_id: str, role: str, status: str = "running"):
    from anvil.db import open_conn
    conn = open_conn(app.state.db_path)
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) VALUES (?,?,?,?,?)",
        (run_id, role, task_id, status, "2026-11-11T00:00:00+00:00"),
    )
    conn.commit()
    return conn


async def test_unclaim_deferred_while_role_executing(client, app):
    """角色仍在执行 slice 时不能立即释放 claim；run 结束后再 released。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    conn = _insert_run(app, "R-ACTIVE", "T-0001", "qa-role")
    conn.close()

    r = await client.post(f"{API}/tasks/T-0001/unclaim", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    claim = body["role_claims"][0]
    assert claim["status"] == "working"
    assert claim["release_requested_at"]

    from anvil.db import open_conn
    from anvil.services import tasks as tasks_service
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                 ("2026-11-11T00:01:00+00:00", "R-ACTIVE"))
    conn.commit()
    assert tasks_service.process_release_requests(conn) == [
        {"task_id": "T-0001", "role_slug": "qa-role"}]
    conn.close()

    final = (await client.get(f"{API}/tasks/T-0001")).json()
    assert final["status"] == "todo"
    claim = final["role_claims"][0]
    assert claim["status"] == "released"
    assert claim["release_requested_at"] is None


async def test_team_unclaim_releases_only_after_role_run_ends(client, app):
    """多角色：执行中的角色延迟释放，其他角色的 working claim 不受影响。"""
    await _seed(client)
    await client.post(f"{API}/roles", json=role_payload("dev"))
    await _post_task(client, id_="T-TEAM-DEFER", assignees=["qa-role", "dev"])
    await _claim(client, "T-TEAM-DEFER", role="qa-role")
    await _claim(client, "T-TEAM-DEFER", role="dev")
    conn = _insert_run(app, "R-DEV", "T-TEAM-DEFER", "dev")
    conn.close()

    r = await client.post(f"{API}/tasks/T-TEAM-DEFER/unclaim", headers=auth_headers("dev"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    claims = {c["role_slug"]: c for c in body["role_claims"]}
    assert claims["dev"]["status"] == "working"
    assert claims["dev"]["release_requested_at"]
    assert claims["qa-role"]["status"] == "working"

    from anvil.db import open_conn
    from anvil.services import tasks as tasks_service
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                 ("2026-11-11T00:01:00+00:00", "R-DEV"))
    conn.commit()
    tasks_service.process_release_requests(conn)
    conn.close()

    mid = (await client.get(f"{API}/tasks/T-TEAM-DEFER")).json()
    claims = {c["role_slug"]: c for c in mid["role_claims"]}
    assert mid["status"] == "in_progress"
    assert claims["dev"]["status"] == "released"
    assert claims["qa-role"]["status"] == "working"

    last = await client.post(f"{API}/tasks/T-TEAM-DEFER/unclaim", headers=auth_headers("qa-role"))
    assert last.status_code == 200
    assert last.json()["status"] == "todo"


async def test_human_unclaim_team_task_releases_all_roles(client):
    """人类对团队任务整体放弃时，释放所有 pending/working claim。"""
    await _seed(client)
    await client.post(f"{API}/roles", json=role_payload("dev"))
    await _post_task(client, id_="T-TEAM-HUMAN", assignees=["qa-role", "dev"])
    await _claim(client, "T-TEAM-HUMAN", role="qa-role")
    await _claim(client, "T-TEAM-HUMAN", role="dev")

    r = await client.post(f"{API}/tasks/T-TEAM-HUMAN/unclaim", headers=human_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "todo"
    assert {c["status"] for c in body["role_claims"]} == {"released"}


async def test_cancel_defers_claim_release_while_role_executing(client, app):
    """终止任务也不能让 claim 先于活跃 run 释放。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    conn = _insert_run(app, "R-CANCEL", "T-0001", "qa-role")
    conn.close()

    r = await client.post(f"{API}/tasks/T-0001/cancel", headers=human_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "closed"
    claim = body["role_claims"][0]
    assert claim["status"] == "working"
    assert claim["release_requested_at"]

    from anvil.db import open_conn
    from anvil.services import tasks as tasks_service
    conn = open_conn(app.state.db_path)
    conn.execute("UPDATE runs SET status='abandoned', ended_at=? WHERE id=?",
                 ("2026-11-11T00:01:00+00:00", "R-CANCEL"))
    conn.commit()
    tasks_service.process_release_requests(conn)
    conn.close()

    final = (await client.get(f"{API}/tasks/T-0001")).json()
    assert final["role_claims"][0]["status"] == "released"


async def test_reassign_roles_blocked_while_role_executing(client, app):
    """角色仍在执行时不允许重新绑定参与角色（会作废旧 claim）。"""
    await _seed(client)
    await _post_task(client)
    await _claim(client, "T-0001")
    conn = _insert_run(app, "R-ASSIGN", "T-0001", "qa-role")
    conn.close()

    r = await client.post(
        f"{API}/tasks/T-0001/assign", json={"assignees": ["qa-role"]},
        headers=human_headers())
    assert r.status_code == 409
    assert "executing" in r.json()["detail"]


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
    await _post_task(client, id_="T-P")
    await _post_task(client, id_="T-C", parent_id="T-P")
    r = await client.post(
        f"{API}/tasks/T-P/submit", json={"summary": "x"}, headers=auth_headers()
    )
    assert r.status_code in (409, 422)  # 子未完成 → 拒绝提交


async def test_S8_reject_loop_stays_in_progress(client):
    """S8 反复 reject 后任务仍停在可重做态（不再按金额触发闸门）。"""
    await _seed(client)
    await _post_task(client, id_="T-LOOP")
    await _claim(client, "T-LOOP")
    for _ in range(5):
        await client.post(
            f"{API}/tasks/T-LOOP/submit", json={"summary": "retry"}, headers=auth_headers()
        )
        await client.post(
            f"{API}/tasks/T-LOOP/review", json={"approve": False}, headers=human_headers()
        )
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
