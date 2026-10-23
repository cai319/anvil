"""角色知识库：scope/kind 元数据、检索、排序、上下文块与 reindex。

设计要点（见 plan）：
- task scope 只对同一 task_id 可见；role scope 可跨任务复用；legacy 保持 unclassified。
- 文件是真相；DB notes 是索引，notes_fts 由触发器同步。
- 检索基线是 FTS5 BM25 + 元数据重排；接口保留 dense/RRF 扩展点。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..util import jdumps, jloads, now_iso

NOTE_KINDS = {
    "task_snapshot", "vulnerability_pattern", "failure_scenario",
    "module_knowledge", "decision", "note",
}
NOTE_SCOPES = {"task", "role", "unclassified"}
ROLE_KINDS = ("vulnerability_pattern", "failure_scenario", "module_knowledge",
              "decision", "note")

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./\-一-鿿]+$")
_TEXT_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[㐀-鿿]+")
_WS_RE = re.compile(r"\s+")


def task_path_segment(task_id: str) -> str:
    """任务目录段：普通 id 保持原样；特殊字符替换后加短 hash 防碰撞。"""
    raw = str(task_id or "")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:64] or "task"
    if safe != raw:
        safe += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return safe


def _clamp01(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _norm_tags(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，\s]+", values)
    out: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        tag = str(item).strip()
        if not tag or len(tag) > 64:
            continue
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            out.append(tag)
        if len(out) >= 32:
            break
    return out


def normalize_note_path(path: str) -> str:
    """校验并规范化 notes 相对路径；拒绝绝对路径/穿越/空路径。"""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError("invalid note path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    # 兼容 `anvil note write notes/foo.md` 的直觉写法：path 始终相对 notes/，
    # 避免生成 notes/notes/ 的重复目录。
    if parts and parts[0] == "notes":
        parts = parts[1:]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("note path must stay inside the role notes directory")
    clean = "/".join(parts)
    if len(clean) > 200 or not _SAFE_PATH_RE.match(clean):
        raise ValueError("invalid characters in note path")
    return clean


_NOTE_META_RE = re.compile(r"<!--\s*anvil:\s*(\{.*?\})\s*-->\s*\n?", re.S)


def parse_note_file(raw: str) -> tuple[dict, str]:
    """解析文件头 `<!-- anvil: {...} -->` 元数据，并剥离所有元数据块。

    文件是真相：正文可能由多次 append 累积；元数据只保留一份（最新写入时重写）。
    旧文件里的多个元数据块也会一并清理。
    """
    meta: dict = {}
    match = _NOTE_META_RE.search(raw or "")
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                meta = parsed
        except json.JSONDecodeError:
            meta = {}
    body = _NOTE_META_RE.sub("", raw or "").strip()
    return meta, body


def _task_segment_map(conn: sqlite3.Connection) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for row in conn.execute("SELECT id FROM tasks").fetchall():
        tid = str(row["id"])
        seg = task_path_segment(tid)
        if seg in result and result[seg] != tid:
            result[seg] = None
        else:
            result[seg] = tid
    return result


def infer_metadata(conn: sqlite3.Connection, role: str, path: str, content: str,
                   *, explicit: dict | None = None,
                   task_id_hint: str | None = None,
                   default_scope: str | None = None,
                   for_reindex: bool = False) -> dict:
    """从显式参数/路径约定推断 notes 元数据。

    普通 notes/ 默认 role scope；knowledge/<task>/ 默认 task snapshot。目录名无法
    对应到现存任务时保持 unclassified，避免把快照误注入到其它任务。
    """
    explicit = dict(explicit or {})
    clean_path = normalize_note_path(path)
    kind = str(explicit.get("kind") or "").strip() or None
    scope = str(explicit.get("scope") or "").strip() or None
    task_id = str(explicit.get("task_id") or task_id_hint or "").strip() or None
    source_task = str(explicit.get("source_task") or "").strip() or None
    tags = _norm_tags(explicit.get("tags"))
    repo = str(explicit.get("repo") or "").strip() or None
    module = str(explicit.get("module") or "").strip() or None
    confidence = _clamp01(explicit.get("confidence"))
    segs = clean_path.split("/")
    task_seg = None
    if clean_path.startswith("knowledge/") and len(segs) >= 2:
        task_seg = segs[1]
        mapped_tid = task_id
        if not task_id:
            mapped_tid = _task_segment_map(conn).get(task_seg)
            if mapped_tid:
                task_id = mapped_tid
            else:
                # 目录名是旧快照唯一可用的任务标识：保留在 source_task，
                # scope 仍为 unclassified，不会自动注入其它任务。
                task_id = None
                if task_seg:
                    source_task = source_task or task_seg
        if not kind:
            kind = "task_snapshot"
        if not scope:
            scope = "task" if mapped_tid else "unclassified"
    elif not kind:
        prefix = (segs[0] if segs else "").lower()
        if prefix in ("vulnerabilities", "vulns", "patterns", "vulnerability-patterns"):
            kind = "vulnerability_pattern"
        elif prefix in ("failures", "failure-scenarios", "failure_scenarios"):
            kind = "failure_scenario"
        elif prefix in ("modules", "module", "module-knowledge"):
            kind = "module_knowledge"
        elif prefix == "decisions":
            kind = "decision"
        else:
            kind = "note"
    if not scope:
        if default_scope:
            scope = default_scope
        else:
            # 普通 notes/ 属于角色可复用知识；knowledge/ 目录无法解析任务时
            # 已在上面的分支里保持 unclassified（不自动注入）。
            scope = "task" if task_id else "role"
    if scope not in NOTE_SCOPES:
        scope = "unclassified"
    if kind not in NOTE_KINDS:
        kind = "note"
    if kind == "task_snapshot":
        scope = "task" if task_id else "unclassified"
    if scope == "role" and task_id:
        source_task = source_task or task_id
        task_id = None
    if scope == "task" and not task_id:
        scope = "unclassified"
    if scope == "unclassified" and task_id:
        source_task = source_task or task_id
        task_id = None
    return {
        "kind": kind, "scope": scope, "task_id": task_id,
        "source_task": source_task, "tags": tags, "repo": repo,
        "module": module, "confidence": confidence,
        "updated_at": str(explicit.get("updated_at") or now_iso()),
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _tokens(text: str) -> list[str]:
    terms: list[str] = []
    for raw in _TEXT_TOKEN_RE.findall(str(text or "")):
        # 长中文串按 2 字滑窗补充，避免整段中文只被当成一个 token。
        if re.fullmatch(r"[㐀-鿿]+", raw) and len(raw) > 6:
            terms.extend(raw[i:i + 3] for i in range(0, len(raw) - 2, 3))
        else:
            terms.append(raw)
    return list(dict.fromkeys(t for t in terms if t))[:32]


def _fts_query(terms: list[str]) -> str:
    usable = [t for t in terms if len(t) >= 3]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in usable)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scope_clause(scope: str, task_id: str | None) -> tuple[str, list]:
    if scope == "task":
        return ("n.scope='task' AND n.task_id=?", [task_id])
    if scope == "role":
        return ("n.scope='role'", [])
    return ("(n.scope='role' OR (n.scope='task' AND n.task_id=?))", [task_id])


def _base_where(role: str, task_id: str | None, scope: str,
                kinds: tuple[str, ...] | None, tags: tuple[str, ...] | None,
                repo: str | None, module: str | None,
                include_snapshots: bool, include_unclassified: bool,
                repo_scope: str = "any", task_repo: str | None = None):
    clauses = ["n.role_slug=?"]
    params: list = [role]
    if task_id is None:
        clauses.append("n.scope<>'task'")
        if scope == "task":
            return None, []
    else:
        if scope == "task":
            clauses.append("n.scope='task' AND n.task_id=?")
            params.append(task_id)
        elif scope == "role":
            clauses.append("n.scope='role'")
        else:
            clauses.append("(n.scope='role' OR (n.scope='task' AND n.task_id=?))")
            params.append(task_id)
    if include_unclassified:
        # 允许显式搜索 legacy/未分类笔记；任务隔离仍然有效。
        if task_id is None:
            clauses.append("(n.scope IN ('role','unclassified'))")
        else:
            clauses.append(
                "(n.scope IN ('role','unclassified') "
                "OR (n.scope='task' AND n.task_id=?))")
            params.append(task_id)
    else:
        clauses.append("n.scope<>'unclassified'")
    if not include_snapshots:
        clauses.append("n.kind<>'task_snapshot'")
    if kinds:
        clauses.append("n.kind IN (" + ",".join("?" * len(kinds)) + ")")
        params.extend(kinds)
    if repo:
        clauses.append("n.repo=?")
        params.append(repo)
    elif repo_scope == "same_or_shared" and task_repo:
        clauses.append("(n.repo IS NULL OR n.repo=? OR n.repo='shared')")
        params.append(task_repo)
    if module:
        clauses.append("n.module=?")
        params.append(module)
    for tag in tags or ():
        clauses.append("n.tags_json LIKE ? ESCAPE '\\'")
        params.append(f'%"{_like_escape(tag)}"%')
    return " AND ".join(clauses), params


def _row_hit(row, snippet: str, score: float, why: list[str],
             snippet_chars: int) -> dict:
    tags = jloads(row["tags_json"], []) if "tags_json" in row.keys() else []
    return {
        "id": row["id"], "path": row["path"], "kind": row["kind"],
        "scope": row["scope"], "task_id": row["task_id"],
        "source_task": row["source_task"], "tags": tags,
        "repo": row["repo"], "module": row["module"],
        "confidence": row["confidence"], "created_at": row["created_at"],
        "updated_at": row["updated_at"] or row["created_at"],
        "content": row["content"],
        "score": round(float(score), 6),
        "bm25": row["bm25"] if "bm25" in row.keys() else None,
        "snippet": snippet[:snippet_chars],
        "provenance": {
            "id": row["id"], "path": row["path"], "kind": row["kind"],
            "scope": row["scope"], "task_id": row["task_id"],
            "source_task": row["source_task"], "tags": tags,
            "repo": row["repo"], "module": row["module"],
            "updated_at": row["updated_at"] or row["created_at"],
            "confidence": row["confidence"],
        },
        "why": why,
    }


def _snippet(content: str, terms: list[str], chars: int) -> str:
    text = str(content or "")
    low = text.lower()
    pos = -1
    for term in terms:
        pos = low.find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        return text[:chars]
    start = max(0, pos - chars // 3)
    return text[start:start + chars]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _task_context(conn, task_id: str | None) -> dict:
    if not task_id:
        return {}
    row = conn.execute(
        "SELECT title, tags_json, repo, type, scope_paths_json "
        "FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return {}
    tags = [str(x) for x in jloads(row["tags_json"], [])]
    scope_paths = [str(x) for x in jloads(row["scope_paths_json"], [])]
    modules = set()
    for path in scope_paths:
        parts = [p for p in path.split("/") if p and p not in ("*", "**")]
        modules.update(parts[:2])
    return {
        "title": str(row["title"] or ""), "tags": tags,
        "repo": row["repo"], "type": row["type"],
        "scope_paths": scope_paths, "modules": modules,
    }


def _rank(candidates: list[dict], task_ctx: dict, limit: int,
          snippet_chars: int, task_id: str | None = None) -> list[dict]:
    if not candidates:
        return []
    lex_values = [c["_lex"] for c in candidates]
    lo, hi = min(lex_values), max(lex_values)
    span = max(1e-9, hi - lo)
    task_tags = {str(t).lower() for t in task_ctx.get("tags") or []}
    task_modules = {m.lower() for m in task_ctx.get("modules") or set()}
    repo = (task_ctx.get("repo") or "").lower()
    now = datetime.now(timezone.utc)
    for c in candidates:
        r_lex = (c["_lex"] - lo) / span if span > 0 else 0.5
        note_tags = {str(t).lower() for t in c.get("tags") or []}
        tag_hits = len(note_tags & task_tags)
        r_tag = tag_hits / max(1, len(task_tags)) if task_tags else 0.0
        r_repo = 1.0 if repo and (c.get("repo") or "").lower() == repo else 0.0
        module_tokens = set()
        if c.get("module"):
            module_tokens.update(str(c["module"]).lower().split("/"))
        module_tokens.update(str(c.get("path") or "").lower().split("/")[:3])
        r_module = 1.0 if task_modules and module_tokens & task_modules else 0.0
        dt = _parse_ts(c.get("updated_at") or c.get("created_at"))
        age_days = max(0.0, (now - dt).total_seconds() / 86400) if dt else 365.0
        r_recency = math.exp(-age_days / 90.0)
        r_conf = _clamp01(c.get("confidence"))
        kind = c.get("kind")
        r_kind = {
            "vulnerability_pattern": 1.0, "failure_scenario": 1.0,
            "module_knowledge": 0.9, "decision": 0.7, "note": 0.5,
        }.get(kind, 0.0)
        same_task = 1.0 if task_id and (
            c.get("task_id") == task_id or c.get("source_task") == task_id) else 0.0
        meta = (0.20 * r_repo + 0.15 * r_module + 0.20 * r_tag
                + 0.15 * r_recency + 0.15 * r_kind)
        # 先做词法/元数据/同任务加权，再乘 confidence（0.5..1.0）；
        # 低置信度笔记不会被直接排除，但会稳定地排在同等命中之后。
        c["_score"] = (0.65 * r_lex + 0.35 * meta + 0.20 * same_task) \
            * (0.5 + 0.5 * r_conf)
        why = []
        if c.get("bm25") is not None:
            why.append("bm25")
        if tag_hits:
            why.extend(f"tag:{t}" for t in sorted(note_tags & task_tags)[:3])
        if r_repo:
            why.append("repo")
        if r_module:
            why.append("module")
        if r_kind:
            why.append(f"kind:{kind}")
        if same_task:
            why.append("same_task")
        c["why"] = why
    # exact content hash dedup
    seen_hash: dict[str, dict] = {}
    for c in candidates:
        h = c.get("content_hash") or hashlib.sha256(
            str(c.get("content") or "").encode("utf-8")).hexdigest()
        old = seen_hash.get(h)
        if old is None or c["_score"] > old["_score"]:
            seen_hash[h] = c
    candidates = list(seen_hash.values())
    # Same path: keep the best chunk; attach the second-best as extra context.
    by_path: dict[str, list[dict]] = {}
    for c in candidates:
        by_path.setdefault(c["path"], []).append(c)
    merged: list[dict] = []
    for chunks in by_path.values():
        chunks.sort(key=lambda x: x["_score"], reverse=True)
        best = chunks[0]
        if len(chunks) > 1:
            best["snippet"] = (best["snippet"] + "\n…\n"
                               + chunks[1]["snippet"])[: snippet_chars * 2]
        merged.append(best)
    candidates = merged
    # Near-duplicate removal (4-char shingles, Jaccard > .85).
    def shingles(text: str) -> set[str]:
        norm = _WS_RE.sub("", str(text or "").lower())[:2000]
        return {norm[i:i + 4] for i in range(0, max(0, len(norm) - 3))}

    kept: list[dict] = []
    for c in sorted(candidates, key=lambda x: (x["_score"], x.get("updated_at") or ""),
                    reverse=True):
        sh = shingles(c.get("content"))
        if any(
            len(sh & prev) / max(1, len(sh | prev)) > 0.85
            for prev in (k.get("_shingles") or set() for k in kept)
        ):
            continue
        c["_shingles"] = sh
        kept.append(c)
    # MMR-like diversity selection.
    selected: list[dict] = []
    selected_tags: list[set[str]] = []
    selected_shingles: list[set[str]] = []
    selected_kinds: list[str] = []
    kind_counts: dict[str, int] = {}
    while kept and len(selected) < limit:
        # 同类最多 2 条；若其它 kind 已用尽，再放宽（避免只剩一种知识时返回过少）。
        eligible = [c for c in kept if kind_counts.get(c.get("kind"), 0) < 2]
        pool = eligible or kept
        best = None
        best_adj = -1.0
        for c in pool:
            tags = {str(t).lower() for t in c.get("tags") or []}
            tag_overlap = max(
                (len(tags & st) / max(1, len(tags | st)) for st in selected_tags),
                default=0.0)
            sh = c.get("_shingles") or set()
            sh_overlap = max(
                (len(sh & ss) / max(1, len(sh | ss)) for ss in selected_shingles),
                default=0.0)
            same_kind = selected_kinds.count(c.get("kind")) / max(1, len(selected))
            adj = c["_score"] - 0.20 * tag_overlap - 0.15 * sh_overlap - 0.10 * same_kind
            if adj > best_adj:
                best, best_adj = c, adj
        if best is None:
            break
        kept.remove(best)
        selected.append(best)
        selected_tags.append({str(t).lower() for t in best.get("tags") or []})
        selected_shingles.append(best.get("_shingles") or set())
        selected_kinds.append(best.get("kind"))
        kind_counts[best.get("kind")] = kind_counts.get(best.get("kind"), 0) + 1
    out = []
    for c in selected:
        c.pop("_shingles", None)
        c.pop("_lex", None)
        out.append(_row_hit(c, c.get("snippet") or c.get("content") or "",
                            c.get("_score", 0.0), c.get("why") or [],
                            snippet_chars))
    return out


def search_knowledge(conn: sqlite3.Connection, role_slug: str, query: str, *,
                     task_id: str | None = None,
                     scope: str = "auto",
                     kinds: tuple[str, ...] | None = None,
                     tags: tuple[str, ...] | None = None,
                     repo: str | None = None,
                     module: str | None = None,
                     include_snapshots: bool = False,
                     include_task_snapshots: bool | None = None,
                     include_unclassified: bool = False,
                     repo_scope: str = "any",
                     limit: int = 8,
                     snippet_chars: int = 400) -> list[dict]:
    """检索角色知识；任务隔离在 SQL 与后置过滤两层执行。

    `include_task_snapshots` 是设计文档里的参数名，保留为
    `include_snapshots` 的别名。
    """
    if include_task_snapshots is not None:
        include_snapshots = include_task_snapshots
    limit = max(1, min(50, int(limit or 8)))
    scope = scope if scope in ("auto", "role", "task") else "auto"
    task_ctx = _task_context(conn, task_id)
    task_repo = task_ctx.get("repo")
    where, params = _base_where(
        role_slug, task_id, scope, kinds, tags, repo, module,
        include_snapshots, include_unclassified, repo_scope, task_repo)
    if where is None:
        return []
    terms = _tokens(query)
    # FTS 先取 top-30 级候选（limit 较大时适度放宽），再做元数据重排。
    candidate_limit = min(200, max(30, limit * 4))
    rows = []
    used_fts = False
    fts_query = _fts_query(terms)
    if fts_query:
        try:
            sql = (
                "SELECT n.*, bm25(notes_fts) AS bm25 FROM notes_fts "
                "JOIN notes n ON n.rowid=notes_fts.rowid "
                f"WHERE notes_fts MATCH ? AND {where} "
                "ORDER BY bm25 LIMIT ?")
            rows = conn.execute(sql, [fts_query, *params, candidate_limit]).fetchall()
            used_fts = True
        except sqlite3.OperationalError:
            rows = []
    # Short query terms (<3) and no-FTS fallback: LIKE augmentation.
    short_terms = [t for t in terms if len(t) < 3]
    if used_fts and not rows:
        # FTS 无命中：用完整 query 走 LIKE，避免中文/特殊字符漏检。
        like_terms = terms
        used_fts = False
    else:
        like_terms = short_terms if used_fts else terms
    if like_terms:
        clauses = []
        like_params: list = []
        for term in like_terms[:8]:
            pattern = f"%{_like_escape(term)}%"
            clauses.append("(n.content LIKE ? ESCAPE '\\' OR n.tags_json LIKE ? ESCAPE '\\')")
            like_params.extend([pattern, pattern])
        try:
            sql = (
                "SELECT n.*, NULL AS bm25 FROM notes n "
                f"WHERE {where} AND ({' OR '.join(clauses)}) "
                "ORDER BY COALESCE(n.updated_at, n.created_at) DESC LIMIT ?")
            like_rows = conn.execute(
                sql, [*params, *like_params, candidate_limit]).fetchall()
        except sqlite3.OperationalError:
            like_rows = []
        existing = {r["id"] for r in rows}
        rows.extend(r for r in like_rows if r["id"] not in existing)
    if not rows:
        if terms:
            return []
        # No usable query: recent scoped notes, useful for browsing/index.
        sql = (
            "SELECT n.*, NULL AS bm25 FROM notes n "
            f"WHERE {where} ORDER BY COALESCE(n.updated_at, n.created_at) DESC LIMIT ?")
        rows = conn.execute(sql, [*params, candidate_limit]).fetchall()
    candidates: list[dict] = []
    for row in rows:
        # Defense in depth: never return task-scoped rows from another task.
        if row["scope"] == "task" and (task_id is None or row["task_id"] != task_id):
            continue
        if row["scope"] == "unclassified" and not include_unclassified:
            continue
        if row["kind"] == "task_snapshot" and not include_snapshots:
            continue
        content = str(row["content"] or "")
        cand = dict(row)
        cand["tags"] = jloads(row["tags_json"], [])
        cand["_lex"] = (-float(row["bm25"])) if row["bm25"] is not None else 0.3
        cand["snippet"] = _snippet(content, terms, snippet_chars)
        candidates.append(cand)
    return _rank(candidates, task_ctx, limit, snippet_chars, task_id)


def latest_task_snapshot(conn: sqlite3.Connection, role_slug: str,
                         task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM notes WHERE role_slug=? AND kind='task_snapshot' "
        "AND task_id=? AND scope='task' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (role_slug, task_id)).fetchone()
    if row is None:
        return None
    content = str(row["content"] or "")
    return _row_hit(row, content[:4000], 1.0, ["task_snapshot"],
                    snippet_chars=4000)


def get_knowledge(conn: sqlite3.Connection, role_slug: str, note_id: str, *,
                  task_id: str | None = None,
                  include_task_scope: bool = False) -> dict | None:
    row = conn.execute(
        "SELECT * FROM notes WHERE id=? AND role_slug=?", (note_id, role_slug)
    ).fetchone()
    if row is None:
        return None
    if row["scope"] == "task":
        if not include_task_scope and (task_id is None or row["task_id"] != task_id):
            return None
    content = str(row["content"] or "")
    return _row_hit(row, content[:4000], 1.0, ["show"], snippet_chars=4000)


def knowledge_index(conn: sqlite3.Connection, role_slug: str,
                    task_id: str | None = None) -> dict:
    role_counts = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM notes WHERE role_slug=? AND scope='role' "
        "GROUP BY kind", (role_slug,)).fetchall()}
    task_counts = {}
    if task_id:
        task_counts = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM notes WHERE role_slug=? "
            "AND scope='task' AND task_id=? GROUP BY kind",
            (role_slug, task_id)).fetchall()}
    top = conn.execute(
        "SELECT id, path, kind, repo, updated_at FROM notes "
        "WHERE role_slug=? AND scope='role' "
        "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 8",
        (role_slug,)).fetchall()
    return {
        "role_counts": role_counts, "task_counts": task_counts,
        "recent": [dict(r) for r in top],
        "total_role": sum(role_counts.values()),
        "total_task": sum(task_counts.values()),
    }


def _format_hit(hit: dict, max_chars: int = 500) -> str:
    tags = ",".join(hit.get("tags") or [])
    meta = f"{hit.get('kind')}"
    if hit.get("scope") == "task":
        meta += " · task"
    if hit.get("repo"):
        meta += f" · repo={hit['repo']}"
    if tags:
        meta += f" · tags={tags}"
    if hit.get("updated_at"):
        meta += f" · {str(hit['updated_at'])[:19]}"
    return f"- [{meta}] {str(hit.get('snippet') or '')[:max_chars]} (path: {hit.get('path')})"


def format_hits(hits: list[dict], *, max_item_chars: int = 500,
                max_total_chars: int = 3500) -> str:
    lines: list[str] = []
    used = 0
    for hit in hits:
        line = _format_hit(hit, max_item_chars)
        if used + len(line) + 1 > max_total_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def step_knowledge_hint(conn: sqlite3.Connection, role_slug: str,
                        task_id: str | None, task: dict | None,
                        max_chars: int = 1200, *,
                        compact: bool = False) -> str:
    idx = knowledge_index(conn, role_slug, task_id)
    if not idx["total_role"] and not idx["total_task"]:
        return ""
    labels = {"vulnerability_pattern": "漏洞模式", "failure_scenario": "故障场景",
              "module_knowledge": "模块知识", "decision": "决策", "note": "笔记"}
    counts = " / ".join(f"{labels.get(k, k)} {v}"
                        for k, v in sorted(idx["role_counts"].items()) if v)
    task_counts = " / ".join(f"{labels.get(k, k)} {v}"
                             for k, v in sorted(idx["task_counts"].items()) if v)
    if compact:
        # 普通/回复/规划 run 只给一行入口，避免和 CIC 的完整检索块重复。
        summary = "；".join(
            part for part in (
                f"角色 {idx['total_role']} 条（{counts}）" if counts else "",
                f"当前任务 {idx['total_task']} 条（{task_counts}）" if task_counts else "",
            ) if part) or "暂无可复用笔记"
        return (f"知识库：{summary}。需要时执行 `anvil knowledge search "
                f"\"<关键词>\" --task $ANVIL_TASK_ID --limit 5`；"
                f"详情用 `anvil knowledge show <id>`。")[:max_chars]

    task_ctx = _task_context(conn, task_id)
    query = " ".join([str((task or {}).get("title") or ""),
                      *[str(t) for t in task_ctx.get("tags") or []]]).strip()
    hits = search_knowledge(
        conn, role_slug, query, task_id=task_id, scope="auto",
        limit=3, snippet_chars=120,
        repo_scope="same_or_shared") if query else []
    lines = ["## 可用知识（按需检索）"]
    if counts:
        lines.append(f"- 角色知识库：{idx['total_role']} 条（{counts}）")
    if task_counts:
        lines.append(f"- 当前任务：{idx['total_task']} 条（{task_counts}）")
    if hits:
        lines.append("- 相关：")
        for hit in hits:
            lines.append(f"  - [{hit['kind']}] {str(hit['snippet'])[:120]} "
                         f"(path: {hit['path']})")
    lines.append('- 检索：`anvil knowledge search "<关键词>" --task '
                 '$ANVIL_TASK_ID --limit 5`；详情：`anvil knowledge show <id>`')
    text = "\n".join(lines)
    return text[:max_chars]


def pack_blocks(blocks: list[dict], max_chars: int,
                reserve_index_chars: int = 0) -> str:
    """按 priority 打包上下文块，按 order 输出。

    - priority 越小越先保留（关键块先占预算）；order 只决定最终展示顺序；
    - 每个块有 cap/min；预算不足时低优先级块先被丢弃；
    - truncate="tail" 用于最近消息：保留头部标题与最新内容，丢掉较旧的行。
    """
    indexed = list(enumerate(blocks))
    indexed.sort(key=lambda pair: (int(pair[1].get("priority", 100)), pair[0]))
    budget = max(0, int(max_chars) - int(reserve_index_chars))
    selected: list[tuple[int, int, str]] = []
    used = 0
    for idx, block in indexed:
        text = str(block.get("text") or "")
        if not text:
            continue
        cap = max(0, int(block.get("cap") or len(text)))
        minimum = min(int(block.get("min") or 0), len(text))
        available = budget - used
        if available < minimum:
            continue
        take = min(cap, len(text), available)
        if take <= 0:
            continue
        truncate = str(block.get("truncate") or "head")
        text = _truncate_block(text, take, truncate, minimum)
        if not text:
            continue
        order = int(block.get("order", block.get("priority", 100)))
        selected.append((order, idx, text))
        used += len(text) + 2
    selected.sort(key=lambda item: (item[0], item[1]))
    return "\n\n".join(text for _order, _idx, text in selected)[:max_chars]


def _truncate_block(text: str, take: int, mode: str, minimum: int) -> str:
    if take >= len(text):
        return text
    if mode == "tail":
        first, sep, rest = text.partition("\n")
        if not sep:
            return text[-take:]
        keep = max(1, take - len(first) - 1)
        tail = rest[-keep:] if len(rest) > keep else rest
        cut = tail.find("\n")
        if cut >= 0:
            tail = tail[cut + 1:]
        if not tail:
            return first[:take]
        return (first + "\n" + tail)[:take]
    cut = text.rfind("\n", 0, take)
    if cut < minimum:
        cut = take
    return text[:cut]


def reindex_notes(conn: sqlite3.Connection, role_slug: str, *,
                  dry_run: bool = False, prune: bool = False) -> dict:
    """扫描角色 notes/ 文件，解析元数据并同步 notes + FTS。"""
    home = (config.AGENTS_DIR / role_slug).resolve()
    notes_root = (home / "notes").resolve()
    if not notes_root.exists():
        return {"role": role_slug, "scanned": 0, "updated": 0, "skipped": 0}
    if not notes_root.is_relative_to(home):
        raise ValueError("invalid notes root")
    scanned = updated = skipped = 0
    existing = {r["path"]: r for r in conn.execute(
        "SELECT * FROM notes WHERE role_slug=?", (role_slug,)).fetchall()}
    seen: set[str] = set()
    for file in sorted(notes_root.rglob("*.md")):
        try:
            rel = normalize_note_path(file.relative_to(notes_root).as_posix())
        except (ValueError, OSError):
            skipped += 1
            continue
        scanned += 1
        try:
            if file.stat().st_size > 200_000:
                skipped += 1
                continue
            raw = file.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue
        meta, body = parse_note_file(raw)
        inferred = infer_metadata(
            conn, role_slug, rel, body, explicit=meta, for_reindex=True)
        seen.add(rel)
        old = existing.get(rel)
        if old is not None and old["content_hash"] == inferred["content_hash"] \
                and old["kind"] == inferred["kind"] and old["scope"] == inferred["scope"] \
                and old["task_id"] == inferred["task_id"]:
            continue
        updated += 1
        if dry_run:
            continue
        if old is not None:
            conn.execute("DELETE FROM notes WHERE role_slug=? AND path=?",
                         (role_slug, rel))
        conn.execute(
            "INSERT INTO notes (id, role_slug, path, content, created_at, kind, scope, "
            "task_id, source_task, tags_json, repo, module, confidence, updated_at, "
            "source, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hashlib.sha1(f"{role_slug}:{rel}:{inferred['content_hash']}".encode())
             .hexdigest()[:16], role_slug, rel, body, inferred["updated_at"],
             inferred["kind"], inferred["scope"], inferred["task_id"],
             inferred["source_task"], jdumps(inferred["tags"]), inferred["repo"],
             inferred["module"], inferred["confidence"], inferred["updated_at"],
             "reindex", inferred["content_hash"]))
    if prune and not dry_run:
        for rel, old in existing.items():
            if rel not in seen:
                conn.execute("DELETE FROM notes WHERE id=?", (old["id"],))
                updated += 1
    if not dry_run:
        conn.commit()
    return {"role": role_slug, "scanned": scanned, "updated": updated,
            "skipped": skipped, "dry_run": dry_run}


# 设计文档中的名字；保留别名避免调用方按旧名导入失败。
reindex_role_notes = reindex_notes
