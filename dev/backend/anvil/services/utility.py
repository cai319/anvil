"""Utility LLM 调用：复用 CLI 运行时的登录态，不直连模型 API。

用于「AI 生成画像/角色」等内务请求。运行时按以下优先级选择：
1. 调用方显式指定的 runtime_id（角色自身 / 创建者运行时）；
2. 运行时页设置的默认运行时（settings.default_runtime）；
3. ANVIL_DEFAULT_RUNTIME；
4. 第一个可用（优先 ready）的运行时。

这样用户不需要为后端单独配置 ANTHROPIC_*：Claude Code / Codex / Gemini 的
登录态与网关配置都由运行时自己管理。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile

from .. import config
from ..graph.render import render_template
from . import governance, runtime as runtime_service

DEFAULT_RUNTIME_KEY = "default_runtime"

# 不泄漏给子进程的编排器会话变量（与 daemon / platform 层保持一致）。
_SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
_SESSION_ENV_KEYS = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED"}


class UtilityError(RuntimeError):
    """运行时不可用或 utility 调用失败。"""


def get_default_runtime_id(conn: sqlite3.Connection) -> str:
    """有效默认运行时：运行时页设置 → ANVIL_DEFAULT_RUNTIME → 空串。"""
    configured = str(governance.get_setting(conn, DEFAULT_RUNTIME_KEY) or "").strip()
    return configured or config.DEFAULT_RUNTIME


def set_default_runtime_id(conn: sqlite3.Connection, runtime_id: str) -> None:
    governance.set_setting(conn, DEFAULT_RUNTIME_KEY, runtime_id)


def _definition(runtime_id: str) -> dict:
    return runtime_service.load_definitions().get(runtime_id) or {}


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    merged = {
        k: v for k, v in os.environ.items()
        if not k.startswith(_SESSION_ENV_PREFIXES) and k not in _SESSION_ENV_KEYS
    }
    merged.update(extra)
    return merged


def _resolve_runtime(conn: sqlite3.Connection, preferred_id: str | None = None) -> tuple[dict, dict]:
    """返回 (runtimes 表行, YAML definition)。"""
    candidates: list[str] = []
    for rid in (preferred_id, get_default_runtime_id(conn)):
        rid = str(rid or "").strip()
        if rid and rid not in candidates:
            candidates.append(rid)

    problems: list[str] = []
    for rid in candidates:
        rt = runtime_service.get(conn, rid)
        if rt is None:
            problems.append(f"{rid}: 未注册")
            continue
        if not rt.get("enabled"):
            problems.append(f"{rid}: 已禁用")
            continue
        if not rt.get("executable_path"):
            problems.append(f"{rid}: 未找到可执行文件")
            continue
        return rt, _definition(rid)

    # 显式/默认都不可用时，退到第一个可用运行时：先 ready，再 unverified。
    runtimes = runtime_service.list_all(conn)
    for want in ("ready", "unverified"):
        for rt in runtimes:
            if (rt.get("enabled") and rt.get("executable_path")
                    and rt.get("status") == want):
                return rt, _definition(rt["id"])

    detail = ("；".join(problems) + "；") if problems else ""
    raise UtilityError(
        f"没有可用的运行时（{detail}请在「运行时」页扫描本地运行时，"
        "确认至少一个状态为 ready）"
    )


def resolve_runtime_id(conn: sqlite3.Connection, preferred_id: str | None = None) -> str:
    """返回本次 utility 调用实际会使用的运行时 ID。"""
    rt, _ = _resolve_runtime(conn, preferred_id)
    return rt["id"]


def generate(conn: sqlite3.Connection, prompt: str, *, runtime_id: str | None = None,
             timeout: float | None = None) -> str:
    """用 CLI 运行时执行一次一次性 prompt，返回其 stdout。"""
    rt, definition = _resolve_runtime(conn, runtime_id)
    invocation = definition.get("invocation") or {}
    args_tpl = invocation.get("args") or []
    if not args_tpl:
        raise UtilityError(f"运行时 {rt['id']} 未定义 invocation.args，无法执行生成")

    timeout_s = float(timeout or config.UTILITY_TIMEOUT_SECONDS)
    with tempfile.TemporaryDirectory(prefix="anvil-utility-") as tmp:
        ctx = {
            "prompt": prompt,
            "workspace_path": tmp,
            "permission_mode": config.DEFAULT_PERMISSION_MODE,
            "task_id": "",
            "agent_slug": "utility",
            "session_token": "",
        }
        args = [render_template(str(a), ctx) for a in args_tpl]
        env = {
            k: render_template(str(v), ctx)
            for k, v in (invocation.get("env") or {}).items()
        }
        # 运行时 YAML 可能把 endpoint 写死为 7317；utility 调用以当前服务为准。
        env["ANVIL_ENDPOINT"] = f"http://{config.HOST}:{config.PORT}"
        cwd = render_template(str(invocation.get("cwd") or tmp), ctx)
        if not os.path.isdir(cwd):
            cwd = tmp
        try:
            proc = subprocess.run(
                [rt["executable_path"], *args],
                cwd=cwd,
                env=_clean_env(env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise UtilityError(f"运行时 {rt['id']} 生成超时（{timeout_s:.0f}s）") from exc
        except Exception as exc:  # noqa: BLE001
            raise UtilityError(f"无法启动运行时 {rt['id']}: {exc}") from exc

    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        log = ((proc.stdout or "") + (proc.stderr or "")).strip()[:500]
        raise UtilityError(f"运行时 {rt['id']} 退出码 {proc.returncode}: {log or '无输出'}")
    if not text:
        raise UtilityError(f"运行时 {rt['id']} 没有返回内容")
    return text
