"""One-shot 运行的沙箱包装（M0）。

M0 不改变底层 one-shot 的启动方式；只把 ``native_session_id`` 映射到一条
sandbox instance 记录，供后续里程碑接策略/能力/日志。记录失败绝不阻断 spawn。
"""
from __future__ import annotations

import logging

from .base import ProbeResult, ProcessManager, SpawnResult

logger = logging.getLogger("anvil.platform.sandbox")


class SandboxProcessManager(ProcessManager):
    supports_sandbox = True

    def __init__(self, inner: ProcessManager) -> None:
        self._inner = inner
        self._instances: dict[str, str] = {}

    def spawn(self, executable: str, args: list[str], cwd: str, env: dict[str, str],
              stdout=None, stderr=None,
              sandbox_policy: dict | None = None) -> SpawnResult:
        result = self._inner.spawn(
            executable, args, cwd, env, stdout=stdout, stderr=stderr)
        try:
            from ..services import sandbox as sandbox_service
            iid = sandbox_service.record_oneshot(
                native_session_id=result.native_session_id, pid=result.pid)
            if iid:
                if result.native_session_id:
                    self._instances[result.native_session_id] = iid
                result.sandbox_instance_id = iid
        except Exception:  # noqa: BLE001 —— 观测失败不影响运行
            logger.debug("sandbox instance record failed", exc_info=True)
        return result

    def probe(self, native_session_id: str) -> ProbeResult:
        return self._inner.probe(native_session_id)

    def terminate(self, native_session_id: str) -> None:
        self._inner.terminate(native_session_id)
        iid = self._instances.pop(native_session_id, None)
        if iid:
            try:
                from ..services import sandbox as sandbox_service
                sandbox_service.finish_instance_standalone(
                    iid, exit_reason="terminated")
            except Exception:  # noqa: BLE001
                pass
