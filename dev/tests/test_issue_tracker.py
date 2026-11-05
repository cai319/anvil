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


def _long_item() -> dict:
    long = "很长的字段内容" * 250
    return {
        "risk_description": long,
        "risk_type": "注入",
        "severity": "严重",
        "module": long,
        "handler_name": "run_command",
        "file_line_no": "backend/anvil/api/tasks.py:120-126",
        "pre_condition": long,
        "attack_path": long,
        "trigger_point": long,
        "fix_priority": "P0",
        "fix_suggestion": long,
        "description_md": "# 完整详情\n" + long,
    }


def test_compact_items_uses_llm_and_keeps_description(monkeypatch):
    item = _long_item()
    original_description = item["description_md"]
    prompts: list[str] = []

    monkeypatch.setattr(issue_tracker.utility, "resolve_runtime_id",
                        lambda conn, preferred=None: "fake-runtime")

    def fake_generate(conn, prompt, **kwargs):
        prompts.append(prompt)
        return '{"items":[{"index":0,"fields":{' + ",".join(
            f'"{key}":"精简后的{key}"' for key in (
                "risk_description", "module", "pre_condition", "attack_path",
                "trigger_point", "fix_suggestion")
        ) + '}}]}'

    monkeypatch.setattr(issue_tracker.utility, "generate", fake_generate)
    out = issue_tracker._compact_items(None, [item], task_id="T-1")

    assert prompts
    assert '"description_md"' in prompts[0]
    assert "很长的字段内容" in prompts[0]
    assert out[0]["description_md"] == original_description
    assert out[0]["risk_description"] == "精简后的risk_description"
    for key, value in out[0].items():
        if key != "description_md":
            assert len(str(value)) <= issue_tracker.FIELD_MAX_CHARS


def test_compact_items_falls_back_to_truncation(monkeypatch):
    item = _long_item()
    original_description = item["description_md"]

    monkeypatch.setattr(issue_tracker.utility, "resolve_runtime_id",
                        lambda conn, preferred=None: "fake-runtime")

    def fail_generate(*args, **kwargs):
        raise issue_tracker.utility.UtilityError("runtime unavailable")

    monkeypatch.setattr(issue_tracker.utility, "generate", fail_generate)
    out = issue_tracker._compact_items(None, [item], task_id="T-1")

    assert out[0]["description_md"] == original_description
    for key, value in out[0].items():
        if key != "description_md":
            assert len(str(value)) <= issue_tracker.FIELD_MAX_CHARS
    assert out[0]["risk_description"].endswith("…")


def test_compact_items_respects_configured_max(monkeypatch):
    item = _long_item()
    monkeypatch.setattr(issue_tracker.utility, "resolve_runtime_id",
                        lambda conn, preferred=None: "fake-runtime")

    def fake_generate(*args, **kwargs):
        return ('{"items":[{"index":0,"fields":{'
                '"risk_description":"' + "长" * 300 + '"}}]}')

    monkeypatch.setattr(issue_tracker.utility, "generate", fake_generate)
    out = issue_tracker._compact_items(
        None, [item], task_id="T-1", max_chars=128)
    assert out[0]["risk_description"] == "长" * 127 + "…"
    for key, value in out[0].items():
        if key != "description_md":
            assert len(str(value)) <= 128


def test_compact_items_skips_llm_for_short_fields(monkeypatch):
    item = {
        "risk_description": "短描述",
        "severity": "高",
        "description_md": "完整详情",
    }

    def fail_generate(*args, **kwargs):
        raise AssertionError("short fields must not call LLM")

    monkeypatch.setattr(issue_tracker.utility, "generate", fail_generate)
    out = issue_tracker._compact_items(None, [item], task_id="T-1")
    assert out[0]["risk_description"] == "短描述"
    assert out[0]["description_md"] == "完整详情"


def test_settings_roundtrip(tmp_path):
    conn = connect(str(tmp_path / "tracker.db"))
    try:
        initial = issue_tracker.get_settings(conn)
        assert initial["configured"] is False
        assert initial["field_max_chars"] == issue_tracker.FIELD_MAX_CHARS
        out = issue_tracker.save_settings(
            conn, "http://1.2.3.4:5000/api/", field_max_chars=1024,
            actor="human")
        assert out["configured"] is True
        assert out["server_url"] == "http://1.2.3.4:5000/api"
        assert out["field_max_chars"] == 1024
        assert issue_tracker.get_settings(conn)["server_url"].endswith("/api")
        issue_tracker.save_settings(conn, "", actor="human")
        assert issue_tracker.get_settings(conn)["configured"] is False
        assert issue_tracker.get_settings(conn)["field_max_chars"] == 1024
        with pytest.raises(ValueError):
            issue_tracker.save_settings(conn, "", field_max_chars=10,
                                        actor="human")
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


def test_post_request_never_uses_proxy(monkeypatch):
    """Tracker 请求固定直连，即使进程环境或系统存在代理。"""
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, endpoint, json):
            captured["endpoint"] = endpoint
            captured["payload"] = json

            class FakeResponse:
                status_code = 200
                text = "{}"
                content = b"{}"
                headers = {}

            return FakeResponse()

    monkeypatch.setattr(issue_tracker.httpx, "Client", FakeClient)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.local:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.local:8080")
    issue_tracker._post_request(
        "http://1.2.3.4/api/draft/bulkCreate", {"source": "x", "list": []})
    assert captured["trust_env"] is False
    assert captured["proxy"] is None
    assert captured["endpoint"].endswith("/draft/bulkCreate")


def test_connection_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(issue_tracker, "_post_request", timeout)
    result = issue_tracker.test_connection(None, "http://1.2.3.4/api")
    assert result["ok"] is False
    assert "超时" in result["message"]
    assert result["elapsed_ms"] >= 0


def test_connection_classifies_connect_and_read_timeout(monkeypatch):
    def connect_timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("connect slow")

    monkeypatch.setattr(issue_tracker, "_post_request", connect_timeout)
    result = issue_tracker.test_connection(None, "http://1.2.3.4/api")
    assert "连接超时" in result["message"]
    assert "DNS" in result["message"]
    assert result["error_type"] == "ConnectTimeout"

    def read_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("read slow")

    monkeypatch.setattr(issue_tracker, "_post_request", read_timeout)
    result = issue_tracker.test_connection(None, "http://1.2.3.4/api")
    assert "读取超时" in result["message"]
    assert "未收到响应" in result["message"]
    assert result["error_type"] == "ReadTimeout"
