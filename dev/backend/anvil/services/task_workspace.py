"""任务工作空间（Task Workspace）：任务的数据存储，不是知识库。

设计（Phase 0）：
- 每个任务一个目录 `data/task-workspaces/<task-id>/`；
- `work/` 放代码/数据/产物，`knowledge/` 放任务自己的知识库文件，`drafts/` 放待批草稿；
- `CLAUDE.md` / `.claude/` 由运行时投影写入（延迟投影）；
- 任务工作空间只回答“数据放哪”，不定义知识语义；知识库见 `knowledge_repos.py` /
  `task_knowledge.py`。

该服务只做目录/元数据生命周期，不负责解析知识、审批或注入。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..util import new_id, now_iso
from . import knowledge


GLOBAL_TASK_KEY = "__global__"


def _root() -> Path:
    # 测试会 monkeypatch config.DATA_DIR；此时模块级 TASK_WORKSPACES_DIR 仍是旧值，
    # 因此优先用显式环境变量，否则从当前 DATA_DIR 派生，保证测试隔离。
    env = os.environ.get("ANVIL_TASK_WORKSPACES_DIR")
    if env:
        return Path(env).expanduser()
    return Path(config.DATA_DIR) / "task-workspaces"


def path_for(task_id: str) -> Path:
    """任务工作空间目录；`__global__` 用于 reminder/直聊等无任务运行。"""
    key = str(task_id or GLOBAL_TASK_KEY)
    return _root() / knowledge.task_path_segment(key)


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def get(conn: sqlite3.Connection, task_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM task_workspaces WHERE task_id=?", (str(task_id),)
    ).fetchone())


def _write_meta(path: Path, meta: dict) -> None:
    anvil = path / ".anvil"
    anvil.mkdir(parents=True, exist_ok=True)
    target = anvil / "workspace.json"
    tmp = anvil / f".workspace.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def ensure(conn: sqlite3.Connection, task_id: str, *,
           kind: str | None = None, source_repo: str | None = None,
           base_branch: str | None = None,
           create_dir: bool = True,
           commit: bool = True) -> dict:
    """幂等创建/读取任务工作空间目录与 DB 行。

    - `kind=git_worktree` 表示 `work/` 会从 `source_repo` 初始化；否则是普通数据目录。
    - `source_repo` 是数据来源（Git 仓库名），不是知识 repo。
    - `create_dir=False` 只登记 DB 行，目录在首次真正执行时再创建（避免创建任务就落盘）。
    - 目录已存在时只补齐子目录/元数据，不删除任何文件。
    """
    task_key = str(task_id or GLOBAL_TASK_KEY)
    if not config.TASK_WORKSPACE_ENABLED:
        # 回退模式：不创建新目录，调用方应回退角色 home。
        existing = get(conn, task_key)
        if existing:
            return existing
        return {
            "id": "", "task_id": task_key,
            "path": "", "kind": "disabled", "source_repo": source_repo,
            "base_branch": base_branch, "status": "disabled",
            "created_at": now_iso(), "updated_at": now_iso(),
        }

    now = now_iso()
    row = get(conn, task_key)
    path = Path(row["path"]) if row and row.get("path") else path_for(task_key)
    if create_dir:
        path.mkdir(parents=True, exist_ok=True)
        for sub in ("work", "knowledge", "drafts", ".anvil"):
            (path / sub).mkdir(parents=True, exist_ok=True)

    resolved_kind = kind or (
        "git_worktree" if source_repo else (row.get("kind") if row else "data")
    ) or "data"
    if row is None:
        wid = new_id()
        conn.execute(
            "INSERT INTO task_workspaces "
            "(id, task_id, path, kind, source_repo, base_branch, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (wid, task_key, str(path), resolved_kind, source_repo, base_branch,
             "active", now, now),
        )
        row = get(conn, task_key)
    else:
        # 只补空字段，不覆盖人工/迁移已确定的值。
        updates = []
        params: list = []
        if source_repo and not row.get("source_repo"):
            updates.append("source_repo=?")
            params.append(source_repo)
        if base_branch and not row.get("base_branch"):
            updates.append("base_branch=?")
            params.append(base_branch)
        if kind and row.get("kind") in (None, "", "data"):
            updates.append("kind=?")
            params.append(kind)
        if updates:
            params.extend([now, task_key])
            conn.execute(
                f"UPDATE task_workspaces SET {', '.join(updates)}, updated_at=? "
                "WHERE task_id=?", params,
            )
            row = get(conn, task_key)

    if create_dir:
        _write_meta(path, {
            "id": row["id"] if row else "",
            "task_id": task_key,
            "kind": resolved_kind,
            "source_repo": source_repo or (row or {}).get("source_repo"),
            "base_branch": base_branch or (row or {}).get("base_branch"),
            "status": (row or {}).get("status", "active"),
            "path": str(path),
            "created_at": (row or {}).get("created_at", now),
        })
    if commit:
        conn.commit()
    return get(conn, task_key) or {}


def resolve(conn: sqlite3.Connection, task_id: str, *,
            create: bool = True) -> str:
    """返回任务工作空间路径；不存在时按需创建。`TASK_WORKSPACE=0` 返回空串。"""
    key = str(task_id or GLOBAL_TASK_KEY)
    row = get(conn, key)
    if row and row.get("status") in ("deleted",):
        return ""
    if row and row.get("path") and Path(row["path"]).is_dir():
        return str(row["path"])
    if not create:
        return ""
    ensured = ensure(conn, key, create_dir=True)
    return str(ensured.get("path") or "")


def archive(conn: sqlite3.Connection, task_id: str, *,
            commit: bool = True) -> bool:
    if not get(conn, str(task_id)):
        return False
    conn.execute(
        "UPDATE task_workspaces SET status='archived', updated_at=? WHERE task_id=?",
        (now_iso(), str(task_id)),
    )
    if commit:
        conn.commit()
    return True


def mark_deleted(conn: sqlite3.Connection, task_id: str, *,
                 commit: bool = True) -> bool:
    if not get(conn, str(task_id)):
        return False
    conn.execute(
        "UPDATE task_workspaces SET status='deleted', updated_at=? WHERE task_id=?",
        (now_iso(), str(task_id)),
    )
    if commit:
        conn.commit()
    return True


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def cleanup_stale(conn: sqlite3.Connection, *,
                  retention_days: int | None = None,
                  dry_run: bool = False) -> dict:
    """清理过期且无活跃上下文槽的 workspace 目录。

    - 只处理 archived/deleted；active 永不清理；
    - 有 `role_task_sessions.status='busy'` 的任务跳过；
    - 删除目录但保留 DB 行（status=deleted），便于审计。
    """
    days = int(retention_days if retention_days is not None
               else config.TASK_WORKSPACE_RETENTION_DAYS)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, days))
    rows = conn.execute(
        "SELECT w.* FROM task_workspaces w "
        "WHERE w.status IN ('archived','deleted')"
    ).fetchall()
    removed: list[str] = []
    skipped: list[str] = []
    for row in rows:
        task_id = str(row["task_id"])
        updated = _parse_dt(row["updated_at"])
        if updated is None or updated > cutoff:
            skipped.append(task_id)
            continue
        busy = conn.execute(
            "SELECT 1 FROM role_task_sessions WHERE task_key=? AND status='busy' LIMIT 1",
            (task_id,),
        ).fetchone()
        if busy:
            skipped.append(task_id)
            continue
        path = Path(str(row["path"] or ""))
        if not path:
            skipped.append(task_id)
            continue
        removed.append(task_id)
        if dry_run:
            continue
        try:
            if path.exists() and path.is_dir():
                shutil.rmtree(path)
        except OSError:
            # 删除失败保留行，下一轮再试。
            removed.pop()
            skipped.append(task_id)
            continue
        conn.execute(
            "UPDATE task_workspaces SET status='deleted', updated_at=? WHERE task_id=?",
            (now_iso(), task_id),
        )
    if not dry_run:
        conn.commit()
    return {"removed": removed, "skipped": skipped, "dry_run": bool(dry_run)}
