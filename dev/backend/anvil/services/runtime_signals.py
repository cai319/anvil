"""Deterministic runtime component signal scanner (v1.50 P4)."""
from __future__ import annotations

import sqlite3

from . import runtime_proposals

FAILURE_CLASSES = {
    "platform_timeout": "runtime timeout",
    "spawn_error": "runtime spawn error",
    "context_overflow": "context overflow",
}


def scan(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    """Create/merge draft proposals for unhealthy runtimes and failed runs."""
    created: list[dict] = []
    for row in conn.execute(
            "SELECT id, display_name, status, last_probe_log FROM runtimes "
            "WHERE status IN ('missing','unhealthy') LIMIT ?", (limit,)).fetchall():
        result = runtime_proposals.create_or_update(
            conn, component_type="environment",
            component_key=f"runtime:{row['id']}",
            runtime_id=str(row["id"]),
            title=f"运行时 {row['display_name'] or row['id']} 不可用",
            summary=f"运行时探测状态为 {row['status']}",
            proposed_change_md=(
                "检查可执行文件路径/版本并重新探测；修复后重启该运行时。"),
            severity="major", origin="system_detector",
            evidence={"status": row["status"],
                      "probe_log": (row["last_probe_log"] or "")[:1000]},
            verification_md="重新 probe 运行时，状态应为 ready。",
            phase="runtime_probe")
        created.append(result)
    for row in conn.execute(
            "SELECT id, role_slug, failure_class, prompt FROM runs "
            "WHERE failure_class IN ('platform_timeout','spawn_error','context_overflow') "
            "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall():
        failure = str(row["failure_class"])
        result = runtime_proposals.create_or_update(
            conn, component_type="environment",
            component_key=f"run:{row['id']}",
            runtime_id=None,
            title=f"运行失败：{FAILURE_CLASSES.get(failure, failure)}",
            summary=f"run {row['id']} 以 {failure} 失败",
            proposed_change_md=(
                "检查运行时超时/启动/上下文窗口配置，并给出可复现 probe。"),
            severity="major", origin="system_detector",
            evidence={"run_id": row["id"], "role": row["role_slug"],
                      "failure_class": failure,
                      "prompt": (row["prompt"] or "")[:500]},
            verification_md="用同类 prompt 重跑，失败应消失。",
            phase="run_failure")
        created.append(result)
    return created
