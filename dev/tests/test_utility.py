"""Utility 调用：通过 CLI 运行时执行，不依赖 ANTHROPIC_* 环境变量。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from anvil import config
from anvil.db import connect
from anvil.services import runtime as runtime_service
from anvil.services import utility


def _prepare_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    user_dir = tmp_path / "runtimes"
    pkg_dir = tmp_path / "builtin"
    user_dir.mkdir()
    pkg_dir.mkdir()
    monkeypatch.setattr(config, "USER_RUNTIMES_DIR", user_dir)
    monkeypatch.setattr(config, "PACKAGE_RUNTIMES_DIR", pkg_dir)
    return user_dir, pkg_dir


def _add_runtime(conn, tmp_path: Path, user_dir: Path, rid: str, body: str) -> None:
    script = tmp_path / f"{rid}.py"
    script.write_text(body, encoding="utf-8")
    definition = {
        "id": rid,
        "display_name": rid,
        "invocation": {
            "args": [str(script), "{{ prompt }}"],
            "cwd": "{{ workspace_path }}",
        },
    }
    (user_dir / f"{rid}.yaml").write_text(
        yaml.safe_dump(definition, allow_unicode=True), encoding="utf-8")
    runtime_service.register(
        conn, id=rid, display_name=rid, executable_path=sys.executable, status="ready")


def test_generate_uses_ready_runtime_as_fallback(tmp_path, monkeypatch):
    """没有显式默认时，退到第一个 ready 运行时。"""
    user_dir, _ = _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))
    _add_runtime(conn, tmp_path, user_dir, "fake-utility",
                 "import sys\nprint(sys.argv[1] if len(sys.argv) > 1 else '')\n")

    assert utility.generate(conn, "hello world") == "hello world"


def test_default_runtime_setting_is_used(tmp_path, monkeypatch):
    user_dir, _ = _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))
    _add_runtime(conn, tmp_path, user_dir, "runtime-a", "print('A')\n")
    _add_runtime(conn, tmp_path, user_dir, "runtime-b", "print('B')\n")

    utility.set_default_runtime_id(conn, "runtime-b")
    assert utility.get_default_runtime_id(conn) == "runtime-b"
    assert utility.generate(conn, "x") == "B"


def test_explicit_runtime_overrides_default(tmp_path, monkeypatch):
    user_dir, _ = _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))
    _add_runtime(conn, tmp_path, user_dir, "runtime-a", "print('A')\n")
    _add_runtime(conn, tmp_path, user_dir, "runtime-b", "print('B')\n")

    utility.set_default_runtime_id(conn, "runtime-b")
    assert utility.generate(conn, "x", runtime_id="runtime-a") == "A"


def test_no_runtime_raises_actionable_error(tmp_path, monkeypatch):
    _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))

    with pytest.raises(utility.UtilityError) as exc:
        utility.generate(conn, "x", timeout=1)
    assert "没有可用的运行时" in str(exc.value)


def test_timeout_is_reported(tmp_path, monkeypatch):
    user_dir, _ = _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))
    _add_runtime(conn, tmp_path, user_dir, "slow", "import time\ntime.sleep(5)\n")

    with pytest.raises(utility.UtilityError) as exc:
        utility.generate(conn, "x", runtime_id="slow", timeout=0.2)
    assert "超时" in str(exc.value)


def test_nonzero_exit_includes_output(tmp_path, monkeypatch):
    user_dir, _ = _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))
    _add_runtime(conn, tmp_path, user_dir, "failing",
                 "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")

    with pytest.raises(utility.UtilityError) as exc:
        utility.generate(conn, "x", runtime_id="failing", timeout=5)
    msg = str(exc.value)
    assert "退出码 3" in msg
    assert "boom" in msg


def test_empty_output_is_reported(tmp_path, monkeypatch):
    user_dir, _ = _prepare_dirs(tmp_path, monkeypatch)
    conn = connect(str(tmp_path / "test.db"))
    _add_runtime(conn, tmp_path, user_dir, "empty", "pass\n")

    with pytest.raises(utility.UtilityError) as exc:
        utility.generate(conn, "x", runtime_id="empty", timeout=5)
    assert "没有返回内容" in str(exc.value)
