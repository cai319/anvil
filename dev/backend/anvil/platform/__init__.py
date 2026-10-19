"""Platform abstraction layer (R-平台).

Production semantics target Windows (pywin32 Job Object, %APPDATA%, credential
manager); dev/test runs on macOS/arm64 through an equivalent shim. The backend
depends only on `base.ProcessManager` / `base.CredentialStore`, never on the OS.
"""
from __future__ import annotations

from .base import CredentialStore, ProcessManager
from .factory import get_platform

__all__ = ["CredentialStore", "ProcessManager", "get_platform"]
