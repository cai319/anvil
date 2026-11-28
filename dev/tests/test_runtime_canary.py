"""P0-A：运行时环境金丝雀、五元键缓存、派发前拒绝。

真实 CLI 一律用 fake adapter/backend；只有显式 opt-in 才跑真模型调用。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import canary
from anvil.services import runtime as runtime_service
from anvil.services import sandbox as sandbox_service


class FakeProc:
    returncode = 0

    def __init__(self, raw: bytes = b'{"type":"result"}\n'):
        self.raw = raw

    def communicate(self, timeout=None):
        return self.raw, None

    def poll(self):
        return self.returncode


class FakeBackend:
    name = "fake-sbx"

    def __init__(self, raw: bytes = b'{"type":"result"}\n'):
        self.requests = []
        self.raw = raw

    def spawn_stdio(self, req):
        self.requests.append(req)
        return SimpleNamespace(popen=FakeProc(self.raw))

    def kill(self, proc, *, graceful=True):
        return None


class FakeAdapter:
    runtime_id = "claude-code"

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.version = "1.0.0"
        self.verifications = 0

    def cli_version(self, executable, definition):
        return self.version

    def command(self, executable, *, nonce):
        return ["fake-runtime", nonce]

    def verify(self, probe_dir, nonce, output, returncode):
        self.verifications += 1
        return self.ok, "fake canary ok" if self.ok else "fake canary failed"


@pytest.fixture(autouse=True)
def _reset_sandbox_backend():
    """enforce 模式下 runtime_dispatch_guard 会初始化全局后端；用后清掉，避免跨测试泄漏。"""
    yield
    sandbox_service.reset_for_tests(None)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "enforce")
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    c = connect(str(tmp_path / "canary.db"))
    runtime_service.register(
        c, id="claude-code", display_name="Claude Code",
        executable_path="/bin/echo", status="ready", capabilities={})
    yield c
    c.close()


def _run(conn, adapter, backend, **kw):
    return canary.run_canary(
        conn, "claude-code", adapter=adapter, backend=backend, **kw)


def test_canary_uses_five_part_cache_key(conn, monkeypatch):
    adapter = FakeAdapter()
    backend = FakeBackend()
    first = _run(conn, adapter, backend)
    assert first["ok"] is True
    assert first["cached"] is False
    second = _run(conn, adapter, backend)
    assert second["ok"] is True and second["cached"] is True
    assert adapter.verifications == 1          # 同键不重跑

    adapter.version = "2.0.0"
    third = _run(conn, adapter, backend)
    assert third["cli_version"] == "2.0.0"
    assert third["cached"] is False and adapter.verifications == 2

    monkeypatch.setattr(config, "SANDBOX_STANDARD_RSS_MIB", 2048)
    fourth = _run(conn, adapter, backend)
    assert fourth["cached"] is False and adapter.verifications == 3

    other_backend = FakeBackend()
    other_backend.name = "other-sbx"
    fifth = _run(conn, adapter, other_backend)
    assert fifth["cached"] is False and adapter.verifications == 4

    monkeypatch.setattr(canary, "os_tag", lambda: "OtherOS-9")
    sixth = _run(conn, adapter, other_backend)
    assert sixth["cached"] is False and adapter.verifications == 5


def test_canary_failure_is_cached_and_force_reruns(conn):
    adapter = FakeAdapter(ok=False)
    backend = FakeBackend()
    first = _run(conn, adapter, backend)
    assert first["ok"] is False and first["detail"] == "fake canary failed"
    second = _run(conn, adapter, backend)
    assert second["ok"] is False and second["cached"] is True
    assert adapter.verifications == 1
    forced = _run(conn, adapter, backend, force=True)
    assert forced["ok"] is False and forced["cached"] is False
    assert adapter.verifications == 2


def test_canary_env_matches_role_contract_and_strips_agent_shell(conn):
    """从 agent shell 启动时，金丝雀必须复用角色环境契约。"""
    adapter = FakeAdapter()
    backend = FakeBackend()
    inherited = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_NOTES_DIR": "/private/tmp/claude-501/x",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc.sock",
        "CLAUDE_CODE_SESSION_ID": "uuid",
        "CLAUDE_PID": "123",
        "XDG_RUNTIME_DIR": "/run/user/501",
        "ANTHROPIC_AUTH_TOKEN": "model-token",
        "ANVIL_TOKEN": "human-token-must-not-leak",
        "PATH": "/usr/bin:/bin",
    }
    result = _run(conn, adapter, backend, env_base=inherited)
    assert result["ok"] is True
    req = backend.requests[0]
    env = req.env
    for key in ("CLAUDECODE", "CLAUDE_CODE_CHILD_NOTES_DIR",
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_PID"):
        assert key not in env
    assert "ANVIL_TOKEN" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == "model-token"
    assert env["CLAUDE_CODE_TMPDIR"] == env["TMPDIR"]
    assert env["CLAUDE_CODE_TMPDIR"].startswith(str(req.workspace_path))
    assert env["HOME"].startswith(str(req.workspace_path))


def test_real_adapter_command_forces_bash_tool():
    adapter = canary.ClaudeCodeCanaryAdapter()
    args = adapter.command("/opt/claude", nonce="abc123")
    assert args[0] == "-p"
    assert "/opt/claude" not in args
    assert "-p" in args
    assert "--allowedTools" in args
    assert args[args.index("--allowedTools") + 1] == "Bash"
    prompt = args[args.index("-p") + 1]
    assert "Bash tool" in prompt
    assert "anvil-canary-abc123" in prompt


def test_claude_adapter_only_accepts_actual_bash_side_effect(tmp_path):
    adapter = canary.ClaudeCodeCanaryAdapter()
    ok, detail = adapter.verify(tmp_path, "abc123", "CANARY_OK", 0)
    assert ok is False
    assert "Bash tool did not run" in detail
    (tmp_path / "anvil-canary-abc123").write_text("abc123", encoding="utf-8")
    ok, detail = adapter.verify(tmp_path, "abc123", "", 0)
    assert ok is True


def test_host_unsafe_is_not_evidence(conn, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "host-unsafe")
    backend = FakeBackend()
    result = _run(conn, FakeAdapter(), backend)
    assert result["ok"] is False and result["skipped"] is True
    assert "host-unsafe" in result["detail"]
    assert backend.requests == []


def test_runtime_without_adapter_is_unavailable(conn):
    runtime_service.register(
        conn, id="gemini-cli", display_name="Gemini",
        executable_path="/bin/echo", status="ready", capabilities={})
    result = canary.run_canary(conn, "gemini-cli", backend=FakeBackend())
    assert result["ok"] is False
    assert "适配器" in result["detail"]


def test_dispatch_guard_refuses_unavailable_runtime(conn, monkeypatch):
    monkeypatch.setattr(
        canary, "run_canary",
        lambda *a, **kw: {"ok": False, "detail": "EPERM mkdir claude-501",
                          "key_parts": {"os": "Darwin-1", "sandbox_backend": "seatbelt"}})
    ok, message = sandbox_service.runtime_dispatch_guard(conn, role_slug="dev")
    assert ok is False
    assert "EPERM" in message
    assert "self-check --force" in message


def test_dispatch_guard_allows_host_unsafe_for_tests(conn, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "host-unsafe")
    ok, message = sandbox_service.runtime_dispatch_guard(conn, role_slug="dev")
    assert ok is True and message == ""


def test_capability_matrix_exposes_cached_failure(conn, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(sandbox_service, "get_backend", lambda: backend)
    result = _run(conn, FakeAdapter(ok=False), backend)
    assert result["ok"] is False
    matrix = sandbox_service.capability_matrix(conn)
    assert "claude-code" in matrix["unavailable"]
    entry = next(r for r in matrix["runtimes"] if r["runtime_id"] == "claude-code")
    assert entry["canary"]["detail"] == "fake canary failed"
