"""运行时发现：npm 全局布局（Windows/Unix）与缺失诊断日志。

Windows 上 `npm prefix -g` 返回的目录直接把 `claude.cmd` 放在根下；
Unix 上才是 `<prefix>/bin/claude`。两条路径都必须能发现。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from anvil.services import runtime as runtime_service


def _fake_npm(monkeypatch, prefix: Path) -> None:
    def fake_which(name, *args, **kwargs):
        if name == "npm":
            return r"C:\Program Files\nodejs\npm.cmd"
        return None

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{prefix}\n", stderr="")

    monkeypatch.setattr(runtime_service.shutil, "which", fake_which)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)


def test_npm_global_finds_windows_cmd_layout(tmp_path, monkeypatch):
    """Windows: <prefix>/claude.cmd（不是 <prefix>/bin/claude.cmd）。"""
    prefix = tmp_path / "npm"
    prefix.mkdir()
    (prefix / "claude.cmd").write_text("@echo off\n", encoding="utf-8")
    _fake_npm(monkeypatch, prefix)

    assert runtime_service._npm_global("@anthropic-ai/claude-code") == str(prefix / "claude.cmd")


def test_npm_global_maps_gemini_cli_package_to_gemini_binary(tmp_path, monkeypatch):
    """@google/gemini-cli 安装的可执行名是 gemini，不是 gemini-cli。"""
    prefix = tmp_path / "npm"
    prefix.mkdir()
    (prefix / "gemini.cmd").write_text("@echo off\n", encoding="utf-8")
    _fake_npm(monkeypatch, prefix)

    assert runtime_service._npm_global("@google/gemini-cli") == str(prefix / "gemini.cmd")


def test_npm_global_finds_unix_bin_layout(tmp_path, monkeypatch):
    """Unix: <prefix>/bin/claude 仍然可发现（回归保护）。"""
    prefix = tmp_path / "prefix"
    bindir = prefix / "bin"
    bindir.mkdir(parents=True)
    (bindir / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
    (bindir / "claude").chmod(0o755)
    _fake_npm(monkeypatch, prefix)

    assert runtime_service._npm_global("@anthropic-ai/claude-code") == str(bindir / "claude")


def test_discovery_log_lists_attempted_strategies():
    """找不到可执行文件时，日志要说明尝试过哪些策略，便于 UI 诊断。"""
    definition = {
        "discovery": {
            "strategies": [
                {"type": "path_lookup", "command": "claude"},
                {"type": "fixed_path", "candidates": ["C:/a/claude.exe", "C:/b/claude.exe"]},
            ]
        }
    }
    log = runtime_service._discovery_log(definition)
    assert "claude" in log
    assert "C:/a/claude.exe" in log
    assert "C:/b/claude.exe" in log


def _win_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_service, "_is_windows", lambda: True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("PATH", "")


def test_prefer_native_exe_sibling(tmp_path, monkeypatch):
    """Windows: 同目录存在 claude.exe 时，不返回 claude.cmd。"""
    _win_env(tmp_path, monkeypatch)
    cmd = tmp_path / "claude.cmd"
    cmd.write_text("@echo off\n", encoding="utf-8")
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")

    assert runtime_service._prefer_native_exe(str(cmd), "claude") == str(exe)


def test_prefer_native_exe_on_path(tmp_path, monkeypatch):
    """PATH 中其它目录的 .exe 优先于先命中的 .cmd。"""
    _win_env(tmp_path, monkeypatch)
    cmd = tmp_path / "npm" / "claude.cmd"
    cmd.parent.mkdir()
    cmd.write_text("@echo off\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir))

    assert runtime_service._prefer_native_exe(str(cmd), "claude") == str(exe)


def test_prefer_native_exe_native_install(tmp_path, monkeypatch):
    """Windows 原生安装位置 %USERPROFILE%\\.local\\bin\\claude.exe。"""
    _win_env(tmp_path, monkeypatch)
    cmd = tmp_path / "npm" / "claude.cmd"
    cmd.parent.mkdir()
    cmd.write_text("@echo off\n", encoding="utf-8")
    exe = tmp_path / "home" / ".local" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    assert runtime_service._prefer_native_exe(str(cmd), "claude") == str(exe)


def test_resolve_skips_cmd_for_later_native_exe(tmp_path, monkeypatch):
    """path_lookup 命中 .cmd 时，继续尝试 fixed_path 找原生 .exe。"""
    _win_env(tmp_path, monkeypatch)
    cmd = tmp_path / "npm" / "claude.cmd"
    cmd.parent.mkdir()
    cmd.write_text("@echo off\n", encoding="utf-8")
    exe = tmp_path / "native" / "claude.exe"
    exe.parent.mkdir()
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        runtime_service.shutil, "which",
        lambda name, *args, **kwargs: str(cmd) if name == "claude" else None,
    )

    strategies = [
        {"type": "path_lookup", "command": "claude"},
        {"type": "fixed_path", "candidates": [str(exe)]},
    ]
    assert runtime_service._resolve_executable(strategies) == str(exe)


def test_find_executable_prefers_exe_on_windows(tmp_path, monkeypatch):
    _win_env(tmp_path, monkeypatch)
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "claude.cmd").write_text("", encoding="utf-8")
    exe = directory / "claude.exe"
    exe.write_text("", encoding="utf-8")

    assert runtime_service._find_executable(directory, ["claude"]) == str(exe)


def test_wrapper_warning_mentions_cmd(tmp_path, monkeypatch):
    _win_env(tmp_path, monkeypatch)
    assert ".cmd" in (runtime_service._wrapper_warning("claude.cmd") or "")


def test_effective_executable_is_noop_on_non_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_service, "_is_windows", lambda: False)
    cmd = tmp_path / "claude.cmd"
    cmd.write_text("@echo off\n", encoding="utf-8")
    assert runtime_service.effective_executable(str(cmd)) == str(cmd)
