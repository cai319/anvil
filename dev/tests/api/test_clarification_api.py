"""Cindy 规划前澄清 API：dispatch 路由、门禁、确认/跳过、stale。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import (  # noqa: E402
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)


@pytest.fixture
async def seeded(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "developer", "tester"):
        await client.post(
            f"{API}/roles", json=role_payload(slug), headers=auth_headers())


async def _create_task(client, tid: str = "T-CLAR") -> str:
    r = await client.post(
        f"{API}/tasks",
        json=task_payload(id=tid, assignees=["pm", "developer"]),
        headers=auth_headers())
    assert r.status_code == 201, r.text
    return tid


async def _dispatch(client, tid: str) -> dict:
    r = await client.post(f"{API}/tasks/{tid}/dispatch", headers=human_headers())
    assert r.status_code == 201, r.text
    return r.json()


def _brief() -> dict:
    return {
        "goal": "实现登录",
        "true_intent": "让用户能安全登录",
        "non_goals": ["不做注册"],
        "constraints": ["沿用现有会话表"],
        "acceptance": ["登录成功返回 token"],
        "assumptions": ["已有用户表"],
        "open_questions": [],
        "weight": "standard",
        "weight_rationale": "中等风险",
        "plan_shape": "auto",
        "plan_shape_rationale": "按 scope 判断",
        "risks": ["会话固定"],
    }


async def test_dispatch_starts_clarification_and_blocks_drafts_and_runs(
        client, seeded):
    tid = await _create_task(client)
    body = await _dispatch(client, tid)
    assert body["mode"] == "clarifying"
    assert body["clarification"]["status"] == "in_progress"

    draft = await client.post(
        f"{API}/board/drafts", headers=human_headers(), json={
            "goal": "g", "source_task": tid, "kind": "workflow",
            "tree": {"steps": [
                {"id": "S1", "title": "实现", "role": "developer"}]},
        })
    assert draft.status_code == 409, draft.text
    assert "clarification" in draft.text

    run = await client.post(
        f"{API}/runs", json={"role_slug": "developer", "task_id": tid},
        headers=human_headers())
    assert run.status_code == 409, run.text


async def test_mention_continues_clarification(client, seeded):
    tid = await _create_task(client)
    await _dispatch(client, tid)
    r = await client.post(
        f"{API}/tasks/{tid}/mention", headers=human_headers(),
        json={"target": "cindy", "message": "补充：必须支持 SSO"})
    assert r.status_code == 200, r.text

    state = (await client.get(
        f"{API}/tasks/{tid}/clarification", headers=human_headers())).json()
    assert state["status"] == "in_progress"
    assert state["round"] == 2

    runs = (await client.get(f"{API}/runs", headers=human_headers())).json()
    clarify_runs = [x for x in runs if x.get("task_id") == tid
                    and x.get("origin") == "clarify"]
    assert len(clarify_runs) >= 2
    assert any("补充：必须支持 SSO" in (x.get("prompt") or "")
               for x in clarify_runs)


async def test_confirm_starts_planner_with_brief(client, seeded):
    tid = await _create_task(client)
    await _dispatch(client, tid)
    draft = await client.put(
        f"{API}/tasks/{tid}/clarification/draft", headers=human_headers(),
        json={"brief": _brief()})
    assert draft.status_code == 200, draft.text

    confirm = await client.post(
        f"{API}/tasks/{tid}/clarification/confirm", headers=human_headers(),
        json={"acknowledge_open_questions": True})
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["clarification"]["status"] == "confirmed"
    assert "[anvil:plan]" in body["run"]["prompt"]
    assert "已确认任务简报" in body["run"]["prompt"]

    task = (await client.get(
        f"{API}/tasks/{tid}", headers=auth_headers())).json()
    assert task["workflow_weight"] == "standard"
    assert task["workflow_weight_source"] == "human"
    assert task["clarification_status"] == "confirmed"


async def test_skip_starts_planner_without_brief(client, seeded):
    tid = await _create_task(client)
    await _dispatch(client, tid)
    skip = await client.post(
        f"{API}/tasks/{tid}/clarification/skip", headers=human_headers(),
        json={"reason": "需求明确"})
    assert skip.status_code == 200, skip.text
    body = skip.json()
    assert body["clarification"]["status"] == "skipped"
    assert "[anvil:plan]" in body["run"]["prompt"]
    assert "已确认任务简报" not in body["run"]["prompt"]


async def test_task_change_marks_confirmed_brief_stale(client, seeded):
    tid = await _create_task(client)
    await _dispatch(client, tid)
    await client.put(
        f"{API}/tasks/{tid}/clarification/draft", headers=human_headers(),
        json={"brief": _brief()})
    confirmed = await client.post(
        f"{API}/tasks/{tid}/clarification/confirm", headers=human_headers(),
        json={})
    assert confirmed.status_code == 200, confirmed.text

    patch = await client.patch(
        f"{API}/tasks/{tid}", headers=human_headers(),
        json={"description": "改成新的描述"})
    assert patch.status_code == 200, patch.text
    state = (await client.get(
        f"{API}/tasks/{tid}/clarification", headers=human_headers())).json()
    assert state["status"] == "stale"


def _agent_session(app, role: str, task_id: str) -> dict:
    """真实运行级 token：让 planner agent 走 CLI 的草稿写入路径。"""
    from anvil.db import open_conn
    from anvil.util import now_iso

    token = f"tok-{role}-{task_id}"
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (token, role_slug, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?)", (token, role, task_id, now_iso(), None))
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


async def test_planner_agent_can_write_brief_draft(client, seeded, app):
    tid = await _create_task(client)
    await _dispatch(client, tid)
    headers = _agent_session(app, "cindy", tid)
    resp = await client.put(
        f"{API}/tasks/{tid}/clarification/draft", headers=headers,
        json={"brief": _brief()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["brief"]["goal"] == "实现登录"


async def test_confirm_replaces_suspended_clarify_run(client, seeded, app):
    """确认时必须放弃挂起的 clarify run，并创建真正的 planner run。"""
    from anvil.db import open_conn

    tid = await _create_task(client)
    await _dispatch(client, tid)
    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "UPDATE runs SET status='suspended' WHERE task_id=? AND "
            "role_slug='cindy' AND origin='clarify'", (tid,))
        conn.commit()
    finally:
        conn.close()

    await client.put(
        f"{API}/tasks/{tid}/clarification/draft", headers=human_headers(),
        json={"brief": _brief()})
    confirmed = await client.post(
        f"{API}/tasks/{tid}/clarification/confirm", headers=human_headers(),
        json={})
    assert confirmed.status_code == 200, confirmed.text
    assert "[anvil:plan]" in confirmed.json()["run"]["prompt"]

    conn = open_conn(app.state.db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT status, origin, prompt FROM runs WHERE task_id=? "
            "AND role_slug='cindy' ORDER BY started_at", (tid,))]
    finally:
        conn.close()
    clarify_rows = [r for r in rows if r["origin"] == "clarify"]
    plan_rows = [r for r in rows if "[anvil:plan]" in (r["prompt"] or "")]
    assert clarify_rows and all(r["status"] == "abandoned" for r in clarify_rows)
    assert plan_rows


async def test_confirmed_weight_normalizes_workflow_draft(client, seeded):
    tid = await _create_task(client)
    await _dispatch(client, tid)
    await client.put(
        f"{API}/tasks/{tid}/clarification/draft", headers=human_headers(),
        json={"brief": _brief()})
    confirmed = await client.post(
        f"{API}/tasks/{tid}/clarification/confirm", headers=human_headers(),
        json={"weight": "light", "plan_shape": "auto"})
    assert confirmed.status_code == 200, confirmed.text

    draft = await client.post(
        f"{API}/board/drafts", headers=human_headers(), json={
            "goal": "g", "source_task": tid, "kind": "workflow",
            "tree": {"weight": "strict", "steps": [
                {"id": "S1", "title": "需求", "role": "pm"},
                {"id": "S2", "title": "实现", "role": "developer"},
            ]},
        })
    assert draft.status_code == 201, draft.text
    assert draft.json()["tree"]["weight"] == "light"
