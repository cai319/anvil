"""L3 · anvil CLI 契约测试：命令↔端点映射 + 退出码 0–4（design §7.2/§7.3，SE §4）。

实现：用 stdlib `http.server` 起一个 mock 后端（无额外依赖），记录 CLI 发出的
(method, path)，并可按用例强制返回状态码，据此断言 CLI 的映射与退出码语义。
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from urllib.parse import urlsplit

import pytest

from tests.conftest import API

ANVIL_BIN = os.environ.get("ANVIL_BIN", "anvil")

# 契约映射表（§7.2 命令 → REST）。args 为 CLI 调用参数（不含 `anvil` 前缀）。
CONTRACT = {
    "task list": (["task", "list"], "GET", f"{API}/tasks"),
    "task show": (["task", "show", "T-0001"], "GET", f"{API}/tasks/T-0001"),
    "task create": (["task", "create", "hello"], "POST", f"{API}/tasks"),
    "task claim": (["task", "claim", "T-0001"], "POST", f"{API}/tasks/T-0001/claim"),
    "task unclaim": (["task", "unclaim", "T-0001"], "POST", f"{API}/tasks/T-0001/unclaim"),
    "task heartbeat": (["task", "heartbeat", "T-0001"], "POST", f"{API}/tasks/T-0001/heartbeat"),
    "task submit": (["task", "submit", "T-0001", "done"], "POST", f"{API}/tasks/T-0001/submit"),
    "task comment": (["task", "comment", "T-0001", "x"], "POST", f"{API}/tasks/T-0001/comment"),
    "task block": (["task", "block", "T-0001", "--on", "T-0002"], "POST", f"{API}/tasks/T-0001/deps"),
    "memory append": (["memory", "append", "x"], "POST", f"{API}/me/memory"),
    "memory search": (["memory", "search", "x"], "GET", f"{API}/me/memory/search"),
    "note write": (["note", "write", "p"], "POST", f"{API}/me/notes"),
    "knowledge search": (["knowledge", "search", "jwt"], "GET",
                         f"{API}/me/knowledge/search"),
    "knowledge show": (["knowledge", "show", "--id", "n1"], "GET",
                       f"{API}/me/knowledge/n1"),
    "knowledge index": (["knowledge", "index"], "GET", f"{API}/me/knowledge/index"),
    "knowledge reindex": (["knowledge", "reindex"], "POST",
                          f"{API}/me/knowledge/reindex"),
    "lesson propose": (["lesson", "propose", "x"], "POST", f"{API}/lessons/pending"),
    "self show": (["self", "show"], "GET", f"{API}/me"),
    "self reset-context": (["self", "reset-context", "--task", "T-0001"],
                           "POST", f"{API}/me/session/reset"),
    "reminder list": (["reminder", "list"], "GET", f"{API}/reminders"),
    "reminder add": (["reminder", "add", "--cron", "0"], "POST", f"{API}/reminders"),
    "ws status": (["ws", "status"], "GET", f"{API}/workspaces/0/tree"),
}

EXIT = {"ok": 0, "generic": 1, "usage": 2, "conflict": 3, "forbidden": 4}


class _Handler(http.server.BaseHTTPRequestHandler):
    """记录请求；按 forced_status 返回。"""

    forced_status = 200
    requests: list[tuple[str, str]] = []

    def _handle(self) -> None:
        n = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(n) if n else None
        type(self).requests.append((self.command, urlsplit(self.path).path))
        body = json.dumps({"ok": True}).encode()
        self.send_response(type(self).forced_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def mock_backend():
    _Handler.requests = []
    _Handler.forced_status = 200
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    t.join()


def _run(args: list[str], endpoint: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "ANVIL_ENDPOINT": endpoint,
        "ANVIL_TOKEN": "test-token",
        "ANVIL_AGENT_SLUG": "qa-role",
        "ANVIL_TASK_ID": "T-0001",
    }
    return subprocess.run(
        [ANVIL_BIN, *args], env=env, capture_output=True, text=True, timeout=30
    )


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_command_maps_to_endpoint(name, mock_backend):
    """每个命令命中正确 (method, path) 且成功退 0。"""
    args, method, path = CONTRACT[name]
    r = _run(args, mock_backend)
    assert r.returncode == EXIT["ok"], f"{name}: stderr={r.stderr!r}"
    assert (method, path) in _Handler.requests, f"{name}: 实际请求 {_Handler.requests}"


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_conflict_409(mock_backend):
    """409 → 退出码 3（认领失败/依赖未满足，§7.3 关键区分）。"""
    _Handler.forced_status = 409
    r = _run(["task", "claim", "T-0001"], mock_backend)
    assert r.returncode == EXIT["conflict"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_forbidden_403(mock_backend):
    """403 → 退出码 4（无权限/scope 越界）。"""
    _Handler.forced_status = 403
    r = _run(["task", "submit", "T-0001", "x"], mock_backend)
    assert r.returncode == EXIT["forbidden"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_bad_request_422(mock_backend):
    """422 → 退出码 2（参数/预算超限）。"""
    _Handler.forced_status = 422
    r = _run(["task", "create", "x"], mock_backend)
    assert r.returncode == EXIT["usage"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_usage_missing_arg():
    """缺任务 id → 退出码 2（Typer 参数错误，不走网络）。"""
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1", "ANVIL_TASK_ID": ""}
    r = subprocess.run(
        [ANVIL_BIN, "task", "claim"], env=env, capture_output=True, text=True, timeout=30
    )
    assert r.returncode == EXIT["usage"]
