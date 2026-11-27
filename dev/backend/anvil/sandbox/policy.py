"""沙箱策略解析（M1：standard 档）。

M1 只解析 standard：资源上限 + HOME/写范围由后端执行；strict 档在 M2/M3 加入。
策略随 run 快照冻结，``policy_digest`` 变化会在 turn 边界触发进程重建。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .. import config
from ..util import jloads
from .ports import TIER_STANDARD, SandboxPolicy


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _as_int(value: Any, fallback: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


def resolve_standard_policy(*, role_sandbox: dict | None = None,
                            task_sandbox: dict | None = None) -> SandboxPolicy:
    """默认限额 + 角色/任务覆盖（任务优先）；返回冻结用策略。"""
    resources = {
        "timeout_seconds": int(config.SANDBOX_STANDARD_TIMEOUT),
        "rss_mib": int(config.SANDBOX_STANDARD_RSS_MIB),
        "pids": int(config.SANDBOX_STANDARD_PIDS),
        "disk_mib": int(config.SANDBOX_STANDARD_DISK_MIB),
    }
    profile = "standard"
    network = "host"
    source = "default"
    read_allow: list[str] = []
    for overrides, src in ((role_sandbox, "role"), (task_sandbox, "task")):
        if not isinstance(overrides, dict):
            continue
        profile = str(overrides.get("profile") or profile)
        network = str(overrides.get("network") or network)
        limits = overrides.get("resources")
        if isinstance(limits, dict):
            for key in resources:
                if key in limits:
                    resources[key] = _as_int(limits[key], resources[key])
        raw_allow = overrides.get("read_allow")
        if isinstance(raw_allow, list):
            read_allow = [str(p) for p in raw_allow if str(p).strip()]
        source = src
    resources["read_allow"] = read_allow
    payload = {
        "tier": TIER_STANDARD,
        "profile": profile,
        "network": network,
        "resources": resources,
        "source": source,
    }
    return SandboxPolicy(
        tier=TIER_STANDARD,
        profile=profile,
        backend=None,
        network=network,
        resources=resources,
        policy_digest=hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest(),
        source=source,
    )


def task_overrides(task: dict | None) -> dict:
    if not isinstance(task, dict):
        return {}
    raw = task.get("sandbox_json")
    if isinstance(raw, dict):
        return raw
    return jloads(str(raw or "{}"), {}) or {}


def resolve_for_run(effective: dict | None, task: dict | None = None) -> SandboxPolicy:
    """从冻结快照或实时 effective 解析；快照里已有 ``sandbox_policy`` 时直接复用。"""
    effective = effective or {}
    frozen = effective.get("sandbox_policy")
    if isinstance(frozen, dict) and frozen.get("tier") == TIER_STANDARD:
        return SandboxPolicy(
            tier=TIER_STANDARD,
            profile=str(frozen.get("profile") or "standard"),
            backend=frozen.get("backend"),
            network=str(frozen.get("network") or "host"),
            resources=dict(frozen.get("resources") or {}),
            policy_digest=str(frozen.get("policy_digest") or ""),
            source=str(frozen.get("source") or "snapshot"),
        )
    projection = effective.get("projection") or {}
    role_sandbox = projection.get("sandbox") if isinstance(projection, dict) else None
    return resolve_standard_policy(
        role_sandbox=role_sandbox if isinstance(role_sandbox, dict) else None,
        task_sandbox=task_overrides(task),
    )


def as_dict(policy: SandboxPolicy) -> dict:
    return asdict(policy)
