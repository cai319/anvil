"""上下文清单：记录一次 CIC / 投影实际注入了哪些块（只存元数据，不存正文）。

用途：
- 回答“这条内容为什么进入任务 B 的上下文”；
- 隔离回归：任务 B 的 manifest 不应出现任务 A 的 task-scoped source id；
- 预算调优：哪些块被截断/丢弃、原因是什么。
"""
from __future__ import annotations

import sqlite3

from .. import config
from ..util import jdumps, jloads, now_iso


_ALLOWED = {
    "name", "layer", "kind", "source_id", "source_ids", "path", "scope",
    "tags", "score", "chars", "injected", "reason", "priority", "order",
    "truncated",
}


def _clean_block(block: dict) -> dict:
    """只保留白名单元数据；绝不写入正文/路径内容之外的敏感文本。"""
    out = {k: v for k, v in (block or {}).items() if k in _ALLOWED}
    if "source_ids" in out and not isinstance(out["source_ids"], list):
        out["source_ids"] = [str(out["source_ids"])]
    return out


def record(conn: sqlite3.Connection, *, run_id: str | None, role_slug: str,
           task_id: str | None, kind: str, budget_chars: int,
           total_chars: int, blocks: list[dict],
           omitted: list[dict] | None = None,
           slice_key: str | None = None) -> int | None:
    if not config.CONTEXT_MANIFEST_ENABLED:
        return None
    cur = conn.execute(
        "INSERT INTO context_manifests "
        "(run_id, role_slug, task_id, slice_key, kind, budget_chars, total_chars, "
        " blocks_json, omitted_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, role_slug, task_id, slice_key, kind, int(budget_chars),
         int(total_chars),
         jdumps([_clean_block(b) for b in (blocks or [])]),
         jdumps([_clean_block(b) for b in (omitted or [])]),
         now_iso()),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def list_for_run(conn: sqlite3.Connection, run_id: str, *,
                 kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM context_manifests WHERE run_id=?"
    params: list = [run_id]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["blocks"] = jloads(d.pop("blocks_json", "[]"), [])
        d["omitted"] = jloads(d.pop("omitted_json", "[]"), [])
        out.append(d)
    return out


def latest_for_task(conn: sqlite3.Connection, task_id: str, *,
                    role_slug: str | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM context_manifests WHERE task_id=?"
    params: list = [task_id]
    if role_slug:
        sql += " AND role_slug=?"
        params.append(role_slug)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(200, int(limit or 20))))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["blocks"] = jloads(d.pop("blocks_json", "[]"), [])
        d["omitted"] = jloads(d.pop("omitted_json", "[]"), [])
        out.append(d)
    return out
