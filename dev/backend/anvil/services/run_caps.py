"""运行上限（v1.62 #13）：diff numstat 记录与超限人工升级。

- 提交时在角色 worktree 上收集 staged / unstaged / committed / untracked 的
  ``git numstat``，按本次提交的 changed_paths 过滤后持久化到
  ``workflow_steps.diff_stats_json``。
- 每片 diff 行数上限与每任务总时长上限是运行参数（见 ``config``）；超限只升级
  人工（收件箱 mention + 评审包 risk），不把步骤判失败。
- fix 轮次上限仍由决策步骤的 ``max_rework`` 控制，本模块不改动它。
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..util import jdumps, jloads, now_iso

# 评审包 risks / 决策升级触发器中的运行上限信号。
ESCALATION_SIGNALS = frozenset({
    "diff_cap_exceeded",
    "task_duration_cap_exceeded",
})

# 这些切片阶段不产生实现 diff，不参与每片 diff 上限。
_NON_IMPLEMENTATION_SUFFIXES = frozenset({"scope", "verify", "verify2"})
_IGNORED_PREFIXES = (".git/", ".anvil/", ".claude/", ".gemini/")


def _git_lines(root: Path, args: list[str]) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20)
        return (result.stdout or "").splitlines() if result.returncode == 0 else []
    except Exception:  # noqa: BLE001 —— git 不可用时不阻断提交
        return []


def _clean_path(raw: str) -> str:
    path = str(raw or "").strip().strip('"')
    if " => " in path:
        # git numstat rename 形式：`old => new` 或 `dir/{old => new}.py`。
        head, tail = path.split(" => ", 1)
        if "{" in head and "}" in tail:
            prefix, suffix = head.split("{", 1)
            path = prefix + tail.split("}", 1)[0] + tail.split("}", 1)[1]
        else:
            path = tail
    return path[2:] if path.startswith("./") else path


def _ignored(path: str) -> bool:
    return (not path) or any(path.startswith(prefix) for prefix in _IGNORED_PREFIXES)


def _selected(path: str, provided: list[str]) -> bool:
    if not provided:
        return True
    return path in provided or any(
        path.startswith(item.rstrip("/") + "/") for item in provided)


def parse_numstat(lines: list[str]) -> dict:
    """解析 ``git diff --numstat`` 输出；二进制行（``-``）不计入行数。"""
    files: dict[str, dict] = {}
    for line in lines:
        parts = str(line).split("\t")
        if len(parts) < 3:
            continue
        added, deleted, raw_path = parts[0], parts[1], parts[2]
        path = _clean_path(raw_path)
        if _ignored(path) or added == "-" or deleted == "-":
            continue
        try:
            add_n, del_n = int(added), int(deleted)
        except ValueError:
            continue
        entry = files.setdefault(path, {"added": 0, "deleted": 0})
        entry["added"] += add_n
        entry["deleted"] += del_n
    return {
        "added": sum(item["added"] for item in files.values()),
        "deleted": sum(item["deleted"] for item in files.values()),
        "total": sum(item["added"] + item["deleted"] for item in files.values()),
        "files": files,
    }


def _count_lines(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\0" in data[:8000]:  # 二进制文件不计行数
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _resolve_root(conn: sqlite3.Connection, task_id: str, role: str | None,
                  run_id: str | None) -> tuple[Path | None, str]:
    row = None
    if run_id:
        row = conn.execute(
            "SELECT worktree_path FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT worktree_path FROM runs WHERE task_id=? AND role_slug=? "
            "ORDER BY started_at DESC LIMIT 1", (task_id, role or "")).fetchone()
    if row is not None and row["worktree_path"]:
        worktree = Path(row["worktree_path"])
        if worktree.exists():
            return worktree, "worktree"
    workspace = config.TASK_WORKSPACES_DIR / str(task_id)
    if workspace.exists():
        return workspace, "task_workspace"
    return None, "none"


def collect_numstat(conn: sqlite3.Connection, task_id: str, role: str | None,
                    changed_paths: list[str] | None = None, *,
                    run_id: str | None = None,
                    base_branch: str | None = None) -> dict:
    """收集本次提交的增删行数；无 git 环境时返回全 0，绝不阻断提交。"""
    provided = [_clean_path(p) for p in (changed_paths or []) if _clean_path(p)]
    root, source = _resolve_root(conn, task_id, role, run_id)
    files: dict[str, dict] = {}

    def add(lines: list[str]) -> None:
        parsed = parse_numstat(lines)
        for path, counts in parsed["files"].items():
            if not _selected(path, provided):
                continue
            entry = files.setdefault(path, {"added": 0, "deleted": 0})
            entry["added"] += counts["added"]
            entry["deleted"] += counts["deleted"]

    if root is not None and (root / ".git").exists():
        # 三段互不重叠：worktree↔index、index↔HEAD、merge-base↔HEAD。
        add(_git_lines(root, ["diff", "--numstat"]))
        add(_git_lines(root, ["diff", "--numstat", "--cached"]))
        if base_branch:
            if _git_lines(root, ["rev-parse", "--verify", "--quiet", base_branch]):
                add(_git_lines(
                    root, ["diff", "--numstat", f"{base_branch}...HEAD"]))
        # untracked 文件不在 diff 中，逐文件计新增行。
        for raw_path in _git_lines(
                root, ["ls-files", "--others", "--exclude-standard"]):
            path = _clean_path(raw_path)
            if _ignored(path) or not _selected(path, provided):
                continue
            count = _count_lines(root / path)
            if count:
                entry = files.setdefault(path, {"added": 0, "deleted": 0})
                entry["added"] += count

    return {
        "added": sum(item["added"] for item in files.values()),
        "deleted": sum(item["deleted"] for item in files.values()),
        "total": sum(item["added"] + item["deleted"] for item in files.values()),
        "files": files,
        "paths": sorted(files),
        "source": source,
        "recorded_at": now_iso(),
    }


def _is_implementation_step(step: dict) -> bool:
    if not str(step.get("slice_key") or "").strip():
        return False
    if step.get("kind") in ("decision", "plan_slices"):
        return False
    suffix = str(step.get("step_id") or "").rsplit(".", 1)[-1]
    return suffix not in _NON_IMPLEMENTATION_SUFFIXES


def _elapsed_seconds(started_at) -> float:
    if not started_at:
        return 0.0
    try:
        start = datetime.fromisoformat(str(started_at))
    except (TypeError, ValueError):
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds())


def _step_stats(conn: sqlite3.Connection, step: dict) -> dict:
    stats = step.get("diff_stats")
    if isinstance(stats, dict) and stats:
        return stats
    workflow_id = step.get("workflow_id")
    step_id = step.get("step_id")
    if not workflow_id or not step_id:
        return {}
    row = conn.execute(
        "SELECT diff_stats_json FROM workflow_steps "
        "WHERE workflow_id=? AND step_id=?", (workflow_id, step_id)).fetchone()
    if row is None or not row["diff_stats_json"]:
        return {}
    return jloads(row["diff_stats_json"], {}) or {}


def _slice_diff_total(conn: sqlite3.Connection, step: dict, own_total: int) -> int:
    """切片内实现步骤的最大 diff 行数；decision/verify 的评审包据此展示超限。"""
    slice_key = str(step.get("slice_key") or "").strip()
    workflow_id = step.get("workflow_id")
    if not slice_key or not workflow_id:
        return own_total
    best = own_total
    for row in conn.execute(
            "SELECT step_id, diff_stats_json FROM workflow_steps "
            "WHERE workflow_id=? AND slice_key=? AND diff_stats_json IS NOT NULL",
            (workflow_id, slice_key)).fetchall():
        if not _is_implementation_step({"slice_key": slice_key,
                                        "step_id": row["step_id"]}):
            continue
        stats = jloads(row["diff_stats_json"], {}) or {}
        best = max(best, int(stats.get("total") or 0))
    return best


def evaluate(conn: sqlite3.Connection, wf: dict, step: dict) -> dict:
    """计算本次步骤的运行参数与超限信号（不写库）。"""
    stats = _step_stats(conn, step)
    diff_cap = int(config.DIFF_CAP_LINES)
    duration_cap = float(config.TASK_DURATION_CAP_SECONDS)
    elapsed = _elapsed_seconds(wf.get("created_at"))
    diff_total = int(stats.get("total") or 0)
    if not _is_implementation_step(step):
        # decision / verify 步骤本身没有实现 diff，但同切片的超限仍须进入评审包。
        diff_total = _slice_diff_total(conn, step, diff_total)
    signals: list[str] = []
    slice_key = str(step.get("slice_key") or "").strip()
    if config.RUN_CAP_ENFORCE:
        # 每片 diff 上限只约束切片步骤；静态工作流没有切片语义。
        if slice_key and diff_cap > 0 and diff_total > diff_cap:
            signals.append("diff_cap_exceeded")
        if duration_cap > 0 and elapsed > duration_cap:
            signals.append("task_duration_cap_exceeded")
    return {
        "diff": {
            "added": int(stats.get("added") or 0),
            "deleted": int(stats.get("deleted") or 0),
            "total": diff_total,
        },
        "diff_cap": diff_cap,
        "duration_seconds": round(elapsed, 1),
        "duration_cap_seconds": duration_cap,
        "signals": signals,
        "checked_at": now_iso(),
    }


def evidence_entry(report: dict) -> dict:
    diff = report.get("diff") or {}
    label = (f"运行上限检查：diff +{diff.get('added', 0)}/-{diff.get('deleted', 0)} 行"
             f"（上限 {report.get('diff_cap', 0)}），任务时长 "
             f"{report.get('duration_seconds', 0)}s（上限 "
             f"{report.get('duration_cap_seconds', 0)}s）；"
             f"超限信号：{', '.join(report.get('signals') or []) or '无'}")
    return {
        "id": "engine:run_caps",
        "kind": "note",
        "ref": "step.diff_stats",
        "label": label[:240],
        "digest": "sha256:" + hashlib.sha256(
            jdumps(report).encode("utf-8")).hexdigest(),
        "captured_at": report.get("checked_at") or now_iso(),
        "fresh": True,
    }


def escalate(conn: sqlite3.Connection, wf: dict, step: dict, report: dict) -> bool:
    """超限时写入收件箱 mention + 事件；不改变步骤状态、不抛异常。"""
    from . import ledger

    signals = list(report.get("signals") or [])
    if not signals:
        return False
    task_row = conn.execute(
        "SELECT title FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
    task_title = (task_row["title"] if task_row else "") or "当前任务"
    diff = report.get("diff") or {}
    reasons: list[str] = []
    if "diff_cap_exceeded" in signals:
        reasons.append(
            f"本片本次 diff 增删 {diff.get('total', 0)} 行，"
            f"超过每片上限 {report.get('diff_cap', 0)} 行")
    if "task_duration_cap_exceeded" in signals:
        reasons.append(
            f"任务已运行 {report.get('duration_seconds', 0)}s，"
            f"超过总时长上限 {report.get('duration_cap_seconds', 0)}s")
    step_label = step.get("title") or step.get("step_id")
    message = (
        f"任务「{task_title}」的步骤「{step_label}」提交触发运行上限，需要人工确认："
        + "；".join(reasons)
        + "。步骤未失败、工作流继续；请确认是否接受本次提交或要求返工。")
    ledger.emit(
        conn, "step.run_cap_exceeded", task_id=wf["task_id"],
        workflow_id=wf["id"], execution_no=wf.get("execution_no"),
        step_id=step.get("step_id"), slice_key=step.get("slice_key"),
        run_id=step.get("run_id"), actor_type="system", actor="workflow",
        payload={"signals": signals, **report},
    )
    ledger.emit_activity(
        conn, task_id=wf["task_id"], role_slug="workflow", kind="mention",
        payload=jdumps({
            "target": "human",
            "message": message,
            "event": "run_cap_exceeded",
            "workflow_id": wf["id"],
            "step_id": step.get("step_id"),
            "slice_key": step.get("slice_key"),
            "signals": signals,
        }),
        now=now_iso(), workflow_id=wf["id"], step_id=step.get("step_id"),
        slice_key=step.get("slice_key"), execution_no=wf.get("execution_no"),
    )
    return True
