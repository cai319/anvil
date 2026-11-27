"""POSIX/宿主后端：行为等价于 v1.51 的持久进程 Popen。

M0 不提供写范围/读拒绝/网络隔离；它是 macOS/Linux 的接缝默认实现，避免
在没有容器/Seatbelt 的环境中改变现有语义。进程组回收在此补齐。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

from ..ports import SandboxCapabilities, SandboxProcess, SandboxProbe, SpawnRequest


class HostBackend:
    name = "host"

    def capabilities(self) -> SandboxCapabilities:
        has_killpg = hasattr(os, "killpg")
        return SandboxCapabilities(
            backend=self.name,
            platform=sys.platform,
            process_tree=has_killpg,
            detail="host process without OS confinement",
        )

    def spawn_persistent(self, req: SpawnRequest) -> SandboxProcess:
        kwargs: dict[str, Any] = dict(
            cwd=req.cwd,
            env=req.env,
            stdin=req.stdin,
            stdout=req.stdout,
            stderr=req.stderr,
        )
        try:
            proc = subprocess.Popen(
                [req.executable, *req.args], start_new_session=True, **kwargs)
        except TypeError:  # Windows 不支持 start_new_session
            proc = subprocess.Popen([req.executable, *req.args], **kwargs)
        handle = f"pg:{proc.pid}" if hasattr(os, "killpg") else f"pid:{proc.pid}"
        return SandboxProcess(
            popen=proc, backend=self.name,
            instance_id=req.instance_id, native_handle=handle)

    def kill(self, proc: Any, *, graceful: bool = True) -> None:
        if proc is None:
            return
        if graceful:
            stdin = getattr(proc, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:  # noqa: BLE001 —— 管道可能已关闭
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._signal_group(proc, signal.SIGTERM)
        if proc.poll() is None:
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        # 根进程可能已退出但组内还有子进程；组存在时再补一刀。
        self._signal_group(proc, signal.SIGKILL)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    def probe(self, proc: Any) -> SandboxProbe:
        if proc is None:
            return SandboxProbe(alive=False)
        code = proc.poll()
        return SandboxProbe(
            alive=code is None,
            pid=getattr(proc, "pid", None),
            exit_code=code,
        )

    @staticmethod
    def _signal_group(proc: Any, sig: int) -> None:
        """只向仍属于本进程的进程组发信号，避免误杀复用 PID 的无关进程。"""
        if not hasattr(os, "killpg"):
            return
        pid = int(getattr(proc, "pid", 0) or 0)
        if pid <= 1:
            return
        try:
            if os.getpgid(pid) != pid:
                return
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
