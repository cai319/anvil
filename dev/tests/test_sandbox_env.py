"""沙箱 HOME/TMP 环境重定向（M1）。"""
from __future__ import annotations

from pathlib import Path

from anvil.sandbox import env


def test_prepare_home_under_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, tmp = env.prepare_home(str(ws), str(tmp_path / "role"),
                                 str(tmp_path / "inst"))
    assert home == ws / ".anvil" / "home"
    assert home.is_dir()
    assert (home / ".claude").is_dir()
    assert tmp == tmp_path / "inst" / "tmp"
    assert tmp.is_dir()


def test_prepare_home_falls_back_to_role_home(tmp_path):
    role = tmp_path / "role"
    role.mkdir()
    home, tmp = env.prepare_home(None, str(role), str(tmp_path / "inst"))
    assert home == role / ".anvil" / "home"
    assert tmp.is_dir()


def test_apply_standard_env_redirects_cache_and_keeps_anvil(tmp_path):
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir(); tmp.mkdir()
    out = env.apply_standard_env(
        {"ANVIL_TOKEN": "role-token", "PATH": "/usr/bin"}, home=home, tmp=tmp)
    assert out["HOME"] == str(home)
    assert out["USERPROFILE"] == str(home)
    assert out["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    assert out["CLAUDE_CODE_TMPDIR"] == str(tmp)
    assert out["XDG_RUNTIME_DIR"] == str(home / ".cache")
    assert out["TMPDIR"] == str(tmp)
    assert out["TMP"] == str(tmp)
    assert out["TEMP"] == str(tmp)
    assert out["XDG_CACHE_HOME"] == str(home / ".cache")
    assert out["npm_config_cache"] == str(home / ".npm")
    assert out["PIP_CACHE_DIR"] == str(home / ".pip")
    assert out["ANVIL_TOKEN"] == "role-token"
    assert out["PATH"] == "/usr/bin"


def test_apply_standard_env_strips_host_session_markers(tmp_path):
    """从 agent shell 启动后端时，宿主会话变量不能带进角色进程。"""
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir(); tmp.mkdir()
    out = env.apply_standard_env({
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_CHILD_NOTES_DIR": "/private/tmp/claude-501/x",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/1.sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
        "CLAUDE_CODE_SESSION_ID": "uuid",
        "CLAUDE_CODE_EXECPATH": "/opt/homebrew/bin/claude",
        "CLAUDE_PID": "123",
        "XDG_RUNTIME_DIR": "/run/user/501",
        "TMPDIR": "/var/folders/x",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "high",
        "ANTHROPIC_AUTH_TOKEN": "token",
        "PATH": "/usr/bin",
    }, home=home, tmp=tmp)
    for key in (
            "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_CHILD_NOTES_DIR", "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_EXECPATH", "CLAUDE_PID"):
        assert key not in out
    assert out["CLAUDE_CODE_TMPDIR"] == str(tmp)
    assert out["XDG_RUNTIME_DIR"] == str(home / ".cache")
    assert out["TMPDIR"] == str(tmp)
    # Anvil 注入的模型/强度变量与模型凭证继续继承。
    assert out["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-flash"
    assert out["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "token"
    assert out["PATH"] == "/usr/bin"


def test_host_session_env_reports_markers():
    assert env.host_session_env({
        "CLAUDECODE": "1", "PATH": "/usr/bin",
    }) == ["CLAUDECODE"]
    assert env.host_session_env({"PATH": "/usr/bin"}) == []
