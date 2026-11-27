"""macOS Seatbelt 标准档（M1）：写范围、秘密读拒绝、profile 生成。

集成用例在没有 sandbox-exec 的机器（含 CI Linux）自动跳过。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil import config
from anvil.sandbox.backends import seatbelt


pytestmark = pytest.mark.skipif(
    not seatbelt.available(), reason="sandbox-exec unavailable")


def _run(profile_path: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [seatbelt.SANDBOX_EXEC, "-f", str(profile_path), "/bin/sh", "-c", script],
        capture_output=True, text=True, timeout=30)


def _profile(tmp_path: Path, monkeypatch) -> Path:
    ws = tmp_path / "ws"; ws.mkdir()
    role = tmp_path / "role"; role.mkdir()
    inst = tmp_path / "inst"; inst.mkdir()
    secret = tmp_path / "auth-token"; secret.write_text("secret\n")
    db = tmp_path / "anvil.db"; db.write_text("db\n")
    graph = tmp_path / "graph.db"; graph.write_text("db\n")
    monkeypatch.setattr(config, "AUTH_TOKEN_FILE", secret)
    monkeypatch.setattr(config, "SQLITE_PATH", db)
    monkeypatch.setattr(config, "GRAPH_DB_PATH", graph)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "TASK_WORKSPACES_DIR", tmp_path / "task-workspaces")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "SANDBOX_INSTANCES_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    text = seatbelt.build_profile(
        workspace=str(ws), role_home=str(role), instance_dir=str(inst))
    path = inst / "profile.sb"
    path.write_text(text, encoding="utf-8")
    return path


def test_profile_escalation_rejected():
    with pytest.raises(ValueError):
        seatbelt.build_profile(
            workspace='/tmp/evil"\n(allow everything)',
            role_home="/tmp/role", instance_dir="/tmp/inst")


def test_write_scope(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    assert _run(profile, f"echo ok > {tmp_path}/ws/a.txt").returncode == 0
    denied = _run(profile, f"echo bad > {tmp_path}/outside.txt")
    assert denied.returncode != 0
    assert "Operation not permitted" in (denied.stderr or "")


def test_secret_reads_denied(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    assert _run(profile, f"cat {tmp_path}/auth-token").returncode != 0
    assert _run(profile, f"cat {tmp_path}/anvil.db").returncode != 0
    # 角色 home 可写可读；其它角色 home 被拒。
    other = tmp_path / "agents" / "other"; other.mkdir(parents=True)
    (other / "instruction.md").write_text("x")
    assert _run(profile, f"echo hi > {tmp_path}/role/r.txt").returncode == 0
    assert _run(profile, f"cat {other}/instruction.md").returncode != 0


def test_workspace_read_allowed(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    (tmp_path / "ws" / "src.c").write_text("int main(void){return 0;}\n")
    assert _run(profile, f"cat {tmp_path}/ws/src.c").returncode == 0


def test_status_and_self_check_report_seatbelt(monkeypatch):
    from anvil import config as app_config
    from anvil.services import sandbox

    monkeypatch.setattr(app_config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(app_config, "SANDBOX_MODE", "enforce")
    sandbox.reset_for_tests(seatbelt.SeatbeltBackend())
    try:
        status = sandbox.status()
        assert status["standard"]["backend"] == "seatbelt"
        assert status["standard"]["capabilities"]["write_scope"] is True
        assert "host_unsafe" not in status["warnings"]
        check = sandbox.self_check()
        assert check["checks"]["profile_preflight"] is True
    finally:
        sandbox.reset_for_tests(None)
