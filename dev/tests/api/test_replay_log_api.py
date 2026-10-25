"""Replay log export API."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, task_payload


@pytest.mark.asyncio
async def test_replay_log_api(client):
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-RPL", title="回放导出"), headers=auth_headers())
    await client.post(f"{API}/tasks/T-RPL/comment", headers=auth_headers("qa-role"),
                      json={"body": "关键结论：日志可导出"})
    res = await client.get(f"{API}/tasks/T-RPL/replay/log", headers=human_headers())
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/plain")
    assert "attachment" in res.headers["content-disposition"]
    assert ".log" in res.headers["content-disposition"]
    assert "# Anvil Task Replay Log" in res.text
    assert "T-RPL" in res.text
    assert "关键结论：日志可导出" in res.text
    assert "message.comment" in res.text


@pytest.mark.asyncio
async def test_replay_log_api_unknown_task(client):
    res = await client.get(f"{API}/tasks/missing/replay/log", headers=human_headers())
    assert res.status_code == 404
