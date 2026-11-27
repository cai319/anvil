"""平台基质探测（M1）。

只做轻量探测，供状态 API 与后端选择使用；不启动容器/发行版，避免拖慢启动。
深度自检（sandbox-exec profile 预检、Docker info、WSL bwrap）在 M1 自检端点/后续里程碑。
"""
from __future__ import annotations

import platform
import shutil

from .backends.seatbelt import available as seatbelt_available


def substrates() -> dict:
    system = platform.system()
    return {
        "platform": system,
        "seatbelt": seatbelt_available(),
        "docker": shutil.which("docker") is not None,
        "podman": shutil.which("podman") is not None,
        "wsl": system == "Windows" and shutil.which("wsl.exe") is not None,
    }
