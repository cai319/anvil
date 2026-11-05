"""问题板 → 外部问题跟踪服务器提交 API。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.services import issue_tracker
from tests.api.test_issue_board_api import (
    _agent_headers, _seed, finding_payload,
)
from tests.conftest import API, human_headers

pytestmark = pytest.mark.usefixtures("client")


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


async def _next_id(client, reviewer) -> str:
    r = await client.post(
        f"{API}/tasks/T-IB/findings", json=finding_payload(),
        headers=reviewer)
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    submitted = await client.post(
        f"{API}/tasks/T-IB/findings/{fid}/submit", headers=reviewer)
    assert submitted.status_code == 200, submitted.text
    return fid


async def _configure(client, url="http://127.0.0.1:9/api"):
    r = await client.put(
        f"{API}/tooling/issue-tracker", json={"server_url": url},
        headers=human_headers())
    assert r.status_code == 200, r.text
    return r.json()


async def test_single_and_bulk_export(client, app, monkeypatch):
    reviewer = await _seed(client, app)
    await _configure(client)
    calls: list[dict] = []

    def fake_post(endpoint, payload, timeout=15):
        calls.append(payload)
        return FakeResponse(200, {
            "code": 0,
            "msg": "ok",
            "data": [700 + i for i in range(len(payload["list"]))],
        })

    monkeypatch.setattr(issue_tracker, "_post_request", fake_post)

    submitted_fid = await _next_id(client, reviewer)
    single = await client.post(
        f"{API}/tasks/T-IB/findings/{submitted_fid}/external-submit",
        headers=human_headers())
    assert single.status_code == 200, single.text
    assert str(single.json()["external_id"]) == "700"
    assert calls[-1]["source"].startswith("anvil:T-IB:")

    # 可提交：submitted / confirmed / changes_requested；排除 draft / false_positive
    confirmed_fid = await _next_id(client, reviewer)
    assert (await client.post(
        f"{API}/tasks/T-IB/findings/{confirmed_fid}/confirm",
        json={}, headers=human_headers())).status_code == 200
    changes_fid = await _next_id(client, reviewer)
    assert (await client.post(
        f"{API}/tasks/T-IB/findings/{changes_fid}/request-changes",
        json={"note": "补充证据"}, headers=human_headers())).status_code == 200
    draft = await client.post(
        f"{API}/tasks/T-IB/findings", json={"title": "草稿问题"},
        headers=reviewer)
    assert draft.status_code == 201
    fp_fid = await _next_id(client, reviewer)
    assert (await client.post(
        f"{API}/tasks/T-IB/findings/{fp_fid}/false-positive",
        json={}, headers=human_headers())).status_code == 200

    before = len(calls)
    bulk = await client.post(
        f"{API}/tasks/T-IB/findings/external-submit",
        json={}, headers=human_headers())
    assert bulk.status_code == 200, bulk.text
    body = bulk.json()
    assert len(body["submitted"]) == 3  # submitted + confirmed + changes_requested
    assert body["failed"] == []
    payload = calls[before]
    assert len(payload["list"]) == 3
    titles = [item["risk_description"] for item in payload["list"]]
    assert "草稿问题" not in titles
    assert payload["source"].startswith("anvil:T-IB:")

    # 每次点击都重新调用，不做本地跳过
    repeat = await client.post(
        f"{API}/tasks/T-IB/findings/external-submit",
        json={}, headers=human_headers())
    assert repeat.status_code == 200
    assert len(calls) == before + 2


async def test_export_permissions_and_tooling_test(client, app, monkeypatch):
    reviewer = await _seed(client, app)
    fid = await _next_id(client, reviewer)
    await _configure(client)

    agent = _agent_headers(app, "auditor", "auditor-export-token")
    denied = await client.post(
        f"{API}/tasks/T-IB/findings/{fid}/external-submit", headers=agent)
    assert denied.status_code == 403

    def validation_response(endpoint, payload, timeout=15):
        return FakeResponse(400, {
            "code": 1, "msg": "[draft bulkCreate] list 不能为空",
        })

    monkeypatch.setattr(issue_tracker, "_post_request", validation_response)
    tested = await client.post(
        f"{API}/tooling/issue-tracker/test",
        json={"server_url": "http://127.0.0.1:9/api"},
        headers=human_headers())
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True

    settings = await client.get(
        f"{API}/tooling/issue-tracker", headers=human_headers())
    assert settings.status_code == 200
    assert settings.json()["configured"] is True

    # 任务删除应级联清理提交历史。
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO task_finding_submissions "
            "(id, task_id, finding_id, source, external_id, status, "
            " request_json, response_json, submitted_by, created_at) "
            "VALUES ('S1','T-IB','F1','src','1','submitted','{}','{}','human','now')")
        conn.commit()
    finally:
        conn.close()
    deleted = await client.delete(f"{API}/tasks/T-IB?force=true", headers=human_headers())
    assert deleted.status_code in (200, 204), deleted.text
    conn = sqlite3.connect(app.state.db_path)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM task_finding_submissions WHERE task_id='T-IB'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert left == 0


async def test_export_failure_records_history(client, app, monkeypatch):
    reviewer = await _seed(client, app)
    fid = await _next_id(client, reviewer)
    await _configure(client)

    def fail_post(endpoint, payload, timeout=15):
        return FakeResponse(500, {"msg": "upstream down"})

    monkeypatch.setattr(issue_tracker, "_post_request", fail_post)
    r = await client.post(
        f"{API}/tasks/T-IB/findings/{fid}/external-submit",
        headers=human_headers())
    assert r.status_code == 502
    conn = sqlite3.connect(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT status, error FROM task_finding_submissions "
            "WHERE finding_id=?", (fid,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "failed"
    assert "HTTP 500" in row[1]
