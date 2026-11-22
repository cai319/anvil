"""共享经验（lesson）知识卡：结构化字段、单条详情与兼容旧数据。

知识设计（docs/v1.30-channel-knowledge.md）要求 notes/lessons 采用同一张紧凑知识卡：
title / tldr / 正文 / fields_json。人工审批时需要看到完整卡片，而不是被截断的一行。
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import (API, auth_headers, human_headers, role_payload,
                            runtime_payload, task_payload)

pytestmark = pytest.mark.usefixtures("client")


COMPLETE_LESSON_FIELDS = {
    "use_when": "任务涉及 X509 且使用发行版 OpenSSL 时。",
    "expected_effect": "避免把环境差异误判为漏洞，减少一轮返工。",
    "evidence": "T-1 / run r-1 的两组复现记录。",
    "observation": "发行版 OpenSSL 与自编译版本在 X509 校验上行为不同。",
    "lesson": "先核对 OpenSSL 版本与编译选项，再判断是否为漏洞。",
    "applicability": "使用发行版 OpenSSL 的 X509 审计任务。",
    "counterexamples": "自编译且关闭 ASan 的环境不适用。",
}


async def test_lesson_structured_card_and_detail(client):
    payload = {
        "title": "发行版 OpenSSL 的 X509 行为与自编译版本不同",
        "tldr": "先核对 OpenSSL 版本与编译选项，再判断是否为漏洞。",
        "content": (
            "## 观察\n发行版 OpenSSL 与自编译版本在 X509 校验上行为不同。\n\n"
            "## 证据\n任务 T-1 的 ASan 复现与发行版复现结论相反。"
        ),
        "fields": {
            **COMPLETE_LESSON_FIELDS,
            "preconditions": "确认 OpenSSL 版本与编译选项。",
            "trigger_point": "读取证书链校验结果时。",
        },
        "tags": ["openssl", "x509"],
        "repo": "shared",
        "source_task": "T-1",
    }
    created = await client.post(
        f"{API}/lessons/pending", json=payload, headers=auth_headers("pm"))
    assert created.status_code == 201, created.text
    lesson = created.json()
    lid = lesson["id"]
    assert lesson["title"] == payload["title"]
    assert lesson["tldr"] == payload["tldr"]
    assert lesson["fields"]["use_when"] == payload["fields"]["use_when"]
    assert lesson["fields"]["trigger_point"] == payload["fields"]["trigger_point"]
    assert "fields_json" not in lesson
    assert lesson["source_role"] == "pm"
    assert lesson["source_task"] == "T-1"

    detail = await client.get(f"{API}/lessons/{lid}", headers=auth_headers("pm"))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["content"] == payload["content"]
    assert body["fields"] == payload["fields"]
    assert body["quality"]["resolved_fields"]["use_when"] == "use_when"
    assert "use_when" not in body["quality"]["missing"]

    listed = await client.get(f"{API}/lessons/pending", headers=auth_headers("pm"))
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["id"] == lid)
    assert row["title"] == payload["title"]
    assert row["fields"] == payload["fields"]

    updated = await client.put(
        f"{API}/lessons/{lid}",
        json={"tldr": "更新后的摘要", "fields": {"use_when": "仅发行版环境"}},
        headers=human_headers())
    assert updated.status_code == 200, updated.text
    assert updated.json()["tldr"] == "更新后的摘要"
    assert updated.json()["fields"] == {"use_when": "仅发行版环境"}

    missing = await client.get(f"{API}/lessons/not-found", headers=auth_headers("pm"))
    assert missing.status_code == 404


async def test_lesson_submission_requires_complete_knowledge(client):
    """知识提交前的必须条款：缺任一完整知识字段即格式不合法。"""
    created = await client.post(
        f"{API}/lessons/pending",
        json={"content": "只有正文，没有结构化知识字段", "tags": ["review"]},
        headers=auth_headers())
    assert created.status_code == 422, created.text
    detail = str(created.json()["detail"])
    assert "知识格式不合法" in detail
    assert "use_when" in detail and "observation" in detail
    assert "expected_effect" in detail and "evidence" in detail


async def test_lesson_exclusions_alias_maps_to_counterexamples(client):
    """通用“不适用/反例”exclusions 与 insight 的 counterexamples 同义，只保留一个。"""
    fields = {key: value for key, value in COMPLETE_LESSON_FIELDS.items()
              if key != "counterexamples"}
    fields["exclusions"] = "自编译环境不适用"
    created = await client.post(
        f"{API}/lessons/pending",
        json={"title": "别名测试", "tldr": "摘要", "content": "正文",
              "fields": fields},
        headers=auth_headers())
    assert created.status_code == 201, created.text
    lesson = created.json()
    assert lesson["fields"]["counterexamples"] == "自编译环境不适用"
    assert "exclusions" not in lesson["fields"]
    assert lesson["ready"] is True


async def test_lesson_title_must_be_concise(client):
    """标题是概括，不是正文；超过 80 字属于格式不合法。"""
    created = await client.post(
        f"{API}/lessons/pending",
        json={"content": "正文", "title": "长" * 81,
              "tldr": "摘要", "fields": COMPLETE_LESSON_FIELDS},
        headers=auth_headers())
    assert created.status_code == 422, created.text
    assert "标题超过 80 字" in str(created.json()["detail"])


async def test_legacy_lesson_detail_derives_title_tldr(client, app):
    """旧库中只有 content 的 lesson 仍可查看；title/tldr 从正文推导。"""
    from anvil.db import open_conn
    from anvil.util import now_iso

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO lessons (id, content, tags_json, repo, status, created_at) "
            "VALUES ('L-LEGACY', ?, '[]', 'shared', 'pending', ?)",
            ("评审前先复算证据哈希\n第二行补充说明", now_iso()))
        conn.commit()
    finally:
        conn.close()

    detail = await client.get(f"{API}/lessons/L-LEGACY", headers=human_headers())
    assert detail.status_code == 200, detail.text
    lesson = detail.json()
    assert lesson["title"] == "评审前先复算证据哈希"
    assert lesson["tldr"] == "评审前先复算证据哈希 第二行补充说明"
    assert lesson["fields"] == {}
    assert lesson["ready"] is False
    assert lesson["submission_errors"]


async def test_legacy_lesson_fields_can_be_completed_then_approved(client, app):
    """旧待审经验可在详情中补全字段，再通过必须条款批准。"""
    from anvil.db import open_conn
    from anvil.util import now_iso

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO lessons (id, content, tags_json, repo, status, created_at) "
            "VALUES ('L-FIX', '旧正文', '[]', 'shared', 'pending', ?)",
            (now_iso(),))
        conn.commit()
    finally:
        conn.close()

    updated = await client.put(
        f"{API}/lessons/L-FIX",
        json={"title": "补全后的标题", "tldr": "补全后的摘要",
              "fields": COMPLETE_LESSON_FIELDS},
        headers=human_headers())
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "补全后的标题"

    approved = await client.post(
        f"{API}/lessons/L-FIX/approve", headers=human_headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


async def _seed_role(client):
    await client.post(f"{API}/runtimes", json=runtime_payload())
    await client.post(f"{API}/roles", json=role_payload())


def _agent_headers(app, role: str, token: str) -> dict:
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, role, None, None, "2026-01-01T00:00:00+00:00",
             "2999-01-01T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()
    return {"X-Anvil-Session": token}


def _insert_legacy_lesson(app, lid: str, *, source_role: str | None = None,
                          source_task: str | None = None) -> None:
    from anvil.db import open_conn
    from anvil.util import now_iso

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO lessons (id, content, tags_json, repo, status, created_at, "
            "source_role, source_task) VALUES (?,?,?,?,?,?,?,?)",
            (lid, "旧正文", "[]", "shared", "pending", now_iso(),
             source_role, source_task))
        conn.commit()
    finally:
        conn.close()


async def test_harvesting_role_can_fill_lesson_fields(client, app):
    """补全字段由收割角色（source_role）用自身上下文完成；必须一次完整。"""
    await _seed_role(client)
    _insert_legacy_lesson(app, "L-FILL", source_role="qa-role")

    denied = await client.post(
        f"{API}/lessons/L-FILL/fill",
        json={"fields": COMPLETE_LESSON_FIELDS},
        headers=_agent_headers(app, "other-role", "other-fill-token"))
    assert denied.status_code == 403

    owner_headers = _agent_headers(app, "qa-role", "owner-fill-token")
    partial = await client.post(
        f"{API}/lessons/L-FILL/fill",
        json={"fields": {"use_when": "同类任务"}},
        headers=owner_headers)
    assert partial.status_code == 422, partial.text
    assert "知识格式不合法" in str(partial.json()["detail"])

    filled = await client.post(
        f"{API}/lessons/L-FILL/fill",
        json={"title": "收割角色补全的标题", "tldr": "补全摘要",
              "fields": COMPLETE_LESSON_FIELDS},
        headers=owner_headers)
    assert filled.status_code == 200, filled.text
    assert filled.json()["title"] == "收割角色补全的标题"
    assert filled.json()["fields"] == COMPLETE_LESSON_FIELDS
    assert filled.json()["completion"]["state"] == "completed"
    assert filled.json()["ready"] is True

    approved = await client.post(
        f"{API}/lessons/L-FILL/approve", headers=human_headers())
    assert approved.status_code == 200, approved.text


async def test_ready_approved_lesson_gets_completion_marker(client):
    """历史已达标经验（无补全任务记录）也要显示“补全完成”。

    待审阶段仍显示“可入库”；批准后即为库内历史知识，应带完成标记。
    """
    created = await client.post(
        f"{API}/lessons/pending",
        json={"title": "历史达标经验", "tldr": "摘要", "content": "正文",
              "fields": COMPLETE_LESSON_FIELDS},
        headers=auth_headers())
    assert created.status_code == 201, created.text
    lesson = created.json()
    assert lesson["ready"] is True
    assert lesson["completion"] is None  # 待审：前端显示“可入库”

    approved = await client.post(
        f"{API}/lessons/{lesson['id']}/approve", headers=human_headers())
    assert approved.status_code == 200, approved.text
    completion = approved.json()["completion"]
    assert completion is not None
    assert completion["state"] == "completed"
    assert completion["active"] is False
    assert completion["inferred"] is True

    listed = await client.get(
        f"{API}/lessons?status=approved", headers=auth_headers())
    row = next(item for item in listed.json() if item["id"] == lesson["id"])
    assert row["completion"]["state"] == "completed"
    assert row["completion"]["inferred"] is True

    detail = await client.get(
        f"{API}/lessons/{lesson['id']}", headers=auth_headers())
    assert detail.json()["completion"]["state"] == "completed"


async def test_fill_must_also_shorten_title_and_fields(client, app):
    """补全时角色必须同时满足标题/正文/字段长度限制。"""
    await _seed_role(client)
    _insert_legacy_lesson(app, "L-LONG", source_role="qa-role")
    owner = _agent_headers(app, "qa-role", "owner-long-token")

    too_long_field = {
        **COMPLETE_LESSON_FIELDS,
        "evidence": "长" * 2001,
    }
    bad_field = await client.post(
        f"{API}/lessons/L-LONG/fill",
        json={"title": "合适标题", "tldr": "摘要", "fields": too_long_field},
        headers=owner)
    assert bad_field.status_code == 422, bad_field.text
    assert "超过 2000 字" in str(bad_field.json()["detail"])

    bad_title = await client.post(
        f"{API}/lessons/L-LONG/fill",
        json={"title": "长" * 81, "tldr": "摘要",
              "fields": COMPLETE_LESSON_FIELDS},
        headers=owner)
    assert bad_title.status_code == 422, bad_title.text
    assert "标题超过 80 字" in str(bad_title.json()["detail"])

    fixed = await client.post(
        f"{API}/lessons/L-LONG/fill",
        json={"title": "概括标题", "tldr": "精简摘要",
              "fields": COMPLETE_LESSON_FIELDS},
        headers=owner)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["title"] == "概括标题"


async def test_request_completion_starts_harvesting_role_run(client, app):
    """无来源任务时，补全请求给收割角色创建带知识上下文的 run。"""
    from anvil.db import open_conn

    await _seed_role(client)
    _insert_legacy_lesson(app, "L-REQUEST", source_role="qa-role")
    requested = await client.post(
        f"{API}/lessons/L-REQUEST/request-completion", headers=human_headers())
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert body["status"] == "requested" and body["role"] == "qa-role"
    assert body["run_id"] and body["queued"] is False
    assert body["completion"] is not None

    conn = open_conn(app.state.db_path)
    try:
        run = conn.execute(
            "SELECT role_slug, prompt FROM runs WHERE id=?", (body["run_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert run is not None and run["role_slug"] == "qa-role"
    assert "L-REQUEST" in run["prompt"]
    assert "anvil lesson fill" in run["prompt"]


async def test_request_completion_queues_when_role_busy(client, app, monkeypatch):
    """角色正在工作：补全 run 保持 created，交给角色队列/调度器排队投递。"""
    from anvil.db import open_conn
    from anvil.services import daemon as daemon_service

    await _seed_role(client)
    _insert_legacy_lesson(app, "L-BUSY", source_role="qa-role")

    class _BusyManager:
        enabled = True

        def status(self, role):
            return {"busy": True, "queue_len": 2}

    monkeypatch.setattr(daemon_service, "get_manager", lambda: _BusyManager())
    requested = await client.post(
        f"{API}/lessons/L-BUSY/request-completion", headers=human_headers())
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert body["status"] == "requested" and body["queued"] is True
    assert body["completion"]["state"] == "queued"

    conn = open_conn(app.state.db_path)
    try:
        run = conn.execute(
            "SELECT status FROM runs WHERE id=?", (body["run_id"],)).fetchone()
    finally:
        conn.close()
    assert run is not None and run["status"] == "created"


async def test_request_completion_uses_source_task_context(client, app):
    """有来源任务时，用 task mention + wake 让收割角色回到该任务上下文补全。"""
    from anvil.db import open_conn

    await _seed_role(client)
    task = await client.post(
        f"{API}/tasks",
        json=task_payload(id_="T-LESSON", assignees=["qa-role"]),
        headers=human_headers())
    assert task.status_code == 201, task.text
    _insert_legacy_lesson(
        app, "L-TASK", source_role="qa-role", source_task="T-LESSON")

    requested = await client.post(
        f"{API}/lessons/L-TASK/request-completion", headers=human_headers())
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert body["status"] == "requested" and body["task_id"] == "T-LESSON"
    assert body["queued"] is False  # 角色空闲；wake 请求仍由调度器异步投递

    conn = open_conn(app.state.db_path)
    try:
        wake = conn.execute(
            "SELECT note FROM wake_requests WHERE task_id='T-LESSON' "
            "AND role_slug='qa-role' AND reason LIKE 'lesson.complete.%'"
        ).fetchone()
        mention = conn.execute(
            "SELECT payload_json FROM activities WHERE task_id='T-LESSON' "
            "AND kind='mention' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert wake is not None and "L-TASK" in wake["note"]
    assert mention is not None and "L-TASK" in mention["payload_json"]


async def test_approved_legacy_lesson_can_be_completed_by_human(client, app):
    """已批准的历史经验也允许 human 补全字段，保留 approved 状态。"""
    from anvil.db import open_conn
    from anvil.util import now_iso

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO lessons (id, content, tags_json, repo, status, created_at) "
            "VALUES ('L-OLD-APPROVED', '旧经验正文', '[]', 'shared', 'approved', ?)",
            (now_iso(),))
        conn.commit()
    finally:
        conn.close()

    updated = await client.put(
        f"{API}/lessons/L-OLD-APPROVED",
        json={"title": "补全后的旧经验", "tldr": "补全摘要",
              "fields": COMPLETE_LESSON_FIELDS},
        headers=human_headers())
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["status"] == "approved"
    assert body["ready"] is True
    assert body["submission_errors"] == []


async def test_legacy_lesson_cannot_be_approved_without_complete_fields(client, app):
    """入库（批准）同样执行必须条款；旧待审数据需先补全。"""
    from anvil.db import open_conn
    from anvil.util import now_iso

    conn = open_conn(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO lessons (id, content, tags_json, repo, status, created_at) "
            "VALUES ('L-INCOMPLETE', '只有正文', '[]', 'shared', 'pending', ?)",
            (now_iso(),))
        conn.commit()
    finally:
        conn.close()

    approved = await client.post(
        f"{API}/lessons/L-INCOMPLETE/approve", headers=human_headers())
    assert approved.status_code == 422, approved.text
    assert "知识格式不合法" in str(approved.json()["detail"])
