"""Windows production implementation (owner 硬约束：Windows 第一要素).

Job Object 承载进程树（kill-on-close），Credential Manager 承载凭据。此模块仅在
`platform.system() == "Windows"` 时由 factory 导入；dev 机是 macOS，故此处代码
在 Windows 验收机（QA L5）上运行。
"""
from __future__ import annotations

import os
import subprocess

import win32api
import win32con
import win32cred
import win32job

from .base import CredentialStore, ProbeResult, ProcessManager, SpawnResult

# 会话 id → Job Object 句柄 / Popen 句柄（后端单进程长驻，模块级即可）。
_jobs: dict[str, int] = {}
_procs: dict[str, subprocess.Popen] = {}


class WindowsProcessManager(ProcessManager):
    def spawn(self, executable: str, args: list[str], cwd: str, env: dict[str, str],
              stdout=None, stderr=None) -> SpawnResult:
        # 用 subprocess.Popen 以便复用 Python 的 stdout/stderr 重定向；
        # 进程随后挂到 Job Object 上（kill-on-close，整棵进程树）。
        merged_env = {**os.environ, **env}
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            [executable, *args], cwd=cwd, env=merged_env,
            stdout=stdout, stderr=stderr, creationflags=flags,
        )

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicLimitInformation)
        info["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectBasicLimitInformation, info)

        access = (win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
                  | win32con.PROCESS_QUERY_INFORMATION)
        try:
            h_process = win32api.OpenProcess(access, False, proc.pid)
            win32job.AssignProcessToJobObject(job, h_process)
            win32api.CloseHandle(h_process)
        except Exception:  # noqa: BLE001 —— 赋值失败仍保留 Popen，至少可 terminate
            pass

        sid = f"job:{job}"
        _jobs[sid] = job
        _procs[sid] = proc
        return SpawnResult(pid=proc.pid, native_session_id=sid)

    def probe(self, native_session_id: str) -> ProbeResult:
        proc = _procs.get(native_session_id)
        if proc is not None:
            rc = proc.poll()
            job = _jobs.get(native_session_id)
            accounting = None
            if job is not None:
                try:
                    accounting = win32job.QueryInformationJobObject(
                        job, win32job.JobObjectBasicAccountingInformation
                    )
                except Exception:  # noqa: BLE001
                    accounting = None
            alive = rc is None or bool(accounting and accounting.get("ActiveProcesses", 0) > 0)
            if not alive:
                _procs.pop(native_session_id, None)
            return ProbeResult(alive=alive, pid=proc.pid, exit_code=rc)

        job = _jobs.get(native_session_id)
        if job is None:
            return ProbeResult(alive=False)
        accounting = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)
        alive = accounting.get("ActiveProcesses", 0) > 0
        return ProbeResult(alive=alive)

    def terminate(self, native_session_id: str) -> None:
        _procs.pop(native_session_id, None)
        job = _jobs.pop(native_session_id, None)
        if job is None:
            return
        try:
            win32job.TerminateJobObject(job, 1)
        finally:
            win32api.CloseHandle(job)


class WindowsCredentialStore(CredentialStore):
    def _target(self, service: str, account: str) -> str:
        return f"{service}/{account}"

    def get(self, service: str, account: str) -> str | None:
        try:
            cred = win32cred.CredRead(self._target(service, account), win32cred.CRED_TYPE_GENERIC, 0)
            return cred.get("CredentialBlob") or None
        except win32cred.error:
            return None

    def set(self, service: str, account: str, secret: str) -> None:
        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": self._target(service, account),
                "CredentialBlob": secret,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "UserName": account,
            },
            0,
        )

    def delete(self, service: str, account: str) -> None:
        try:
            win32cred.CredDelete(self._target(service, account), win32cred.CRED_TYPE_GENERIC, 0)
        except win32cred.error:
            pass
