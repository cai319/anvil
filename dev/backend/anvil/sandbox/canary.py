"""运行时环境金丝雀。

在目标沙箱后端里**真实启动**运行时 CLI，并让它通过 Bash 工具产生一个带 nonce 的
文件。只有文件的副作用的出现才算通过：模型只复述 ``CANARY_OK`` 不算数。这样能抓住
v1.62.1 那类“CLI 退出码 0、Bash 工具其实没执行”的静默降级。

缓存键固定为 ``(runtime, cli_version, policy_digest, os, sandbox_backend)``；
任一变化都会重跑。结果为失败时同样缓存，避免每次调度都消耗模型调用；修复环境后
用 ``anvil sandbox self-check --force`` 主动刷新。

适配器接缝：新增运行时实现 :class:`CanaryAdapter` 并注册到 ``_ADAPTERS`` 即可。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Protocol

from .. import config
from ..util import decode_child_output, now_iso, scrub_agent_env
from . import env as sandbox_env
from .policy import resolve_for_run
from .ports import SandboxPolicy, SpawnRequest

CANARY_TIMEOUT_SECONDS = 120
MARKER_PREFIX = "anvil-canary-"
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


class CanaryAdapter(Protocol):
    """每种运行时的金丝雀命令与判定；保持无 DB、可替换以便测试。"""

    runtime_id: str

    def cli_version(self, executable: str, definition: dict) -> str | None:
        ...

    def command(self, executable: str, *, nonce: str) -> list[str]:
        ...

    def verify(self, probe_dir: Path, nonce: str, output: str,
               returncode: int | None) -> tuple[bool, str]:
        ...


class ClaudeCodeCanaryAdapter:
    """Claude Code：要求用 Bash 工具写 nonce 文件。"""

    runtime_id = "claude-code"

    def cli_version(self, executable: str, definition: dict) -> str | None:
        vp = (definition or {}).get("version_probe") or {}
        args = vp.get("args") or ["--version"]
        parse = vp.get("parse") or "regex:(\\d+\\.\\d+\\.\\d+)"
        try:
            out = subprocess.run(
                [executable, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
                env=scrub_agent_env(dict(os.environ), allow_run_token=False))
        except Exception:  # noqa: BLE001 —— 版本探测失败不影响金丝雀本身
            return None
        text = (out.stdout or "") + (out.stderr or "")
        if parse.startswith("regex:"):
            m = re.search(parse[len("regex:"):], text)
            if m:
                return m.group(1)
        m = _VERSION_RE.search(text)
        return m.group(1) if m else (text.strip().splitlines()[0][:80] if text.strip() else None)

    def command(self, executable: str, *, nonce: str) -> list[str]:
        prompt = (
            "Use the Bash tool to run exactly this shell command once: "
            f"`printf %s {nonce} > {MARKER_PREFIX}{nonce}`. "
            "Do not create the file by any other means and do not use other tools. "
            "After it succeeds, reply DONE."
        )
        # SpawnRequest.executable 已经承载 CLI 路径；args 只放参数，避免
        # 生成 `claude claude -p ...` 把路径当成 prompt。
        _ = executable
        return [
            "-p", prompt,
            "--allowedTools", "Bash",
            "--permission-mode", config.DEFAULT_PERMISSION_MODE,
            "--output-format", "stream-json",
            "--verbose",
        ]

    def verify(self, probe_dir: Path, nonce: str, output: str,
               returncode: int | None) -> tuple[bool, str]:
        marker = probe_dir / f"{MARKER_PREFIX}{nonce}"
        try:
            actual = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            actual = ""
        if actual == nonce:
            return True, "Bash tool executed inside the sandbox"
        tail = _tail(output)
        if returncode not in (None, 0):
            return False, f"runtime exited {returncode}; Bash tool did not produce the marker. {tail}".strip()
        return False, (
            "runtime exited 0 but the Bash tool did not run (silent degradation). "
            + tail).strip()


_ADAPTERS: dict[str, CanaryAdapter] = {
    ClaudeCodeCanaryAdapter.runtime_id: ClaudeCodeCanaryAdapter(),
}


def adapter_for(runtime_id: str) -> CanaryAdapter | None:
    return _ADAPTERS.get(str(runtime_id or ""))


def os_tag() -> str:
    return f"{platform.system()}-{platform.release()}"


def _tail(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    return text[-limit:]


def _safe(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "unknown"))[:80] or "unknown"


def key_parts(runtime_id: str, cli_version: str | None, policy_digest: str,
              sandbox_backend: str, *, os_name: str | None = None) -> dict[str, str]:
    return {
        "runtime_id": str(runtime_id),
        "cli_version": str(cli_version or "unknown"),
        "policy_digest": str(policy_digest or ""),
        "os": str(os_name or os_tag()),
        "sandbox_backend": str(sandbox_backend or "unknown"),
    }


def cache_key(parts: dict[str, str]) -> str:
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_result(row) -> dict:
    d = dict(row)
    d["ok"] = bool(d.get("ok"))
    d["skipped"] = bool(d.get("skipped"))
    d["cached"] = True
    return d


def load_cached(conn, parts: dict[str, str]) -> dict | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM runtime_canaries WHERE runtime_id=? AND cli_version=? "
            "AND policy_digest=? AND os=? AND sandbox_backend=?",
            (parts["runtime_id"], parts["cli_version"], parts["policy_digest"],
             parts["os"], parts["sandbox_backend"]),
        ).fetchone()
    except Exception:  # noqa: BLE001 —— 旧库没有表时按未缓存处理
        return None
    return _row_to_result(row) if row is not None else None


def load_latest(conn, runtime_id: str) -> dict | None:
    """最近一次金丝雀（不看版本键）；能力矩阵用于展示可能已过期的结果。"""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM runtime_canaries WHERE runtime_id=? "
            "ORDER BY checked_at DESC, rowid DESC LIMIT 1", (runtime_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return _row_to_result(row) if row is not None else None


def _store(conn, result: dict) -> None:
    if conn is None:
        return
    parts = result["key_parts"]
    uid = "canary_" + hashlib.sha256(
        json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    try:
        conn.execute(
            "INSERT INTO runtime_canaries "
            "(id, runtime_id, cli_version, policy_digest, os, sandbox_backend, ok, "
            " skipped, detail, output_tail, duration_ms, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(runtime_id, cli_version, policy_digest, os, sandbox_backend) "
            "DO UPDATE SET ok=excluded.ok, skipped=excluded.skipped, "
            "detail=excluded.detail, output_tail=excluded.output_tail, "
            "duration_ms=excluded.duration_ms, checked_at=excluded.checked_at",
            (uid, parts["runtime_id"], parts["cli_version"], parts["policy_digest"],
             parts["os"], parts["sandbox_backend"], 1 if result["ok"] else 0,
             1 if result.get("skipped") else 0, result.get("detail"),
             result.get("output_tail"), result.get("duration_ms"),
             result["checked_at"]),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 —— 缓存失败不改变金丝雀结论
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def runtime_info(conn, runtime_id: str) -> dict:
    """解析运行时可执行文件/版本；DB 行优先，回退内置/用户 YAML。"""
    from ..services import runtime as runtime_service

    definition = runtime_service.load_definitions().get(runtime_id) or {}
    row = runtime_service.get(conn, runtime_id) if conn is not None else None
    row = row or {}
    executable = (
        row.get("executable_path")
        or definition.get("executable")
        or definition.get("executable_path")
    )
    if not executable:
        # 新装/未 discover 的运行时：按 YAML 发现策略找一次，避免自检假阴性。
        strategies = (definition.get("discovery") or {}).get("strategies") or []
        try:
            executable = runtime_service._resolve_executable(strategies)
        except Exception:  # noqa: BLE001
            executable = None
    executable = runtime_service.effective_executable(executable) or executable or runtime_id
    version = row.get("detected_version")
    return {
        "runtime_id": runtime_id,
        "display_name": row.get("display_name") or definition.get("display_name") or runtime_id,
        "executable": executable,
        "detected_version": version,
        "definition": definition,
        "status": row.get("status"),
    }


def _execute(conn, *, runtime_id: str, info: dict, adapter: CanaryAdapter,
             backend, env_base: dict[str, str] | None,
             timeout: float) -> dict:
    probe_root = Path(config.SANDBOX_PROBES_DIR) / _safe(getattr(backend, "name", "host")) \
        / f"{_safe(runtime_id)}-{uuid.uuid4().hex[:10]}"
    instance_dir = probe_root / "inst"
    started = time.monotonic()
    nonce = uuid.uuid4().hex[:16]
    output = ""
    code: int | None = None
    try:
        probe_root.mkdir(parents=True, exist_ok=True)
        instance_dir.mkdir(parents=True, exist_ok=True)
        home, tmp = sandbox_env.prepare_home(
            str(probe_root), None, str(instance_dir))
        base = dict(os.environ) if env_base is None else dict(env_base)
        # 与角色进程同一契约：先剥人类认证变量，再重定向 HOME/TMP/缓存并剥宿主会话变量。
        role_env = sandbox_env.apply_standard_env(
            scrub_agent_env(base, allow_run_token=False), home=home, tmp=tmp)
        role_env["ANVIL_SANDBOX_CANARY"] = "1"
        args = adapter.command(info["executable"], nonce=nonce)
        req = SpawnRequest(
            executable=info["executable"], args=args, cwd=str(probe_root),
            env=role_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, instance_id="canary",
            instance_dir=str(instance_dir), workspace_path=str(probe_root),
            role_slug="canary",
        )
        sp = backend.spawn_stdio(req)
        proc = sp.popen
        try:
            raw, _ = proc.communicate(timeout=timeout)
            output = decode_child_output(raw or b"")
            code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                backend.kill(proc, graceful=False)
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": False,
                "detail": f"canary timed out after {int(timeout)}s",
                "output_tail": _tail(output),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        ok, detail = adapter.verify(probe_root, nonce, output, code)
        return {
            "ok": bool(ok),
            "detail": detail,
            "output_tail": _tail(output),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


def run_canary(conn, runtime_id: str | None = None, *,
               policy: SandboxPolicy | None = None,
               force: bool = False,
               backend=None,
               adapter: CanaryAdapter | None = None,
               env_base: dict[str, str] | None = None,
               os_name: str | None = None,
               timeout: float | None = None) -> dict:
    """运行/读取金丝雀；``force=True`` 忽略缓存。

    返回字段：runtime_id, ok, skipped, detail, output_tail, duration_ms,
    checked_at, cached, key_parts。
    """
    runtime_id = str(runtime_id or config.DEFAULT_RUNTIME)
    info = runtime_info(conn, runtime_id)
    chosen_adapter = adapter if adapter is not None else adapter_for(runtime_id)
    # 以真实 CLI 版本为准：DB 的 detected_version 可能停留在升级前，不能让它
    # 把“CLI 升级”藏在同一个缓存键后面。
    version = None
    if chosen_adapter is not None:
        try:
            version = chosen_adapter.cli_version(
                info["executable"], info["definition"])
        except Exception:  # noqa: BLE001 —— 版本探测失败回退 DB 值
            version = None
    version = version or info.get("detected_version") or "unknown"
    if backend is None:
        from ..services import sandbox as sandbox_service
        backend = sandbox_service.get_backend()
    backend_name = str(getattr(backend, "name", "unknown"))
    # 与 dispatch/advance 使用同一个策略解析入口，保证缓存键一致。
    policy_obj = policy or resolve_for_run({}, None)
    parts = key_parts(runtime_id, version, policy_obj.policy_digest, backend_name,
                      os_name=os_name)
    if not force:
        cached = load_cached(conn, parts)
        if cached is not None:
            cached["key_parts"] = parts
            return cached

    if config.SANDBOX_MODE == "host-unsafe":
        result = {
            "ok": False, "skipped": True,
            "detail": ("host-unsafe 是开发/排障模式，不能作为运行时可用性证据；"
                       "请用标准沙箱后端运行金丝雀。"),
            "output_tail": "", "duration_ms": 0,
        }
    elif not info.get("executable") or not Path(info["executable"]).exists() \
            and not shutil.which(info["executable"]):
        result = {
            "ok": False, "skipped": False,
            "detail": (f"未找到运行时 {runtime_id} 的可执行文件 "
                       f"({info.get('executable') or 'unset'})；先发现/注册运行时，"
                       "再运行 anvil sandbox self-check --force。"),
            "output_tail": "", "duration_ms": 0,
        }
    elif chosen_adapter is None:
        result = {
            "ok": False, "skipped": False,
            "detail": (f"运行时 {runtime_id} 还没有金丝雀适配器；"
                       "在 anvil/sandbox/canary.py 注册 CanaryAdapter 后才能派发。"),
            "output_tail": "", "duration_ms": 0,
        }
    else:
        try:
            result = _execute(
                conn, runtime_id=runtime_id, info=info, adapter=chosen_adapter,
                backend=backend, env_base=env_base,
                timeout=float(timeout or CANARY_TIMEOUT_SECONDS))
        except Exception as exc:  # noqa: BLE001 —— 启动器异常也要转成可见失败
            result = {
                "ok": False, "skipped": False,
                "detail": f"canary launch failed: {exc!r}",
                "output_tail": "", "duration_ms": 0,
            }
    result.update({
        "runtime_id": runtime_id,
        "cli_version": parts["cli_version"],
        "checked_at": now_iso(),
        "key_parts": parts,
        "cached": False,
    })
    result.setdefault("skipped", False)
    _store(conn, result)
    return result
