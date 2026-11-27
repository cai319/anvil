"""执行沙箱子系统（v1.60 M0）。

端口与后端在此包内；DB 单写者在 ``anvil.services.sandbox``。
本模块刻意不导入任何平台后端，避免在 macOS 上拉起 pywin32。
"""
from __future__ import annotations

from .ports import (
    TIER_STANDARD,
    TIER_STRICT,
    SandboxBackend,
    SandboxCapabilities,
    SandboxPolicy,
    SandboxProcess,
    SpawnRequest,
)

__all__ = [
    "TIER_STANDARD",
    "TIER_STRICT",
    "SandboxBackend",
    "SandboxCapabilities",
    "SandboxPolicy",
    "SandboxProcess",
    "SpawnRequest",
]
