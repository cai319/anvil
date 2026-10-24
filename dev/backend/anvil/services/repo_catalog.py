"""派生式知识仓库目录（2026-10-24）。

不维护独立的仓库注册表：repo profile 从 workspaces / lessons / notes / tasks 和
REPOS_DIR 自动聚合。notes 是角色隔离数据，只贡献聚合 tags/计数，**绝不**进入
catalog 描述或 prompt 正文。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from ..util import now_iso
from . import knowledge as knowledge_service

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def repo_key(name: str | None) -> str:
    """repo 的 canonical key：basename、去 .git、casefold、空白/下划线→-。"""
    text = unicodedata.normalize("NFKC", str(name or "")).strip().replace("\\", "/")
    text = text.rstrip("/").split("/")[-1]
    if text.endswith(".git"):
        text = text[:-4]
    return re.sub(r"[\s_]+", "-", text.casefold()).strip("-")


def display_repo(name: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).strip().replace("\\", "/")
    text = text.rstrip("/").split("/")[-1]
    if text.endswith(".git"):
        text = text[:-4]
    return text or "shared"


def tag_key(tag: str) -> str:
    return re.sub(r"[\s_/]+", "-", unicodedata.normalize("NFKC", str(tag)).strip().casefold())


def invalidate_catalog() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_key(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        path = row[2] if not isinstance(row, sqlite3.Row) else row["file"]
        if path and path != ":memory:":
            return str(Path(path).resolve())
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _profile(profiles: dict[str, dict], name: str | None) -> dict | None:
    key = repo_key(name)
    if not key or key == "shared":
        return None
    entry = profiles.setdefault(key, {
        "key": key,
        "names": Counter(),
        "descriptions": [],
        "sources": set(),
        "tag_freq": Counter(),
        "lesson_samples": [],
        "task_samples": [],
        "counts": {
            "workspaces": 0,
            "lessons_approved": 0,
            "lessons_pending": 0,
            "notes_role": 0,
            "tasks_exec": 0,
            "filesystem": False,
        },
        "last_activity_at": None,
    })
    entry["names"][display_repo(name)] += 1
    return entry


def _touch(entry: dict, at: str | None) -> None:
    if not at:
        return
    current = entry.get("last_activity_at")
    if not current or str(at) > str(current):
        entry["last_activity_at"] = at


def _collect_workspaces(conn: sqlite3.Connection, profiles: dict[str, dict]) -> None:
    try:
        rows = conn.execute(
            "SELECT name, repo, description, updated_at FROM workspaces "
            "WHERE repo IS NOT NULL AND repo <> ''"
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        entry = _profile(profiles, row["repo"])
        if entry is None:
            continue
        entry["sources"].add("workspace")
        entry["counts"]["workspaces"] += 1
        desc = str(row["description"] or "").strip()
        if desc:
            entry["descriptions"].append(desc)
        _touch(entry, row["updated_at"])


def _collect_lessons(conn: sqlite3.Connection, profiles: dict[str, dict]) -> None:
    try:
        rows = conn.execute(
            "SELECT repo, status, tags_json, content, COALESCE(approved_at, created_at) AS at "
            "FROM lessons WHERE repo IS NOT NULL AND repo <> '' "
            "ORDER BY at DESC LIMIT 5000"
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        entry = _profile(profiles, row["repo"])
        if entry is None:
            continue
        entry["sources"].add("lessons")
        if row["status"] == "approved":
            entry["counts"]["lessons_approved"] += 1
            try:
                tags = json.loads(row["tags_json"] or "[]")
            except (TypeError, ValueError):
                tags = []
            for tag in tags if isinstance(tags, list) else []:
                entry["tag_freq"][tag_key(str(tag))] += 2
            if len(entry["lesson_samples"]) < 3:
                entry["lesson_samples"].append({
                    "content": str(row["content"] or "")[:240],
                    "tags": [str(t) for t in (tags or [])][:8],
                })
        elif row["status"] == "pending":
            entry["counts"]["lessons_pending"] += 1
        _touch(entry, row["at"])


def _collect_notes(conn: sqlite3.Connection, profiles: dict[str, dict]) -> None:
    """只取 role-scope 的 tags/计数；不读正文，避免跨角色泄漏。"""
    try:
        rows = conn.execute(
            "SELECT repo, tags_json, COALESCE(updated_at, created_at) AS at "
            "FROM notes WHERE repo IS NOT NULL AND repo <> '' "
            "AND scope='role' AND kind <> 'task_snapshot' "
            "ORDER BY at DESC LIMIT 5000"
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        entry = _profile(profiles, row["repo"])
        if entry is None:
            continue
        entry["sources"].add("notes")
        entry["counts"]["notes_role"] += 1
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, ValueError):
            tags = []
        for tag in tags if isinstance(tags, list) else []:
            entry["tag_freq"][tag_key(str(tag))] += 1
        _touch(entry, row["at"])


def _collect_tasks(conn: sqlite3.Connection, profiles: dict[str, dict],
                   exclude_task_id: str | None) -> None:
    try:
        rows = conn.execute(
            "SELECT id, repo, tags_json, title, COALESCE(updated_at, created_at) AS at "
            "FROM tasks WHERE repo IS NOT NULL AND repo <> '' "
            "ORDER BY at DESC LIMIT 5000"
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        if exclude_task_id and row["id"] == exclude_task_id:
            continue
        entry = _profile(profiles, row["repo"])
        if entry is None:
            continue
        entry["sources"].add("tasks")
        entry["counts"]["tasks_exec"] += 1
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, ValueError):
            tags = []
        for tag in tags if isinstance(tags, list) else []:
            entry["tag_freq"][tag_key(str(tag))] += 1
        if len(entry["task_samples"]) < 3:
            entry["task_samples"].append({"title": str(row["title"] or "")[:160]})
        _touch(entry, row["at"])


def _collect_filesystem(profiles: dict[str, dict]) -> None:
    root = Path(config.REPOS_DIR)
    if not root.exists():
        return
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    base = root.resolve()
    for path in entries:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent != base or not resolved.is_dir() or path.name.startswith("."):
            continue
        entry = _profile(profiles, path.name)
        if entry is None:
            continue
        entry["sources"].add("filesystem")
        entry["counts"]["filesystem"] = True


def _finalize(entry: dict) -> dict:
    names = entry["names"]
    name = names.most_common(1)[0][0] if names else entry["key"]
    descriptions = [d for d in entry["descriptions"] if d]
    description = max(descriptions, key=len) if descriptions else ""
    tag_freq = [
        {"tag": tag, "count": count, "weight": round(1.0 + math.log1p(count), 3)}
        for tag, count in entry["tag_freq"].most_common()
    ]
    counts = entry["counts"]
    description_derived = not description
    if description_derived:
        parts = []
        if counts["lessons_approved"]:
            parts.append(f"经验 {counts['lessons_approved']}")
        if counts["notes_role"]:
            parts.append(f"笔记 {counts['notes_role']}")
        if tag_freq:
            parts.append("标签：" + "、".join(x["tag"] for x in tag_freq[:6]))
        description = "；".join(parts)
    return {
        "repo": name,
        "key": entry["key"],
        "aliases": sorted(set(names)),
        "description": description,
        "description_derived": description_derived,
        "selectable": True,
        "sources": sorted(entry["sources"]),
        "counts": dict(counts),
        "tag_freq": tag_freq,
        "top_tags": [x["tag"] for x in tag_freq[:10]],
        "samples": {
            "lessons": entry["lesson_samples"],
            "tasks": entry["task_samples"],
        },
        "last_activity_at": entry["last_activity_at"],
    }


def build_catalog(conn: sqlite3.Connection, *, include_filesystem: bool = True,
                  exclude_task_id: str | None = None,
                  use_cache: bool = True) -> dict:
    if not getattr(config, "KNOWLEDGE_REPOS_ENABLED", True):
        return {"repos": [], "total": 0, "truncated": False,
                "generated_at": now_iso(), "enabled": False}
    key = _cache_key(conn)
    ttl = float(getattr(config, "KNOWLEDGE_CATALOG_TTL", 30))
    if use_cache and key and exclude_task_id is None:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] <= ttl:
                return hit[1]

    profiles: dict[str, dict] = {}
    _collect_workspaces(conn, profiles)
    _collect_lessons(conn, profiles)
    _collect_notes(conn, profiles)
    _collect_tasks(conn, profiles, exclude_task_id)
    if include_filesystem:
        _collect_filesystem(profiles)

    repos = [_finalize(entry) for entry in profiles.values()]
    repos.sort(key=lambda p: p["repo"].casefold())
    cap = int(getattr(config, "KNOWLEDGE_CATALOG_MAX_REPOS", 200))
    truncated = len(repos) > cap
    repos = repos[:cap]
    catalog = {
        "repos": repos,
        "total": len(repos),
        "truncated": truncated,
        "generated_at": now_iso(),
        "enabled": True,
    }
    if use_cache and key and exclude_task_id is None:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), catalog)
    return catalog


def get_profile(conn: sqlite3.Connection, repo: str, *, use_cache: bool = True) -> dict | None:
    key = repo_key(repo)
    for item in build_catalog(conn, use_cache=use_cache)["repos"]:
        if item["key"] == key:
            return item
    return None


def _idf(n: int, df: int) -> float:
    return math.log(1.0 + n / (df + 1.0))


def _recency(value: str | None) -> float:
    parsed = _parse_time(value)
    if parsed is None:
        return 0.0
    days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    return math.exp(-days / 180.0)


def _profile_doc(profile: dict) -> dict[str, float]:
    doc: dict[str, float] = {}
    def add(text: str, weight: float) -> None:
        for token in knowledge_service.tokenize_for_match(text):
            doc[token] = max(doc.get(token, 0.0), weight)
    add(profile.get("repo") or "", 2.0)
    add(profile.get("description") or "", 2.0)
    add(" ".join(profile.get("top_tags") or []), 1.5)
    for sample in (profile.get("samples") or {}).get("lessons", []):
        add(str(sample.get("content") or ""), 1.0)
    for sample in (profile.get("samples") or {}).get("tasks", []):
        add(str(sample.get("title") or ""), 1.0)
    return doc


def suggest(conn: sqlite3.Connection, *, title: str, description: str = "",
            tags: list[str] | None = None, exec_repo: str | None = None,
            workspace_id: str | None = None, limit: int = 3,
            use_cache: bool = True, exclude_task_id: str | None = None) -> dict:
    """按标签+词法+活跃度+亲和度给候选 repo 打分；不写库。"""
    catalog = build_catalog(
        conn, use_cache=use_cache, exclude_task_id=exclude_task_id)
    profiles = [p for p in catalog["repos"] if p.get("selectable")]
    if not profiles:
        return {"candidates": [], "tag_keys": [], "generated_at": catalog["generated_at"]}

    normalized_tags = knowledge_service.normalize_tags(tags or [])
    tag_keys = list(dict.fromkeys(tag_key(t) for t in normalized_tags))
    query_tokens = knowledge_service.tokenize_for_match(f"{title}\n{description}")
    query: dict[str, float] = {}
    for token in query_tokens:
        query[token] = max(query.get(token, 0.0), 1.0)
    for key in tag_keys:
        query[key] = max(query.get(key, 0.0), 1.5)

    docs = {p["key"]: _profile_doc(p) for p in profiles}
    profile_tag_keys = {
        p["key"]: {tag_key(x["tag"]) for x in p.get("tag_freq") or []}
        for p in profiles
    }
    tag_df = Counter()
    token_df = Counter()
    for p in profiles:
        for key in profile_tag_keys[p["key"]]:
            tag_df[key] += 1
        for token in docs[p["key"]]:
            token_df[token] += 1
    n_profiles = len(profiles)

    workspace_repo = None
    if workspace_id:
        try:
            row = conn.execute("SELECT repo FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            workspace_repo = repo_key(row["repo"] if row else None)
        except sqlite3.Error:
            workspace_repo = None
    exec_key = repo_key(exec_repo) if exec_repo else ""

    candidates = []
    for profile in profiles:
        key = profile["key"]
        p_tags = profile_tag_keys[key]
        matched = [t for t in tag_keys if t in p_tags]
        coverage = len(matched) / max(1, len(tag_keys))
        total_idf = sum(_idf(n_profiles, tag_df[t]) for t in tag_keys)
        matched_idf = sum(_idf(n_profiles, tag_df[t]) for t in matched)
        specificity = matched_idf / total_idf if total_idf > 0 else 0.0
        s_tag = 0.6 * coverage + 0.4 * specificity

        doc = docs[key]
        denom = sum(w * _idf(n_profiles, token_df[t]) for t, w in query.items())
        numer = sum(w * doc.get(t, 0.0) * _idf(n_profiles, token_df[t])
                    for t, w in query.items() if t in doc)
        s_lex = min(1.0, numer / denom) if denom > 0 else 0.0

        counts = profile.get("counts") or {}
        activity_raw = (3 * int(counts.get("lessons_approved") or 0)
                        + 2 * int(counts.get("notes_role") or 0)
                        + int(counts.get("tasks_exec") or 0))
        activity = min(1.0, math.log1p(max(0, activity_raw)) / math.log1p(50))
        s_prior = 0.6 * activity + 0.4 * _recency(profile.get("last_activity_at"))

        affinity = 1.0 if (exec_key and key == exec_key) else (
            0.8 if (workspace_repo and key == workspace_repo) else 0.0)
        score = 0.48 * s_tag + 0.27 * s_lex + 0.10 * s_prior + 0.15 * affinity
        reasons = []
        if matched:
            reasons.append("命中标签 " + "、".join(matched))
        if s_lex >= 0.25:
            reasons.append(f"文本相似 {s_lex:.2f}")
        if counts.get("lessons_approved"):
            reasons.append(f"{counts['lessons_approved']} 条已批准经验")
        if affinity:
            reasons.append("执行/频道仓库匹配")
        candidates.append({
            "repo": profile["repo"],
            "key": key,
            "score": round(score, 4),
            "confidence": _confidence(score),
            "matched_tags": matched,
            "reason": "；".join(reasons) or "弱匹配",
            "components": {
                "tag": round(s_tag, 4),
                "lexical": round(s_lex, 4),
                "prior": round(s_prior, 4),
                "affinity": affinity,
            },
            "counts": counts,
            "top_tags": profile.get("top_tags") or [],
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return {
        "candidates": candidates[:max(1, min(limit, 20))],
        "tag_keys": tag_keys,
        "generated_at": catalog["generated_at"],
    }


def _confidence(score: float) -> float:
    high = float(getattr(config, "KNOWLEDGE_BINDING_HIGH", 0.62))
    suggest_at = float(getattr(config, "KNOWLEDGE_BINDING_SUGGEST", 0.38))
    if score >= high:
        return round(0.90 + 0.09 * (score - high) / max(1e-6, 1.0 - high), 3)
    if score >= suggest_at:
        return round(0.50 + 0.40 * (score - suggest_at) / max(1e-6, high - suggest_at), 3)
    return round(score, 3)


def render_catalog_for_prompt(catalog: dict, *, max_repos: int = 25,
                              max_chars: int = 2600) -> str:
    lines: list[str] = []
    used = 0
    for profile in catalog.get("repos") or []:
        tags = "、".join(profile.get("top_tags") or [])[:120]
        desc = re.sub(r"\s+", " ", str(profile.get("description") or ""))[:180]
        counts = profile.get("counts") or {}
        if profile.get("description_derived"):
            line = f"- {profile['repo']}：{desc or '（暂无描述）'}"
        else:
            line = (f"- {profile['repo']}：{desc or '（暂无描述）'}；"
                    f"经验 {counts.get('lessons_approved', 0)} / "
                    f"笔记 {counts.get('notes_role', 0)}"
                    + (f"；标签：{tags}" if tags else ""))
        if used + len(line) + 1 > max_chars or len(lines) >= max_repos:
            break
        lines.append(line)
        used += len(line) + 1
    if len(catalog.get("repos") or []) > len(lines):
        lines.append(f"（另有 {len(catalog['repos']) - len(lines)} 个仓库未列出）")
    return "\n".join(lines)


def binding_stats(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute(
            "SELECT status, source, COUNT(*) AS n FROM task_knowledge_repos GROUP BY status, source"
        ).fetchall()
    except sqlite3.Error:
        return {"total": 0, "by_status": {}, "by_source": {}}
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + int(row["n"])
        by_source[row["source"]] = by_source.get(row["source"], 0) + int(row["n"])
    return {"total": sum(by_status.values()), "by_status": by_status, "by_source": by_source}
