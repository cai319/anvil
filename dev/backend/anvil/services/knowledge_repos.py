"""主题知识库注册表：跨任务的 topic repo（不是 task workspace）。

- 共享/主题知识库：`knowledge_repos` + `data/knowledge-repos/<repo_id>/`；
- 任务级知识库：`task_knowledge_repos.scope='local'`，文件在 task workspace 内；
- 任务级 repo 不进入全局注册表，只有晋升（Phase 2）后才登记。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import knowledge, repo_catalog


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    return d


def repo_key(name: str | None) -> str:
    return repo_catalog.repo_key(name)


def display_repo(name: str | None) -> str:
    return repo_catalog.display_repo(name)


def get(conn: sqlite3.Connection, name_or_id: str) -> dict | None:
    key = repo_key(name_or_id)
    row = conn.execute(
        "SELECT * FROM knowledge_repos WHERE id=?", (key,)).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT repo_id FROM knowledge_repo_aliases WHERE alias_key=?",
            (key,)).fetchone()
        if alias is not None:
            row = conn.execute(
                "SELECT * FROM knowledge_repos WHERE id=?",
                (alias["repo_id"],)).fetchone()
    return _row(row)


def list_registry(conn: sqlite3.Connection, *, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM knowledge_repos"
    params: list = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY display_name"
    return [_row(r) or {} for r in conn.execute(sql, params).fetchall()]


def list_with_stats(conn: sqlite3.Connection, *,
                    status: str | None = "active") -> list[dict]:
    """注册表 + 卡片状态计数 + 别名 + 绑定任务数（主题知识库管理页）。"""
    repos = list_registry(conn, status=status)
    for repo in repos:
        counts = {
            str(row["status"]): int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM repo_knowledge "
                "WHERE repo_id=? GROUP BY status", (repo["id"],)).fetchall()
        }
        aliases = [
            row["alias"] for row in conn.execute(
                "SELECT alias FROM knowledge_repo_aliases WHERE repo_id=? "
                "ORDER BY alias", (repo["id"],)).fetchall()
        ]
        bound = conn.execute(
            "SELECT COUNT(DISTINCT task_id) FROM task_knowledge_repos "
            "WHERE repo_id=? AND scope='shared' AND status='applied'",
            (repo["id"],)).fetchone()
        repo["counts"] = counts
        repo["card_count"] = sum(counts.values())
        repo["pending"] = counts.get("proposed", 0)
        repo["aliases"] = aliases
        repo["task_count"] = int(bound[0]) if bound is not None else 0
    return repos


def ensure(conn: sqlite3.Connection, name: str, *,
           description: str = "", created_by: str = "human",
           source_task_id: str | None = None,
           auto_created: bool = False, needs_review: bool = False,
           commit: bool = True) -> dict:
    """幂等登记一个主题知识库（不创建任务级 repo）。"""
    display = display_repo(name)
    key = repo_key(display)
    if not key or key == "shared":
        raise ConflictError("'shared' is reserved and cannot be a topic repo")
    existing = get(conn, key)
    now = now_iso()
    if existing is not None:
        if description and not existing.get("description"):
            conn.execute(
                "UPDATE knowledge_repos SET description=?, updated_at=? WHERE id=?",
                (description, now, existing["id"]))
            existing = get(conn, key)
        if commit:
            conn.commit()
        return existing or {}
    conn.execute(
        "INSERT INTO knowledge_repos "
        "(id, display_name, description, status, locked, auto_created, needs_review, "
        " created_by, source_task_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (key, display, description, "active", 0, int(auto_created),
         int(needs_review or auto_created), created_by, source_task_id, now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_repo_aliases "
        "(alias_key, repo_id, alias, created_at) VALUES (?,?,?,?)",
        (key, key, display, now),
    )
    if commit:
        conn.commit()
    return get(conn, key) or {}


def list_task_bindings(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM task_knowledge_repos WHERE task_id=? "
        "ORDER BY is_primary DESC, CASE status WHEN 'applied' THEN 0 WHEN 'suggested' THEN 1 ELSE 2 END, "
        "score DESC, repo", (task_id,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["tags"] = jloads(d.get("matched_tags_json") or "[]", [])
        out.append(d)
    return out


def primary_binding(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM task_knowledge_repos WHERE task_id=? "
        "AND status='applied' ORDER BY is_primary DESC, score DESC, repo LIMIT 1",
        (task_id,)).fetchone()
    return dict(row) if row is not None else None


def _set_primary(conn: sqlite3.Connection, task_id: str, repo: str, *,
                 scope: str, path: str | None = None,
                 mode: str = "bound") -> None:
    conn.execute(
        "UPDATE task_knowledge_repos SET is_primary=0 WHERE task_id=?", (task_id,))
    key = repo_key(repo)
    existing = conn.execute(
        "SELECT 1 FROM task_knowledge_repos WHERE task_id=? AND repo=?",
        (task_id, repo)).fetchone()
    now = now_iso()
    if existing is None:
        conn.execute(
            "INSERT INTO task_knowledge_repos "
            "(task_id, repo, source, status, confidence, score, matched_tags_json, "
            " reason, scope, repo_id, path, is_primary, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, repo, "auto", "applied", 1.0, 1.0, "[]",
             f"primary:{mode}", scope, key, path, 1, now, now),
        )
    else:
        conn.execute(
            "UPDATE task_knowledge_repos SET is_primary=1, scope=?, repo_id=?, "
            "path=COALESCE(path, ?), updated_at=? WHERE task_id=? AND repo=?",
            (scope, key, path, now, task_id, repo))
    conn.execute(
        "UPDATE tasks SET repo=?, knowledge_mode=?, updated_at=? WHERE id=?",
        (repo, mode, now, task_id))


def bind_shared(conn: sqlite3.Connection, task_id: str, name: str, *,
                score: float = 0.0, source: str = "auto",
                primary: bool = False, commit: bool = True) -> dict:
    """绑定一个共享主题知识库（只读引用）。"""
    repo = ensure(conn, name, created_by=source, commit=False)
    display = repo.get("display_name") or display_repo(name)
    key = repo.get("id") or repo_key(display)
    now = now_iso()
    conn.execute(
        "INSERT INTO task_knowledge_repos "
        "(task_id, repo, source, status, confidence, score, matched_tags_json, "
        " reason, scope, repo_id, path, is_primary, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id, repo) DO UPDATE SET "
        "status='applied', scope='shared', repo_id=excluded.repo_id, "
        "score=MAX(task_knowledge_repos.score, excluded.score), updated_at=excluded.updated_at",
        (task_id, display, source, "applied", min(1.0, max(0.0, score)), score,
         "[]", None, "shared", key, None, 0, now, now),
    )
    if primary:
        _set_primary(conn, task_id, display, scope="shared", mode="bound")
    if commit:
        conn.commit()
    return get(conn, key) or {}


def _name_from_task(task: dict, *, preferred: str | None = None) -> str:
    if preferred:
        return preferred
    tags = [str(t) for t in (task.get("tags") or [])]
    stop = {"backend", "frontend", "bug", "task", "misc", "other", "fix"}
    for tag in tags:
        if tag.casefold() not in stop and len(tag) >= 2:
            return tag
    title = str(task.get("title") or "").strip()
    tokens = [t for t in re.split(r"[^A-Za-z0-9一-鿿]+", title) if len(t) >= 2]
    if tokens:
        return "-".join(tokens[:4])[:64]
    return f"topic-{str(task.get('id') or new_id())[:8]}"


def create_local_for_task(conn: sqlite3.Connection, task: dict, *,
                          workspace_path: str, preferred_name: str | None = None,
                          commit: bool = True) -> dict:
    """在 task workspace 内创建任务级知识库（不登记全局注册表）。"""
    name = _name_from_task(task, preferred=preferred_name)
    name = re.sub(r"[^A-Za-z0-9_.\-一-鿿]+", "-", name).strip("-") or "topic"
    key = repo_key(name)
    root = Path(config.TASK_WORKSPACES_DIR) / knowledge.task_path_segment(
        str(task.get("id") or ""))
    if workspace_path:
        root = Path(workspace_path)
    local = root / "knowledge" / key
    for sub in ("cards", "drafts"):
        (local / sub).mkdir(parents=True, exist_ok=True)
    now = now_iso()
    conn.execute(
        "INSERT INTO task_knowledge_repos "
        "(task_id, repo, source, status, confidence, score, matched_tags_json, "
        " reason, scope, repo_id, path, is_primary, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id, repo) DO UPDATE SET "
        "scope='local', repo_id=excluded.repo_id, path=excluded.path, "
        "is_primary=1, status='applied', updated_at=excluded.updated_at",
        (task["id"], name, "local", "applied", 0.5, 0.0, "[]",
         "local:relevance_below_threshold", "local", key, str(local), 1, now, now),
    )
    conn.execute(
        "UPDATE task_knowledge_repos SET is_primary=0 WHERE task_id=? AND repo<>?",
        (task["id"], name))
    conn.execute(
        "UPDATE tasks SET repo=?, knowledge_mode='local', updated_at=? WHERE id=?",
        (name, now, task["id"]))
    if commit:
        conn.commit()
    return {"repo": name, "repo_id": key, "path": str(local), "scope": "local"}


def local_knowledge_path(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        "SELECT path FROM task_knowledge_repos WHERE task_id=? AND scope='local' "
        "AND status='applied' ORDER BY is_primary DESC, score DESC LIMIT 1",
        (task_id,)).fetchone()
    return str(row["path"]) if row is not None and row["path"] else ""


def rename(conn: sqlite3.Connection, name: str, new_name: str, *,
           actor: str = "human", commit: bool = True) -> dict:
    repo = get(conn, name)
    if repo is None:
        raise LookupError(name)
    if repo.get("locked"):
        raise ConflictError("repo is locked")
    new_display = display_repo(new_name)
    new_key = repo_key(new_display)
    if not new_key or new_key == "shared":
        raise ConflictError("invalid repo name")
    if new_key != repo["id"] and get(conn, new_key) is not None:
        raise ConflictError(f"repo already exists: {new_display}")
    now = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_repo_aliases "
        "(alias_key, repo_id, alias, created_at) VALUES (?,?,?,?)",
        (repo["id"], repo["id"], repo["display_name"], now))
    conn.execute(
        "UPDATE knowledge_repos SET display_name=?, updated_at=? WHERE id=?",
        (new_display, now, repo["id"]))
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_repo_aliases "
        "(alias_key, repo_id, alias, created_at) VALUES (?,?,?,?)",
        (new_key, repo["id"], new_display, now))
    # 同步文本列（repo_id 保持不变）。
    for table in ("notes", "lessons", "workspace_knowledge",
                  "task_knowledge_repos", "repo_knowledge"):
        try:
            conn.execute(
                f"UPDATE {table} SET repo=? WHERE repo_id=? OR repo=?",
                (new_display, repo["id"], repo["display_name"]))
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute(
            "UPDATE tasks SET repo=? WHERE repo=?",
            (new_display, repo["display_name"]))
    except sqlite3.OperationalError:
        pass
    if commit:
        conn.commit()
    return get(conn, repo["id"]) or {}


def merge(conn: sqlite3.Connection, source: str, target: str, *,
          actor: str = "human", commit: bool = True) -> dict:
    src = get(conn, source)
    dst = get(conn, target)
    if src is None or dst is None:
        raise LookupError(source if src is None else target)
    if src["id"] == dst["id"]:
        return dst
    if src.get("locked"):
        raise ConflictError("source repo is locked")
    now = now_iso()
    # 移动卡片/绑定/文本引用；repo_id 统一到目标。
    for table in ("repo_knowledge", "notes", "lessons", "workspace_knowledge"):
        try:
            conn.execute(
                f"UPDATE {table} SET repo_id=?, repo=? WHERE repo_id=? OR repo=?",
                (dst["id"], dst["display_name"], src["id"], src["display_name"]))
        except sqlite3.OperationalError:
            pass
    rows = conn.execute(
        "SELECT task_id, repo FROM task_knowledge_repos "
        "WHERE repo_id=? OR repo=?", (src["id"], src["display_name"])).fetchall()
    for row in rows:
        existing = conn.execute(
            "SELECT 1 FROM task_knowledge_repos WHERE task_id=? AND repo=?",
            (row["task_id"], dst["display_name"])).fetchone()
        if existing is None:
            conn.execute(
                "UPDATE task_knowledge_repos SET repo=?, repo_id=? "
                "WHERE task_id=? AND repo=?",
                (dst["display_name"], dst["id"], row["task_id"], row["repo"]))
        else:
            conn.execute(
                "DELETE FROM task_knowledge_repos WHERE task_id=? AND repo=?",
                (row["task_id"], row["repo"]))
    conn.execute(
        "UPDATE tasks SET repo=? WHERE repo=?",
        (dst["display_name"], src["display_name"]))
    conn.execute(
        "UPDATE knowledge_repos SET status='merged', merged_into=?, updated_at=? "
        "WHERE id=?", (dst["id"], now, src["id"]))
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_repo_aliases "
        "(alias_key, repo_id, alias, created_at) VALUES (?,?,?,?)",
        (src["id"], dst["id"], src["display_name"], now))
    if commit:
        conn.commit()
    return get(conn, dst["id"]) or {}


def lock(conn: sqlite3.Connection, name: str, locked: bool = True, *,
         commit: bool = True) -> dict:
    repo = get(conn, name)
    if repo is None:
        raise LookupError(name)
    conn.execute(
        "UPDATE knowledge_repos SET locked=?, updated_at=? WHERE id=?",
        (int(bool(locked)), now_iso(), repo["id"]))
    if commit:
        conn.commit()
    return get(conn, repo["id"]) or {}
