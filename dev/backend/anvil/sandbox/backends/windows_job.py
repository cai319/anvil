"""Windows 持久进程后端：经 launcher 先入命名 Job Object，再 spawn 目标 CLI。

v1.51 的持久进程直接 ``Popen``，没有 Job Object；kill 只杀根进程，node/tool
子进程会残留。M0 用 launcher 消除"先创建、后归属"的竞态：launcher 自己先入
Job，再启动子进程，子进程自动继承 Job；控制器杀 launcher 即触发
``KILL_ON_JOB_CLOSE`` 回收整棵树。
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

from ..ports import SandboxCapabilities, SandboxProcess, SandboxProbe, SpawnRequest

DEFAULT_PIDS = 256
DEFAULT_RSS_MIB = 4096


class WindowsJobBackend:
    name = "windows_job"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            backend=self.name,
            platform="win32",
            process_tree=True,
            resource_caps=True,
            detail="named job object; write/read isolation pending AppContainer spike",
        )

    def spawn_persistent(self, req: SpawnRequest) -> SandboxProcess:
        job_name = f"Local\\anvil-sbx-{req.instance_id or 'adhoc'}"
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        cmd = [
            sys.executable, "-m", "anvil.sandbox.launcher",
            job_name,
            "--pids", str(DEFAULT_PIDS),
            "--rss-mib", str(DEFAULT_RSS_MIB),
            "--",
            req.executable, *req.args,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=req.cwd,
            env=req.env,
            stdin=req.stdin,
            stdout=req.stdout,
            stderr=req.stderr,
            creationflags=flags,
        )
        return SandboxProcess(
            popen=proc, backend=self.name,
            instance_id=req.instance_id, native_handle=job_name)

    def spawn_stdio(self, req: SpawnRequest) -> SandboxProcess:
        """launcher 继承三根管道；子进程自动继承 Job。"""
        return self.spawn_persistent(req)

    def kill(self, proc: Any, *, graceful: bool = True) -> None:
        if proc is None:
            return
        if graceful:
            stdin = getattr(proc, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        # 杀 launcher：其进程退出即关闭 Job 句柄，KILL_ON_JOB_CLOSE 回收整棵树。
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
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
