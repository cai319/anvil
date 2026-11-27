"""沙箱后端选择。

M0 只区分 POSIX Host 与 Windows 命名 Job；严格档后端在 M2/M3 加入。
导入时看平台，Windows 以外不会导入 pywin32。
"""
from __future__ import annotations

import platform as _platform

from .host import HostBackend


def make_backend():
    system = _platform.system()
    if system == "Windows":
        from .windows_job import WindowsJobBackend

        return WindowsJobBackend()
    return HostBackend()


__all__ = ["HostBackend", "make_backend"]
