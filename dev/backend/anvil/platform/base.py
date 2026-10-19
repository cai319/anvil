"""Abstract interfaces for OS-dependent process/credential behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SpawnResult:
    pid: int
    native_session_id: str | None = None


@dataclass
class ProbeResult:
    alive: bool
    pid: int | None = None
    exit_code: int | None = None  # 进程已退出时的返回码；未知（重启丢失句柄）为 None


class ProcessManager(ABC):
    """Manages child runtimes as an OS-level unit (job object / process group)."""

    @abstractmethod
    def spawn(self, executable: str, args: list[str], cwd: str, env: dict[str, str],
              stdout=None, stderr=None) -> SpawnResult:
        ...

    @abstractmethod
    def probe(self, native_session_id: str) -> ProbeResult:
        ...

    @abstractmethod
    def terminate(self, native_session_id: str) -> None:
        ...


class CredentialStore(ABC):
    """Stores runtime credentials (e.g. API keys) using the OS vault."""

    @abstractmethod
    def get(self, service: str, account: str) -> str | None:
        ...

    @abstractmethod
    def set(self, service: str, account: str, secret: str) -> None:
        ...

    @abstractmethod
    def delete(self, service: str, account: str) -> None:
        ...
