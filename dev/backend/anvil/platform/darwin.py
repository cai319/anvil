"""macOS shim for dev/test.

Process grouping uses a POSIX process group (setsid); credentials fall back to
the `security` CLI (Keychain), then to a file-backed store if Keychain is
unavailable (e.g. headless CI).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .. import config
from .base import CredentialStore, ProbeResult, ProcessManager, SpawnResult

# native_session_id → Popen 句柄（后端单进程长驻，模块级即可）。保留句柄以在 probe
# 时用 poll() 拿退出码；重启后句柄丢失则回退到 _pid_alive（exit_code=None）。
_procs: dict[str, subprocess.Popen] = {}

# 不泄漏给子进程的编排器会话变量（保留 ANTHROPIC_* 等运行时后端配置）。
_SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
_SESSION_ENV_KEYS = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED"}


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    """子进程环境：继承 os.environ 但去掉编排器自身的会话变量，再叠加运行时 env。"""
    merged = {
        k: v for k, v in os.environ.items()
        if not k.startswith(_SESSION_ENV_PREFIXES) and k not in _SESSION_ENV_KEYS
    }
    merged.update(extra)
    return merged


class DarwinProcessManager(ProcessManager):
    def spawn(self, executable: str, args: list[str], cwd: str, env: dict[str, str],
              stdout=None, stderr=None) -> SpawnResult:
        merged_env = _clean_env(env)
        proc = subprocess.Popen(
            [executable, *args],
            cwd=cwd,
            env=merged_env,
            start_new_session=True,  # own process group → group-kill on terminate
            stdout=stdout,
            stderr=stderr,
        )
        sid = f"pg:{proc.pid}"
        _procs[sid] = proc
        return SpawnResult(pid=proc.pid, native_session_id=sid)

    def probe(self, native_session_id: str) -> ProbeResult:
        if not native_session_id.startswith("pg:"):
            return ProbeResult(alive=False)
        pid = int(native_session_id[3:])
        proc = _procs.get(native_session_id)
        if proc is not None:
            rc = proc.poll()
            if rc is not None:
                _procs.pop(native_session_id, None)
                return ProbeResult(alive=False, pid=pid, exit_code=rc)
            return ProbeResult(alive=True, pid=pid)
        alive = _pid_alive(pid)
        return ProbeResult(alive=alive, pid=pid)

    def terminate(self, native_session_id: str) -> None:
        if native_session_id.startswith("pg:"):
            pid = int(native_session_id[3:])
            try:
                os.killpg(pid, 15)  # SIGTERM the whole group
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(pid, os.WNOHANG)  # 若是父进程，回收僵尸
            except (ChildProcessError, OSError):
                pass
        _procs.pop(native_session_id, None)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    # 进程存在，但可能是已终止的僵尸（SIGTERM 后未回收）；用 ps 判断状态。
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return True
    if not out:
        return False
    return not out.startswith("Z")


class DarwinCredentialStore(CredentialStore):
    def get(self, service: str, account: str) -> str | None:
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
            )
            return out.stdout.rstrip("\n") or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            return self._file_store().get(f"{service}:{account}")

    def set(self, service: str, account: str, secret: str) -> None:
        try:
            subprocess.run(
                ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            data = self._file_store()
            data[f"{service}:{account}"] = secret
            self._write_file_store(data)

    def delete(self, service: str, account: str) -> None:
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            data = self._file_store()
            data.pop(f"{service}:{account}", None)
            self._write_file_store(data)

    def _store_path(self) -> Path:
        return config.DATA_DIR / ".credentials.json"

    def _file_store(self) -> dict[str, str]:
        p = self._store_path()
        if p.exists():
            return json.loads(p.read_text() or "{}")
        return {}

    def _write_file_store(self, data: dict[str, str]) -> None:
        p = self._store_path()
        p.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(p, 0o600)  # 明文回退文件至少收紧权限
        except OSError:
            pass
