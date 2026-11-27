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
    assert out["TMPDIR"] == str(tmp)
    assert out["TMP"] == str(tmp)
    assert out["TEMP"] == str(tmp)
    assert out["XDG_CACHE_HOME"] == str(home / ".cache")
    assert out["npm_config_cache"] == str(home / ".npm")
    assert out["PIP_CACHE_DIR"] == str(home / ".pip")
    assert out["ANVIL_TOKEN"] == "role-token"
    assert out["PATH"] == "/usr/bin"
