"""沙箱端口与值对象。

M0 只落地"整个角色进程经后端启动/回收"这一条接缝；策略、能力矩阵与严格档
作业在后续里程碑扩展。这里的类型保持无 DB、无副作用，便于单测与替换后端。
"""
from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

TIER_STANDARD = "standard"
TIER_STRICT = "strict"


@dataclass(frozen=True)
class SandboxCapabilities:
    """后端实际可提供的能力；缺口必须显式暴露，不得静默降级。"""

    backend: str
    platform: str
    write_scope: bool = False
    read_deny: bool = False
    resource_caps: bool = False
    process_tree: bool = False
    network_none: bool = False
    disposable_fs: bool = False
    mount_isolation: bool = False
    artifact_export: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SandboxPolicy:
    """一次执行所需的隔离策略；M0 固定 standard，后续由 policy 解析器生成。"""

    tier: str = TIER_STANDARD
    profile: str = "standard"
    backend: str | None = None
    network: str = "host"
    resources: dict[str, Any] = field(default_factory=dict)
    policy_digest: str = ""
    source: str = "default"


@dataclass
class SpawnRequest:
    """拉起一个角色进程所需的全部输入（已清洗的环境）。"""

    executable: str
    args: list[str]
    cwd: str
    env: dict[str, str]
    stdin: int | None = subprocess.PIPE
    stdout: int | None = subprocess.PIPE
    stderr: int | None = subprocess.STDOUT
    instance_id: str | None = None


@dataclass
class SandboxProcess:
    """后端返回的进程句柄；``popen`` 保持现有 reader 线程可直接复用。"""

    popen: Any
    backend: str
    instance_id: str | None = None
    native_handle: str | None = None


@dataclass
class SandboxProbe:
    alive: bool
    pid: int | None = None
    exit_code: int | None = None


@runtime_checkable
class SandboxBackend(Protocol):
    name: str

    def capabilities(self) -> SandboxCapabilities:
        ...

    def spawn_persistent(self, req: SpawnRequest) -> SandboxProcess:
        ...

    def kill(self, proc: Any, *, graceful: bool = True) -> None:
        ...

    def probe(self, proc: Any) -> SandboxProbe:
        ...
