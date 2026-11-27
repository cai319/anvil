"""沙箱后端（M0）：Host 进程组回收、Windows launcher 命令构建、launcher 解析。"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from anvil.sandbox.backends.host import HostBackend
from anvil.sandbox.backends.windows_job import WindowsJobBackend
from anvil.sandbox.launcher import _parse
from anvil.sandbox.ports import SpawnRequest


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        time.sleep(0.1)
    return False


def test_host_backend_spawns_with_binary_pipes(tmp_path):
    backend = HostBackend()
    req = SpawnRequest(
        executable=sys.executable,
        args=["-c", "import sys; print('hello', flush=True); sys.stdin.read()"],
        cwd=str(tmp_path),
        env=dict(os.environ),
    )
    sp = backend.spawn_persistent(req)
    try:
        assert sp.native_handle and sp.native_handle.startswith("pg:")
        line = sp.popen.stdout.readline()
        assert b"hello" in line
    finally:
        backend.kill(sp.popen, graceful=True)
    assert sp.popen.poll() is not None


def test_host_backend_kills_process_group(tmp_path):
    """根进程与它的子进程必须一起被回收，不能留下孤儿。"""
    backend = HostBackend()
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    req = SpawnRequest(
        executable=sys.executable, args=["-c", script],
        cwd=str(tmp_path), env=dict(os.environ),
    )
    sp = backend.spawn_persistent(req)
    try:
        child_pid = int(sp.popen.stdout.readline().strip())
        backend.kill(sp.popen, graceful=False)
        assert sp.popen.poll() is not None
        assert _wait_gone(child_pid)
    finally:
        try:
            sp.popen.kill()
        except Exception:  # noqa: BLE001
            pass


def test_windows_backend_uses_launcher(monkeypatch):
    captured: dict = {}

    class FakeProc:
        pid = 4321
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(
        "anvil.sandbox.backends.windows_job.subprocess.Popen", fake_popen)
    req = SpawnRequest(
        executable="claude.exe", args=["--print", "--session-id", "sid"],
        cwd="C:\\work", env={"PATH": "x"}, instance_id="sbi_test",
    )
    sp = WindowsJobBackend().spawn_persistent(req)

    cmd = captured["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "anvil.sandbox.launcher"]
    assert cmd[-5:] == ["--", "claude.exe", "--print", "--session-id", "sid"]
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.STDOUT
    assert sp.native_handle == "Local\\anvil-sbx-sbi_test"


def test_launcher_parse():
    job, pids, rss, cmd = _parse(
        ["Local\\anvil-sbx-x", "--pids", "128", "--rss-mib", "2048", "--",
         "/usr/bin/true", "arg"])
    assert job == "Local\\anvil-sbx-x"
    assert pids == 128
    assert rss == 2048
    assert cmd == ["/usr/bin/true", "arg"]
