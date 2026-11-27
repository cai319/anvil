"""进程树与磁盘采样（M1）。

macOS/Linux 用 ``ps``；Windows 由 Job Object 硬限资源，supervisor 跳过采样。
所有函数失败返回 None，绝不让监控线程崩溃。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def sample_tree(root_pid: int) -> dict | None:
    """返回 {rss_mib, pids}；非 POSIX 或采样失败返回 None。"""
    if sys.platform.startswith("win"):
        return None
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss="],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5).stdout
    except Exception:  # noqa: BLE001
        return None
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid, ppid, kb = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rss[pid] = kb
        children.setdefault(ppid, []).append(pid)
    if root_pid not in rss:
        return None
    seen: set[int] = set()
    stack = [root_pid]
    total_kb = 0
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total_kb += rss.get(pid, 0)
        stack.extend(children.get(pid, []))
    return {"rss_mib": int(total_kb / 1024), "pids": len(seen)}


def disk_mib(path: str | None) -> int | None:
    """目录占用（MiB）；超时/失败返回 None。"""
    if not path or sys.platform.startswith("win"):
        return None
    if not Path(path).exists():
        return None
    try:
        out = subprocess.run(
            ["du", "-sk", path], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8).stdout
        first = out.splitlines()[0].split() if out else []
        if not first:
            return None
        return int(int(first[0]) / 1024)
    except Exception:  # noqa: BLE001
        return None
