"""问题板（Issue Board）：reviewer CLI/API 填写、human 逐条确认、任务最终闸门。"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import (
    API, auth_headers, human_headers, role_payload, runtime_payload, task_payload,
)

pytestmark = pytest.mark.usefixtures("client")


def _agent_headers(app, role: str, token: str) -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


def finding_payload(**over) -> dict:
    payload = {
        "title": "命令行参数注入导致任意命令执行",
        "risk_type": "injection",
        "severity": "critical",
        "module": "backend/api",
        "function_name": "run_command",
        "file_path": "backend/anvil/api/tasks.py",
        "line_numbers": "120-126",
        "preconditions": "攻击者能控制 task title 并触发 run_command",
        "reasoning_chain": "title 未过滤进入 shell=True；随后拼接执行。",
        "risk_description": "可执行任意本机命令，拿到当前用户权限。",
        "attack_path": "消息入口：task title -> source: title -> sink: subprocess(shell=True)",
        "trigger_point": "创建任务时传入 title",
        "poc_status": "not_verified",
        "poc": "当前环境未安装目标二进制，尚未构造可复现 PoC",
        "fix_priority": "P0",
        "fix_suggestion": "使用参数数组并禁止 shell=True；对 title 做白名单校验。",
        "confidence": 88,
    }
    payload.update(over)
    return payload


async def _seed(client, app, tid="T-IB"):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("auditor"), headers=auth_headers())
    r = await client.post(f"{API}/tasks", json={
        **task_payload(id_=tid),
        "assignees": ["dev", "auditor"],
        "issue_board_enabled": True,
    }, headers=human_headers())
    assert r.status_code == 201, r.text
    added = await client.post(f"{API}/tasks/{tid}/reviewers",
                              json={"roles": ["auditor"]}, headers=human_headers())
    assert added.status_code == 200, added.text
    # 测试里不走 Cindy/workflow：直接把任务置为进行中，board 完成时走无 workflow 兜底。
    patched = await client.patch(f"{API}/tasks/{tid}", json={"status": "in_progress"},
                                 headers=human_headers())
    assert patched.status_code == 200, patched.text
    return _agent_headers(app, "auditor", "auditor-finding-token")


async def test_issue_board_end_to_end(client, app):
    reviewer = await _seed(client, app)
    schema = await client.get(f"{API}/tasks/T-IB/findings/schema", headers=reviewer)
    assert schema.status_code == 200, schema.text
    assert schema.json()["fields"]

    # 非 reviewer 与 human 都不能代填
    dev = _agent_headers(app, "dev", "dev-finding-token")
    assert (await client.post(f"{API}/tasks/T-IB/findings", json=finding_payload(),
                              headers=dev)).status_code == 403
    assert (await client.post(f"{API}/tasks/T-IB/findings", json=finding_payload(),
                              headers=human_headers())).status_code == 403

    draft = await client.post(f"{API}/tasks/T-IB/findings",
                              json={"title": "待补全的命令注入"}, headers=reviewer)
    assert draft.status_code == 201, draft.text
    fid = draft.json()["id"]
    missing = await client.post(f"{API}/tasks/T-IB/findings/{fid}/submit", headers=reviewer)
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["missing_fields"]

    updated = await client.patch(f"{API}/tasks/T-IB/findings/{fid}",
                                 json=finding_payload(), headers=reviewer)
    assert updated.status_code == 200, updated.text
    submitted = await client.post(f"{API}/tasks/T-IB/findings/{fid}/submit",
                                  headers=reviewer)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"

    # 未完成问题板 / 未逐条确认前，任务最终 review 必须被拒绝
    patched = await client.patch(f"{API}/tasks/T-IB", json={"status": "in_review"},
                                 headers=human_headers())
    assert patched.status_code == 200, patched.text
    blocked = await client.post(f"{API}/tasks/T-IB/review", json={"approve": True},
                                headers=human_headers())
    assert blocked.status_code == 409, blocked.text

    # human 逐条确认
    confirmed = await client.post(f"{API}/tasks/T-IB/findings/{fid}/confirm",
                                  json={"note": "已复现，确认真实"}, headers=human_headers())
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    # reviewer 标记问题板完成；无 workflow 时任务应进入 in_review
    completed = await client.post(f"{API}/tasks/T-IB/findings/complete",
                                  json={"summary": "1 个问题已提交并确认"},
                                  headers=reviewer)
    assert completed.status_code == 200, completed.text
    task = (await client.get(f"{API}/tasks/T-IB", headers=human_headers())).json()
    assert task["status"] == "in_review"

    approved = await client.post(f"{API}/tasks/T-IB/review", json={"approve": True},
                                 headers=human_headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "done"

    summary = (await client.get(f"{API}/tasks/T-IB/findings/summary",
                                headers=human_headers())).json()
    assert summary["board"]["status"] == "completed"
    assert summary["counts"]["confirmed"] == 1


async def test_poc_exemption_and_false_positive(client, app):
    reviewer = await _seed(client, app, "T-IB-POC")
    bad = await client.post(f"{API}/tasks/T-IB-POC/findings",
                            json=finding_payload(poc=""), headers=reviewer)
    assert bad.status_code == 201, bad.text
    fid = bad.json()["id"]
    rejected = await client.post(f"{API}/tasks/T-IB-POC/findings/{fid}/submit",
                                 headers=reviewer)
    assert rejected.status_code == 422, rejected.text
    assert "poc" in rejected.text

    await client.patch(f"{API}/tasks/T-IB-POC/findings/{fid}",
                       json=finding_payload(), headers=reviewer)
    await client.post(f"{API}/tasks/T-IB-POC/findings/{fid}/submit", headers=reviewer)
    fp = await client.post(f"{API}/tasks/T-IB-POC/findings/{fid}/false-positive",
                           json={"note": "误报：输入已白名单"}, headers=human_headers())
    assert fp.status_code == 200, fp.text
    assert fp.json()["status"] == "false_positive"
    assert (await client.post(f"{API}/tasks/T-IB-POC/findings/complete",
                              json={"summary": "唯一问题为误报"}, headers=reviewer)
            ).status_code == 200
    approved = await client.post(f"{API}/tasks/T-IB-POC/review",
                                 json={"approve": True}, headers=human_headers())
    assert approved.status_code == 200, approved.text


async def test_empty_board_requires_explicit_complete(client, app):
    """空问题板也必须显式 complete + summary，之后 human 才能通过任务。"""
    reviewer = await _seed(client, app, "T-IB-EMPTY")
    patched = await client.patch(f"{API}/tasks/T-IB-EMPTY",
                                 json={"status": "in_review"}, headers=human_headers())
    assert patched.status_code == 200, patched.text
    approved = await client.post(f"{API}/tasks/T-IB-EMPTY/review",
                                 json={"approve": True}, headers=human_headers())
    assert approved.status_code == 409, approved.text

    missing_summary = await client.post(f"{API}/tasks/T-IB-EMPTY/findings/complete",
                                        json={"summary": "  "}, headers=reviewer)
    assert missing_summary.status_code == 422, missing_summary.text
    completed = await client.post(f"{API}/tasks/T-IB-EMPTY/findings/complete",
                                  json={"summary": "未发现安全问题；已覆盖全部范围"},
                                  headers=reviewer)
    assert completed.status_code == 200, completed.text
    task = (await client.get(f"{API}/tasks/T-IB-EMPTY", headers=human_headers())).json()
    assert task["status"] == "in_review"
    approved = await client.post(f"{API}/tasks/T-IB-EMPTY/review",
                                 json={"approve": True}, headers=human_headers())
    assert approved.status_code == 200, approved.text


async def test_confidence_zero_is_a_valid_value(client, app):
    """置信度 0 是合法值，不能被完整性校验当作空字段。"""
    reviewer = await _seed(client, app, "T-IB-ZERO")
    created = await client.post(f"{API}/tasks/T-IB-ZERO/findings",
                                json=finding_payload(confidence=0), headers=reviewer)
    assert created.status_code == 201, created.text
    submitted = await client.post(f"{API}/tasks/T-IB-ZERO/findings/{created.json()['id']}/submit",
                                  headers=reviewer)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["confidence"] == 0


async def test_request_changes_reopens_board(client, app):
    reviewer = await _seed(client, app, "T-IB-REWORK")
    created = await client.post(f"{API}/tasks/T-IB-REWORK/findings",
                                json=finding_payload(), headers=reviewer)
    fid = created.json()["id"]
    await client.post(f"{API}/tasks/T-IB-REWORK/findings/{fid}/submit", headers=reviewer)
    await client.post(f"{API}/tasks/T-IB-REWORK/findings/complete",
                      json={"summary": "待人工复核"}, headers=reviewer)

    changed = await client.post(
        f"{API}/tasks/T-IB-REWORK/findings/{fid}/request-changes",
        json={"note": "请让开发确认 source 是否可达"}, headers=human_headers())
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == "changes_requested"
    task = (await client.get(f"{API}/tasks/T-IB-REWORK", headers=human_headers())).json()
    assert task["status"] == "in_progress"
    summary = (await client.get(f"{API}/tasks/T-IB-REWORK/findings/summary",
                                headers=human_headers())).json()
    assert summary["board"]["status"] == "in_progress"

    # reviewer 补证后重交，再完成 board → 可最终通过
    await client.patch(f"{API}/tasks/T-IB-REWORK/findings/{fid}",
                       json={"poc_status": "provided", "poc": "curl ... 复现"},
                       headers=reviewer)
    await client.post(f"{API}/tasks/T-IB-REWORK/findings/{fid}/submit", headers=reviewer)
    await client.post(f"{API}/tasks/T-IB-REWORK/findings/{fid}/confirm",
                      json={}, headers=human_headers())
    await client.post(f"{API}/tasks/T-IB-REWORK/findings/complete",
                      json={"summary": "已补证"}, headers=reviewer)
    approved = await client.post(f"{API}/tasks/T-IB-REWORK/review",
                                 json={"approve": True}, headers=human_headers())
    assert approved.status_code == 200, approved.text


async def test_inbox_lists_pending_finding_board(client, app):
    reviewer = await _seed(client, app, "T-IB-INBOX")
    created = await client.post(f"{API}/tasks/T-IB-INBOX/findings",
                                json=finding_payload(), headers=reviewer)
    fid = created.json()["id"]
    await client.post(f"{API}/tasks/T-IB-INBOX/findings/{fid}/submit", headers=reviewer)
    await client.post(f"{API}/tasks/T-IB-INBOX/findings/complete",
                      json={"summary": "待确认"}, headers=reviewer)
    inbox = (await client.get(f"{API}/inbox", headers=human_headers())).json()
    item = next((i for i in inbox["items"] if i["id"] == "finding:T-IB-INBOX"), None)
    assert item is not None
    assert item["kind"] == "finding"
    assert item["meta"]["finding_pending"] == 1
    # 有待确认 finding 时不再生成一个点进去也会 409 的普通评审项
    assert not any(i["id"] == "review:T-IB-INBOX" for i in inbox["items"])
    await client.post(f"{API}/tasks/T-IB-INBOX/findings/{fid}/confirm",
                      json={}, headers=human_headers())
    inbox = (await client.get(f"{API}/inbox", headers=human_headers())).json()
    assert not any(i["id"] == "finding:T-IB-INBOX" for i in inbox["items"])
    # 全部确认后交回普通任务评审入口
    assert any(i["id"] == "review:T-IB-INBOX" for i in inbox["items"])


async def test_workflow_must_cover_reviewer(client, app):
    reviewer = await _seed(client, app, "T-IB-WF")
    missing = await client.post(f"{API}/board/drafts", headers=reviewer, json={
        "goal": "覆盖审核者", "source_task": "T-IB-WF", "kind": "workflow",
        "tree": {"steps": [{"id": "S1", "title": "实现", "role": "dev"}]},
    })
    assert missing.status_code == 409, missing.text
    assert "reviewer" in missing.text
    ok = await client.post(f"{API}/board/drafts", headers=reviewer, json={
        "goal": "覆盖审核者", "source_task": "T-IB-WF", "kind": "workflow",
        "tree": {"steps": [
            {"id": "S1", "title": "实现", "role": "dev"},
            {"id": "S2", "title": "安全审核", "role": "auditor",
             "depends_on": ["S1"], "gate": True},
        ]},
    })
    assert ok.status_code == 201, ok.text


async def test_board_cleanup_and_assign_guard(client, app):
    reviewer = await _seed(client, app, "T-IB-CLEAN")
    created = await client.post(f"{API}/tasks/T-IB-CLEAN/findings",
                                json=finding_payload(), headers=reviewer)
    assert created.status_code == 201, created.text

    # 不能通过 assign 全量替换把 reviewer 从任务参与角色中移除
    dropped = await client.post(f"{API}/tasks/T-IB-CLEAN/assign",
                                json={"roles": ["dev"]}, headers=human_headers())
    assert dropped.status_code == 409, dropped.text

    # 有关闭问题板时必须 force；force 后 flag 关闭
    blocked = await client.patch(f"{API}/tasks/T-IB-CLEAN",
                                 json={"issue_board_enabled": False},
                                 headers=human_headers())
    assert blocked.status_code == 409, blocked.text
    hidden = await client.patch(f"{API}/tasks/T-IB-CLEAN?force=true",
                                json={"issue_board_enabled": False},
                                headers=human_headers())
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["issue_board_enabled"] is False

    # 删除任务级联清理 reviewers/board/findings
    deleted = await client.delete(f"{API}/tasks/T-IB-CLEAN?force=true",
                                  headers=human_headers())
    assert deleted.status_code == 204, deleted.text
    conn = sqlite3.connect(app.state.db_path)
    try:
        for table in ("task_reviewers", "task_finding_boards", "task_findings"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id='T-IB-CLEAN'").fetchone()[0] == 0
    finally:
        conn.close()


async def test_flag_and_reviewer_permissions(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("auditor"), headers=auth_headers())

    # Agent 不能创建启用问题板的主任务
    dev = _agent_headers(app, "dev", "dev-create-board-token")
    r = await client.post(f"{API}/tasks", json={
        **task_payload(id_="T-IB-AGENT"), "issue_board_enabled": True,
    }, headers=dev)
    assert r.status_code == 403, r.text

    created = await client.post(f"{API}/tasks", json={
        **task_payload(id_="T-IB-PERM"), "assignees": ["dev"],
        "issue_board_enabled": True,
    }, headers=human_headers())
    assert created.status_code == 201, created.text
    # 非 planner/onboarding 的 agent 不能设置 reviewer
    denied = await client.post(f"{API}/tasks/T-IB-PERM/reviewers",
                               json={"roles": ["auditor"]}, headers=auth_headers("dev"))
    assert denied.status_code == 403, denied.text
    added = await client.post(f"{API}/tasks/T-IB-PERM/reviewers",
                              json={"roles": ["auditor"]}, headers=human_headers())
    assert added.status_code == 200, added.text
    # 不能删除最后一个 reviewer
    removed = await client.delete(f"{API}/tasks/T-IB-PERM/reviewers/auditor",
                                  headers=human_headers())
    assert removed.status_code == 409, removed.text
