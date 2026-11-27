"""Windows-only Job Object launcher.

由 ``WindowsJobBackend`` 以 ``python -m anvil.sandbox.launcher`` 启动：

    launcher <job-name> --pids N --rss-mib M -- <executable> [args...]

launcher 先把自身加入命名 Job，再启动目标 CLI 并等待；目标进程自动继承
Job。控制器杀掉 launcher 时，Job 句柄关闭，``KILL_ON_JOB_CLOSE`` 回收整棵
进程树。launcher 不读写 stdin/stdout，避免污染角色 CLI 的 stream-json 管道。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Any


def _launcher_log(text: str) -> None:
    """诊断只写临时文件；绝不能写 stdout/stderr（那是 agent 的协议管道）。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "anvil-sandbox-launcher.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
    except OSError:
        pass


def _setup_job(job_name: str, pids: int, rss_mib: int) -> Any:
    """创建命名 Job、设限、把当前进程加入；失败返回 None（降级为普通子进程）。"""
    try:
        import win32api
        import win32con
        import win32job
    except Exception as exc:  # noqa: BLE001 —— pywin32 未安装时仍可运行
        _launcher_log(f"pywin32 unavailable: {exc!r}")
        return None
    try:
        job = win32job.CreateJobObject(None, job_name)
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation)
        basic = info.get("BasicLimitInformation") or {}
        flags = int(basic.get("LimitFlags") or 0)
        flags |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if pids > 0:
            flags |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            basic["ActiveProcessLimit"] = int(pids)
        basic["LimitFlags"] = flags
        info["BasicLimitInformation"] = basic
        if rss_mib > 0:
            info["JobMemoryLimit"] = int(rss_mib) * 1024 * 1024
            basic["LimitFlags"] = flags | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            info["BasicLimitInformation"] = basic
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info)
        h_self = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_QUERY_INFORMATION,
            False, os.getpid())
        try:
            win32job.AssignProcessToJobObject(job, h_self)
        finally:
            win32api.CloseHandle(h_self)
        return job
    except Exception as exc:  # noqa: BLE001 —— 归属失败不阻塞角色启动
        _launcher_log(f"job setup failed ({job_name}): {exc!r}")
        return None


def _parse(argv: list[str]) -> tuple[str, int, int, list[str]]:
    if not argv:
        raise SystemExit("usage: launcher <job-name> [--pids N] [--rss-mib M] -- cmd...")
    job_name = argv[0]
    pids, rss_mib = 256, 4096
    i = 1
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--pids" and i + 1 < len(argv):
            pids = int(argv[i + 1]); i += 2
        elif argv[i] == "--rss-mib" and i + 1 < len(argv):
            rss_mib = int(argv[i + 1]); i += 2
        else:
            i += 1
    if i >= len(argv) or argv[i] != "--" or i + 1 >= len(argv):
        raise SystemExit("usage: launcher <job-name> [--pids N] [--rss-mib M] -- cmd...")
    return job_name, pids, rss_mib, argv[i + 1:]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    job_name, pids, rss_mib, cmd = _parse(args)
    job = _setup_job(job_name, pids, rss_mib)
    try:
        # stdin/stdout/stderr 全部继承：控制器直接与目标 CLI 通信。
        proc = subprocess.Popen(cmd, cwd=None, env=os.environ)
        return int(proc.wait())
    except FileNotFoundError:
        _launcher_log(f"executable not found: {cmd!r}")
        return 127
    except Exception as exc:  # noqa: BLE001
        _launcher_log(f"spawn failed: {exc!r}")
        return 126
    finally:
        # 显式释放 Job 句柄：KILL_ON_JOB_CLOSE 在最后一句柄关闭时回收残留。
        if job is not None:
            try:
                import win32api
                win32api.CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
