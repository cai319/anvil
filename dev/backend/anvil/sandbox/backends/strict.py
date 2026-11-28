"""严格档 runner 选择：Windows 优先 WSL2+bwrap，其它平台用 Docker/Podman。"""
from __future__ import annotations

import platform


def select_runner():
    """返回 (runner, available, detail)。不可用时仍返回可为 provisioning 的 runner。"""
    if platform.system() == "Windows":
        from .wsl_bwrap import WSLBwrapRunner

        wsl = WSLBwrapRunner()
        ok, detail = wsl.available()
        if ok:
            return wsl, True, detail
        from .docker import DockerRunner

        docker = DockerRunner()
        dok, ddetail = docker.available()
        if dok:
            return docker, True, ddetail
        return wsl, False, detail
    from .docker import DockerRunner

    runner = DockerRunner()
    ok, detail = runner.available()
    return runner, ok, detail
