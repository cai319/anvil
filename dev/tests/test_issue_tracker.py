"""问题板外部提交：URL、字段映射、Markdown 与连通性测试。"""
from __future__ import annotations

import httpx
import pytest

from anvil.db import connect
from anvil.services import issue_tracker


def test_normalize_url_and_endpoint():
    assert issue_tracker.normalize_base_url("http://1.2.3.4:5000/api/") == \
        "http://1.2.3.4:5000/api"
    assert issue_tracker.endpoint_url("http://1.2.3.4:5000/api") == \
        "http://1.2.3.4:5000/api/draft/bulkCreate"
    assert issue_tracker.normalize_base_url(
        "http://1.2.3.4:5000/api/draft/bulkCreate") == \
        "http://1.2.3.4:5000/api"
    with pytest.raises(ValueError):
        issue_tracker.normalize_base_url("ftp://1.2.3.4/api")
    with pytest.raises(ValueError):
        issue_tracker.normalize_base_url("http://user:pass@1.2.3.4/api")


def test_item_mapping_and_markdown():
    task = {"id": "T-1", "title": "OpenSSL 审计"}
    board = {"status": "completed"}
    finding = {
        "id": "F-1", "author_role": "auditor", "title": "命令注入",
        "risk_type": "injection", "severity": "critical",
        "module": "backend", "function_name": "run", "file_path": "a.py",
        "line_numbers": "10-12", "preconditions": "可控参数",
        "reasoning_chain": "参数进入 shell", "risk_description": "可执行命令",
        "attack_path": "input -> shell", "trigger_point": "创建任务",
        "poc_status": "not_verified", "poc": "未构造", "fix_priority": "P0",
        "fix_suggestion": "禁止 shell", "confidence": 90, "status": "confirmed",
        "version": 2, "decided_by": "human", "decided_at": "2026-11-05",
        "decision_note": "确认",
    }
    item = issue_tracker.build_item(
        task, board, finding, source="anvil:T-1:20261105T000000Z",
        submitted_at="2026-11-05T00:00:00+00:00")
    assert item["risk_description"] == "可执行命令"
    assert item["risk_type"] == "注入"
    assert item["severity"] == "严重"
    assert item["handler_name"] == "run"
    assert item["file_line_no"] == "a.py:10-12"
    assert item["pre_condition"] == "可控参数"
    assert item["fix_priority"] == "P0"
    assert "OpenSSL 审计" in item["description_md"]
    assert "参数进入 shell" in item["description_md"]
    assert "禁止 shell" in item["description_md"]


def test_settings_roundtrip(tmp_path):
    conn = connect(str(tmp_path / "tracker.db"))
    try:
        assert issue_tracker.get_settings(conn)["configured"] is False
        out = issue_tracker.save_settings(
            conn, "http://1.2.3.4:5000/api/", actor="human")
        assert out["configured"] is True
        assert out["server_url"] == "http://1.2.3.4:5000/api"
        assert issue_tracker.get_settings(conn)["server_url"].endswith("/api")
        issue_tracker.save_settings(conn, "", actor="human")
        assert issue_tracker.get_settings(conn)["configured"] is False
    finally:
        conn.close()


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_connection_accepts_expected_validation_error(monkeypatch):
    monkeypatch.setattr(
        issue_tracker, "_post_request",
        lambda *a, **k: _FakeResponse(
            400, {"code": 1, "msg": "[draft bulkCreate] list 不能为空"}))
    result = issue_tracker.test_connection(None, "http://1.2.3.4/api")
    assert result["ok"] is True
    assert result["status"] == 400


def test_connection_rejects_missing_endpoint(monkeypatch):
    monkeypatch.setattr(
        issue_tracker, "_post_request",
        lambda *a, **k: _FakeResponse(404, {"msg": "not found"}))
    result = issue_tracker.test_connection(None, "http://1.2.3.4/api")
    assert result["ok"] is False


def test_connection_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(issue_tracker, "_post_request", timeout)
    result = issue_tracker.test_connection(None, "http://1.2.3.4/api")
    assert result["ok"] is False
    assert "超时" in result["message"]
