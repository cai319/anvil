"""Selects the platform backend at import time."""
from __future__ import annotations

import platform

from .base import CredentialStore, ProcessManager


def _make() -> tuple[ProcessManager, CredentialStore]:
    system = platform.system()
    if system == "Darwin":
        from .darwin import DarwinCredentialStore, DarwinProcessManager

        return DarwinProcessManager(), DarwinCredentialStore()
    if system == "Windows":
        from .windows import WindowsCredentialStore, WindowsProcessManager

        return WindowsProcessManager(), WindowsCredentialStore()
    raise RuntimeError(f"Unsupported platform: {system}")


_process_manager: ProcessManager | None = None
_credential_store: CredentialStore | None = None


def get_platform() -> tuple[ProcessManager, CredentialStore]:
    global _process_manager, _credential_store
    if _process_manager is None:
        _process_manager, _credential_store = _make()
    return _process_manager, _credential_store
