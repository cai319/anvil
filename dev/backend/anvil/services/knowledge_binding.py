"""任务 → 知识 repo 绑定（2026-10-24）。

执行仓库仍是单值 `tasks.repo`；这里维护多值 knowledge repos，仅用于 lessons/notes 检索。
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from typing import Any

from .. import config
from ..util import ConflictError, jdumps, jloads, now_iso
from . import knowledge as knowledge_service
from . import repo_catalog

_SAFE_REPO = re.compile(r"^[\w.-]{1,120}$", re.UNICODE)
_SOURCES = ("auto", "cindy", "human", "inherited", "agent")


def _enabled() -> bool:
    return bool(getattr(config, "KNOWLEDGE_REPOS_ENABLED", True))


def _mode() -> str:
    return str(getattr(config, "KNOWLEDGE_BINDING_MODE", "auto") or "auto").lower()


def _binding_row(row) -> dict:
    return {
        "repo": row["repo"],
        "source": row["source"],
        "status": row["status"],
        "confidence": float(row["confidence"] or 0),
        "score": float(row["score"] or 0),
        "matched_tags": jloads(row["matched_tags_json"] or "[]", []),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_bindings(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM task_knowledge_repos WHERE task_id=? "
        "ORDER BY CASE status WHEN 'applied' THEN 0 WHEN 'suggested' THEN 1 ELSE 2 END, "
        "score DESC, repo", (task_id,)
    ).fetchall()
    return [_binding_row(r) for r in rows]


def _state_for(task: dict, rows: list[dict]) -> str:
    if not _enabled() or _mode() == "off":
        return "off"
    if task.get("knowledge_repos_locked"):
        return "locked"
    if any(r["status"] == "applied" for r in rows):
        return "applied"
    if any(r["status"] == "suggested" for r in rows):
        return "suggested"
    if not (task.get("tags") or []):
        return "needs_cindy"
    return "fallback"


def attach_bindings(conn: sqlite3.Connection, tasks: list[dict]) -> list[dict]:
    if not tasks:
        return tasks
    ids = [t["id"] for t in tasks if t.get("id")]
    grouped: dict[str, list[dict]] = {tid: [] for tid in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        try:
            rows = conn.execute(
                "SELECT * FROM task_knowledge_repos WHERE task_id IN ("
                + ",".join("?" * len(chunk)) + ") ORDER BY score DESC, repo",
                chunk,
            ).fetchall()
            for row in rows:
                grouped.setdefault(row["task_id"], []).append(_binding_row(row))
        except sqlite3.Error:
            continue
    for task in tasks:
        rows = grouped.get(task.get("id"), [])
        task["knowledge_repos"] = rows
        task["knowledge_binding_locked"] = bool(task.get("knowledge_repos_locked"))
        task["knowledge_binding_state"] = _state_for(task, rows)
    return tasks


def _refs(values: Any) -> list[dict]:
    """接受 str / {repo,name,reason} / list，输出 [{repo,reason}]。"""
    if values is None:
        return []
    if isinstance(values, (str, dict)):
        values = [values]
    out: list[dict] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, str):
            name, reason = item, None
        elif isinstance(item, dict):
            name = item.get("repo") or item.get("name") or item.get("slug")
            reason = item.get("reason")
        else:
            continue
        name = repo_catalog.display_repo(str(name or ""))
        if not name or name == "shared" or not _SAFE_REPO.fullmatch(name):
            continue
        key = repo_catalog.repo_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"repo": name, "reason": str(reason).strip() if reason else None})
    return out[:max(1, int(getattr(config, "KNOWLEDGE_BINDING_MAX_REPOS", 3)))]


def _audit(conn: sqlite3.Connection, task_id: str, action: str, payload: dict) -> None:
    try:
        conn.execute(
            "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (None, task_id, action, jdumps(payload), now_iso()),
        )
    except sqlite3.Error:
        pass


def _replace_rows(conn: sqlite3.Connection, task_id: str, rows: list[dict],
                  sources: tuple[str, ...]) -> None:
    if not sources:
        return
    marks = ",".join("?" * len(sources))
    conn.execute(
        f"DELETE FROM task_knowledge_repos WHERE task_id=? AND source IN ({marks})",
        (task_id, *sources),
    )
    now = now_iso()
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO task_knowledge_repos "
            "(task_id, repo, source, status, confidence, score, matched_tags_json, reason, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, row["repo"], row.get("source", "auto"), row.get("status", "applied"),
             row.get("confidence", 0), row.get("score", 0),
             jdumps(row.get("matched_tags") or []), row.get("reason"), now, now),
        )


def set_manual(conn: sqlite3.Connection, task_id: str, values: Any, *,
               source: str = "human", lock: bool = True, commit: bool = True) -> list[dict]:
    refs = _refs(values)
    conn.execute("DELETE FROM task_knowledge_repos WHERE task_id=?", (task_id,))
    now = now_iso()
    for ref in refs:
        conn.execute(
            "INSERT INTO task_knowledge_repos "
            "(task_id, repo, source, status, confidence, score, matched_tags_json, reason, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, ref["repo"], source if source in _SOURCES else "human", "applied",
             1.0, 1.0, "[]", ref.get("reason") or "人工指定", now, now),
        )
    if lock:
        conn.execute("UPDATE tasks SET knowledge_repos_locked=1, updated_at=? WHERE id=?",
                     (now, task_id))
    _audit(conn, task_id, "task.knowledge_bind_manual",
           {"source": source, "repos": refs, "locked": lock})
    if commit:
        conn.commit()
    return list_bindings(conn, task_id)


def auto_bind(conn: sqlite3.Connection, task: dict, *, commit: bool = True) -> dict:
    """按标签/文本/亲和度自动绑定；失败不阻断任务创建。"""
    empty = {"applied": [], "suggested": [], "reason": None}
    if not _enabled() or _mode() == "off" or task.get("knowledge_repos_locked"):
        return empty
    task_id = task.get("id")
    if not task_id:
        return empty
    try:
        result = repo_catalog.suggest(
            conn,
            title=str(task.get("title") or ""),
            description=str(task.get("description") or ""),
            tags=task.get("tags") or [],
            exec_repo=task.get("repo"),
            workspace_id=task.get("workspace_id"),
            limit=max(5, int(getattr(config, "KNOWLEDGE_BINDING_MAX_REPOS", 3))),
            use_cache=False,
            exclude_task_id=task_id,
        )
        candidates = result.get("candidates") or []
        has_tags = bool(knowledge_service.normalize_tags(task.get("tags") or []))
        high = float(getattr(config, "KNOWLEDGE_BINDING_HIGH", 0.62))
        suggest_at = float(getattr(config, "KNOWLEDGE_BINDING_SUGGEST", 0.38))
        gap = float(getattr(config, "KNOWLEDGE_BINDING_GAP", 0.15))
        max_repos = max(1, int(getattr(config, "KNOWLEDGE_BINDING_MAX_REPOS", 3)))
        mode = _mode()
        top_score = float(candidates[0]["score"]) if candidates else 0.0
        second_score = float(candidates[1]["score"]) if len(candidates) > 1 else 0.0
        applied: list[dict] = []
        suggested: list[dict] = []
        for cand in candidates:
            score = float(cand.get("score") or 0)
            if score < suggest_at:
                continue
            components = cand.get("components") or {}
            evidence = bool(cand.get("matched_tags")) or float(components.get("lexical") or 0) >= 0.25 or (
                float(components.get("affinity") or 0) > 0 and float(components.get("prior") or 0) >= 0.30
            )
            if not evidence:
                continue
            row = {
                "repo": cand["repo"],
                "source": "auto",
                "confidence": cand.get("confidence", 0),
                "score": score,
                "matched_tags": cand.get("matched_tags") or [],
                "reason": cand.get("reason"),
            }
            if mode == "suggest":
                row["status"] = "suggested"
                suggested.append(row)
                continue
            if has_tags:
                if score >= high and (not applied or top_score - score <= gap):
                    row["status"] = "applied"
                    applied.append(row)
                else:
                    row["status"] = "suggested"
                    suggested.append(row)
            else:
                strong = (float(components.get("lexical") or 0) >= 0.65
                          and float(components.get("prior") or 0) >= 0.40)
                if strong and top_score - second_score >= 0.20 and score == top_score:
                    row["status"] = "applied"
                    applied.append(row)
                else:
                    row["status"] = "suggested"
                    suggested.append(row)
            if len(applied) >= max_repos and len(suggested) >= max_repos:
                break

        applied = applied[:max_repos]
        suggested = suggested[:max_repos]
        _replace_rows(conn, task_id, [], ("auto",))
        rows = applied + suggested
        _replace_rows(conn, task_id, rows, ("auto",))

        # 没有强匹配时，子任务继承父任务的 applied 绑定。
        if mode == "auto" and not applied and task.get("parent_id"):
            parent_rows = conn.execute(
                "SELECT repo, source, confidence FROM task_knowledge_repos "
                "WHERE task_id=? AND status='applied' ORDER BY score DESC LIMIT ?",
                (task["parent_id"], max_repos),
            ).fetchall()
            inherited = [{
                "repo": r["repo"], "source": "inherited", "status": "applied",
                "confidence": float(r["confidence"] or 0) * 0.9, "score": 0.4,
                "matched_tags": [], "reason": "继承自父任务",
            } for r in parent_rows]
            if inherited:
                _replace_rows(conn, task_id, inherited, ("inherited",))
                applied = inherited

        _audit(conn, task_id, "task.knowledge_bind", {
            "mode": mode,
            "applied": [r["repo"] for r in applied],
            "suggested": [r["repo"] for r in suggested],
            "top": [
                {"repo": c["repo"], "score": c["score"], "components": c.get("components")}
                for c in candidates[:5]
            ],
            "tag_keys": result.get("tag_keys") or [],
        })
        if commit:
            conn.commit()
        return {"applied": applied, "suggested": suggested,
                "reason": None if applied else ("needs_cindy" if not has_tags else "fallback")}
    except Exception:  # noqa: BLE001 —— 绑定失败不能阻断任务创建
        try:
            _audit(conn, task_id, "task.knowledge_bind_error", {"error": "auto bind failed"})
            if commit:
                conn.commit()
        except Exception:
            pass
        return empty


def apply_cindy_draft(conn: sqlite3.Connection, task_id: str, tree: dict, *,
                      commit: bool = True) -> dict:
    if not _enabled() or _mode() == "off" or not isinstance(tree, dict):
        return {"applied": [], "skipped": "disabled"}
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return {"applied": [], "skipped": "missing"}
    if task["knowledge_repos_locked"]:
        return {"applied": [], "skipped": "locked"}
    tags = knowledge_service.normalize_tags(tree.get("tags") or [])
    current_tags = jloads(task["tags_json"] or "[]", [])
    if tags and not current_tags:
        conn.execute("UPDATE tasks SET tags_json=?, updated_at=? WHERE id=?",
                     (jdumps(tags), now_iso(), task_id))
        _audit(conn, task_id, "task.tags_cindy", {"tags": tags})
    if "knowledge_repos" not in tree:
        if commit:
            conn.commit()
        return {"applied": [], "tags": tags, "skipped": "no_repo_selection"}
    refs = _refs(tree.get("knowledge_repos"))
    _replace_rows(conn, task_id, [], ("auto", "cindy", "inherited"))
    rows = [{
        "repo": ref["repo"], "source": "cindy", "status": "applied",
        "confidence": 1.0, "score": 1.0, "matched_tags": [],
        "reason": ref.get("reason") or "Cindy 选择",
    } for ref in refs]
    _replace_rows(conn, task_id, rows, ("cindy",))
    _audit(conn, task_id, "task.knowledge_bind_cindy", {
        "repos": refs, "tags": tags,
    })
    if commit:
        conn.commit()
    return {"applied": rows, "tags": tags}


def normalize_draft_tree(conn: sqlite3.Connection, tree: Any, *, strict: bool) -> Any:
    """归一化 tags/knowledge_repos；agent 草稿严格校验 repo 名。"""
    if not isinstance(tree, dict):
        return tree
    if "tags" in tree:
        tree["tags"] = knowledge_service.normalize_tags(tree.get("tags") or [])
    if "knowledge_repos" in tree:
        if strict:
            raw = tree.get("knowledge_repos") or []
            if isinstance(raw, (str, dict)):
                raw = [raw]
            for item in raw:
                name = item if isinstance(item, str) else (
                    item.get("repo") or item.get("name") or item.get("slug")
                    if isinstance(item, dict) else None)
                if repo_catalog.display_repo(str(name or "")) == "shared":
                    raise ConflictError(
                        "shared 是全局经验，不是可选知识仓库；请从 catalog 选择具体 repo")
        refs = _refs(tree.get("knowledge_repos"))
        if strict and refs:
            catalog = repo_catalog.build_catalog(conn)
            known = {p["key"] for p in catalog.get("repos") or []}
            names = {p["repo"]: p for p in catalog.get("repos") or []}
            for ref in refs:
                key = repo_catalog.repo_key(ref["repo"])
                if key not in known:
                    close = difflib.get_close_matches(ref["repo"], list(names), n=3)
                    hint = ("；相近仓库：" + "、".join(close)) if close else ""
                    raise ConflictError(
                        f"unknown knowledge repo: {ref['repo']}{hint}"
                    )
        tree["knowledge_repos"] = refs
    for key in ("children", "steps"):
        children = tree.get(key)
        if isinstance(children, list):
            for child in children:
                normalize_draft_tree(conn, child, strict=strict)
    loop = tree.get("loop")
    if isinstance(loop, dict):
        for value in loop.values():
            if isinstance(value, dict):
                normalize_draft_tree(conn, value, strict=strict)
    return tree


def retrieval_scope(conn: sqlite3.Connection, task: dict | None) -> dict:
    """返回 {mode, repos, repo}；repos=None 表示不过滤（legacy/any）。"""
    if not _enabled() or _mode() == "off":
        if not task or not task.get("id"):
            return {"mode": "any", "repos": None, "repo": None}
        repo = task.get("repo")
        if not repo:
            row = conn.execute("SELECT repo FROM tasks WHERE id=?", (task["id"],)).fetchone()
            repo = row["repo"] if row else None
        if repo:
            return {"mode": "legacy", "repos": [repo, "shared"], "repo": repo}
        return {"mode": "any", "repos": None, "repo": None}
    if not task or not task.get("id"):
        if str(getattr(config, "KNOWLEDGE_TASKLESS_SCOPE", "all")).lower() == "shared":
            return {"mode": "shared", "repos": ["shared"], "repo": None}
        return {"mode": "any", "repos": None, "repo": None}
    try:
        rows = conn.execute(
            "SELECT repo FROM task_knowledge_repos WHERE task_id=? AND status='applied' "
            "ORDER BY score DESC", (task["id"],)
        ).fetchall()
    except sqlite3.Error:
        rows = []
    bound = [r["repo"] for r in rows]
    exec_repo = task.get("repo")
    if not exec_repo:
        row = conn.execute("SELECT repo FROM tasks WHERE id=?", (task["id"],)).fetchone()
        exec_repo = row["repo"] if row else None
    if bound:
        repos = list(dict.fromkeys([*bound, *([exec_repo] if exec_repo else []), "shared"]))
        return {"mode": "multi", "repos": repos, "repo": None}
    if exec_repo:
        return {"mode": "legacy", "repos": [exec_repo, "shared"], "repo": exec_repo}
    return {"mode": "shared", "repos": ["shared"], "repo": None}
