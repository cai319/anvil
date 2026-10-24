"""Memory / notes / lessons / proposals (design §8 / §9).

立场（§8.1）：**文件是真相，SQLite FTS5 是索引**。MEMORY.md / notes 由角色自主维护，
DB 里的 memories/notes 只是检索索引；写入时两者同步。

lesson 升格（§8.3）：pending → 人工审批 → approved，写入共享仓库的
`.anvil/shared/lessons.md`，并设 80 条硬上限（到上限必须先淘汰）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import knowledge as knowledge_service
from . import knowledge_binding, repo_catalog

LESSON_CAP = 80


def _invalidate_repo_catalog() -> None:
    try:
        repo_catalog.invalidate_catalog()
    except Exception:  # noqa: BLE001 —— 缓存失效失败不影响知识写入
        pass


# ---- MEMORY.md / notes ----------------------------------------------------


def append_memory(conn: sqlite3.Connection, role: str, content: str) -> dict:
    mid = new_id()
    conn.execute(
        "INSERT INTO memories (id, role_slug, content, owner, created_at) VALUES (?,?,?,?,?)",
        (mid, role, content, role, now_iso()),
    )
    _index_fts(conn, role, content)
    _append_to_home_file(role, "MEMORY.md", content)
    conn.commit()
    return {"id": mid, "role_slug": role, "content": content, "owner": role}


def write_note(conn: sqlite3.Connection, role: str, path: str, content: str,
               *, kind: str | None = None, scope: str | None = None,
               task_id: str | None = None, source_task: str | None = None,
               tags=None, repo: str | None = None, module: str | None = None,
               confidence: float = 0.5, mode: str = "append",
               source: str = "agent", updated_at: str | None = None) -> dict:
    """写角色笔记：文件 + notes 索引 + notes_fts（触发器同步）。

    - path 必须是 notes/ 下的安全相对路径；
    - scope=task 必须绑定 task_id；scope=role 可跨任务复用；
    - 一个 path 只有一行索引：append 累积文件正文后 upsert 同一行，
      replace 用新内容覆盖文件和索引；
    - 元数据只保留在文件头部的一份 `<!-- anvil: {...} -->` 注释里。
    """
    clean_path = knowledge_service.normalize_note_path(path)
    rel_path = f"notes/{clean_path}"
    existing = conn.execute(
        "SELECT id, content, created_at FROM notes "
        "WHERE role_slug=? AND path=?", (role, clean_path)).fetchone()
    if mode == "replace":
        body = content
    else:
        raw = _read_home_file(role, rel_path)
        _old_meta, old_body = knowledge_service.parse_note_file(raw)
        if not old_body and existing is not None:
            # 文件缺失/为空时以索引内容为底，避免 append 丢历史。
            old_body = str(existing["content"] or "")
        body = (old_body.rstrip("\n") + "\n\n" + content.lstrip("\n")).strip() \
            if old_body else content
    body = body.strip()

    meta = knowledge_service.infer_metadata(
        conn, role, clean_path, body,
        explicit={
            "kind": kind, "scope": scope, "task_id": task_id,
            "source_task": source_task, "tags": tags, "repo": repo,
            "module": module, "confidence": confidence,
            "updated_at": updated_at,
        },
        task_id_hint=task_id, for_reindex=False)
    if kind is None and clean_path.startswith("knowledge/") and not meta.get("task_id"):
        # 无法解析任务目录：保守地不进入自动注入。
        meta["scope"] = "unclassified"
    now = updated_at or now_iso()
    meta["updated_at"] = now
    payload = json.dumps({
        "kind": meta["kind"], "scope": meta["scope"],
        "task_id": meta.get("task_id"), "source_task": meta.get("source_task"),
        "tags": meta.get("tags") or [], "repo": meta.get("repo"),
        "module": meta.get("module"), "confidence": meta.get("confidence"),
        "updated_at": now, "source": source,
    }, ensure_ascii=False)
    file_content = f"<!-- anvil: {payload} -->\n{body.strip()}\n"
    _replace_home_file(role, rel_path, file_content)

    nid = existing["id"] if existing is not None else new_id()
    created_at = existing["created_at"] if existing is not None else now
    values = (
        nid, role, clean_path, body, created_at, meta["kind"], meta["scope"],
        meta.get("task_id"), meta.get("source_task"),
        jdumps(meta.get("tags") or []), meta.get("repo"), meta.get("module"),
        meta.get("confidence"), now, source, meta.get("content_hash"),
    )
    if _notes_path_is_unique(conn):
        conn.execute(
            "INSERT INTO notes (id, role_slug, path, content, created_at, kind, scope, "
            "task_id, source_task, tags_json, repo, module, confidence, updated_at, "
            "source, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_slug, path) DO UPDATE SET "
            "content=excluded.content, kind=excluded.kind, scope=excluded.scope, "
            "task_id=excluded.task_id, source_task=excluded.source_task, "
            "tags_json=excluded.tags_json, repo=excluded.repo, module=excluded.module, "
            "confidence=excluded.confidence, updated_at=excluded.updated_at, "
            "source=excluded.source, content_hash=excluded.content_hash",
            values)
    else:
        conn.execute("DELETE FROM notes WHERE role_slug=? AND path=?",
                     (role, clean_path))
        conn.execute(
            "INSERT INTO notes (id, role_slug, path, content, created_at, kind, scope, "
            "task_id, source_task, tags_json, repo, module, confidence, updated_at, "
            "source, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values)
    conn.commit()
    _invalidate_repo_catalog()
    row = conn.execute(
        "SELECT id, created_at FROM notes WHERE role_slug=? AND path=?",
        (role, clean_path)).fetchone()
    return {
        "id": row["id"] if row else nid, "role_slug": role, "path": clean_path,
        "kind": meta["kind"], "scope": meta["scope"],
        "task_id": meta.get("task_id"), "source_task": meta.get("source_task"),
        "tags": meta.get("tags") or [], "repo": meta.get("repo"),
        "module": meta.get("module"), "confidence": meta.get("confidence"),
        "updated_at": now, "created_at": row["created_at"] if row else created_at,
        "content_hash": meta.get("content_hash"),
    }


def _notes_path_is_unique(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_notes_role_path_unique'").fetchone() is not None


def _index_fts(conn: sqlite3.Connection, role: str, content: str) -> None:
    """MEMORY 专用 FTS；notes 走 notes_fts（由触发器同步）。"""
    if not content:
        return
    try:
        conn.execute("INSERT INTO memory_fts (role_slug, content) VALUES (?,?)", (role, content))
    except sqlite3.OperationalError:
        # FTS5 未编译进该 Python 构建：检索回退 LIKE。
        pass


def _fts_query(q: str) -> str:
    tokens = [t for t in re.split(r"\s+", q.strip()) if t]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def search_memory(conn: sqlite3.Connection, role: str, q: str,
                  limit: int = 8, *, task_id: str | None = None,
                  scope: str | None = None,
                  include_task_snapshots: bool = False) -> list[dict]:
    """兼容入口：MEMORY 条目 + 角色知识库笔记合并检索。

    默认只返回 role-scoped 笔记；显式传 task_id 时才包含该任务的 task-scoped
    内容（快照仍需 include_task_snapshots=True）。MEMORY 与 notes 都严格按
    role_slug 隔离，绝不混入其它角色或其它任务的内容。
    """
    if not q.strip():
        return []
    limit = max(1, int(limit or 8))
    try:
        rows = conn.execute(
            "SELECT role_slug, content, bm25(memory_fts) AS score FROM memory_fts "
            "WHERE memory_fts MATCH ? AND role_slug = ? "
            "ORDER BY score LIMIT ?",
            (_fts_query(q), role, limit),
        ).fetchall()
        memories = [{"role_slug": r["role_slug"], "content": r["content"],
                     "score": r["score"], "owner": "self"} for r in rows]
    except sqlite3.OperationalError:
        like = f"%{q}%"
        memories = [
            {"role_slug": r["role_slug"], "content": r["content"],
             "score": None, "owner": "self"}
            for r in conn.execute(
                "SELECT role_slug, content, created_at FROM memories "
                "WHERE owner = ? AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
                (role, like, limit),
            ).fetchall()
        ]

    note_hits: list[dict] = []
    effective_scope = scope or ("auto" if task_id else "role")
    repos = None
    if task_id:
        task_row = conn.execute("SELECT repo FROM tasks WHERE id=?", (task_id,)).fetchone()
        repos = knowledge_service.scope_repos(
            conn, task_id, task_row["repo"] if task_row else None)
    try:
        hits = knowledge_service.search_knowledge(
            conn, role, q, task_id=task_id,
            scope=effective_scope if effective_scope in ("auto", "role", "task") else "role",
            repo_scope="bound_or_shared", repos=repos,
            limit=limit, include_snapshots=include_task_snapshots)
        note_hits = [{
            "role_slug": role, "content": h["content"], "score": h["score"],
            "owner": "self", "kind": h["kind"], "path": h["path"],
            "scope": h["scope"], "task_id": h["task_id"],
            "snippet": h["snippet"],
        } for h in hits]
    except sqlite3.OperationalError:
        # 知识索引不可用（FTS5 缺失等）时仍返回 MEMORY 命中。
        note_hits = []

    merged: list[dict] = []
    seen: set[str] = set()
    for item in [*memories, *note_hits]:
        content = str(item.get("content") or "")
        if content in seen:
            continue
        seen.add(content)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def read_memory_file(role: str, max_chars: int = 4000) -> str:
    """读角色 MEMORY.md（尾部 max_chars），用于注入上下文。"""
    p = config.AGENTS_DIR / role / "MEMORY.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def consolidate_context(conn: sqlite3.Connection, role: str, task_id: str | None,
                        trigger: str, run_id: str | None = None) -> dict | None:
    """把任务的决策/进度快照写入 notes/knowledge/（文件 + FTS 索引）。

    用于上下文即将被清空（CIC/主动换会话）的场景：新会话不依赖旧对话历史，
    也能从 MEMORY.md + FTS 检索 + 最近快照恢复对历史判断的理解。
    纯 DB 生成，不调用模型，幂等成本低。
    """
    if not task_id:
        return None
    task = conn.execute(
        "SELECT id, title, status, tags_json, repo FROM tasks WHERE id=?",
        (task_id,)).fetchone()
    if task is None:
        return None

    now = now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_trigger = re.sub(r"[^A-Za-z0-9_.-]", "_", trigger or "manual")[:32]
    # 目录段与 knowledge.task_path_segment 一致；真正的任务绑定靠 notes.task_id，
    # 不再依赖 path LIKE（避免特殊字符/通配符误匹配）。
    safe_task = knowledge_service.task_path_segment(task_id)
    run_suffix = re.sub(r"[^A-Za-z0-9]", "", str(run_id or ""))[:8]
    path = f"knowledge/{safe_task}/{stamp}-{safe_trigger}"
    if run_suffix:
        path += f"-{run_suffix}"
    path += ".md"

    lines = [
        "# 任务知识快照",
        f"- 任务：{task['title']}（{task_id}）",
        f"- 状态：{task['status']}",
        f"- 时间：{now}",
        f"- 触发：{safe_trigger}",
    ]
    # 最近结论放在工作流进度之前：快照有长度上限时，优先保住角色自己的判断。
    acts = conn.execute(
        "SELECT role_slug, kind, payload_json FROM activities "
        "WHERE task_id=? AND kind IN ('comment','mention') ORDER BY id DESC LIMIT 30",
        (task_id,)).fetchall()
    if acts:
        lines += ["", "## 最近结论与交接"]
        for a in reversed(acts):
            payload = jloads(a["payload_json"], {})
            if a["kind"] == "comment":
                text = str(payload.get("body") or "")
            else:
                text = f"@{payload.get('target', '')}: {payload.get('message', '')}"
            lines.append(f"- [{a['role_slug'] or 'system'}] {text[:300]}")

    wf = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
        (task_id,)).fetchone()
    if wf is not None:
        steps = conn.execute(
            "SELECT step_id, title, role_slug, status, summary, feedback "
            "FROM workflow_steps WHERE workflow_id=? "
            "ORDER BY updated_at DESC, step_id DESC LIMIT 60",
            (wf["id"],)).fetchall()
        if steps:
            lines += ["", "## 工作流进度"]
            for s in reversed(steps):
                line = (f"- [{s['status']}] {s['step_id']} {s['title']}"
                        f"（{s['role_slug']}）")
                if s["summary"]:
                    line += f"：{str(s['summary'])[:160]}"
                if s["feedback"]:
                    line += f"；反馈：{str(s['feedback'])[:160]}"
                lines.append(line)

    content = "\n".join(lines)[:12000]
    task_tags = [str(t) for t in jloads(task["tags_json"], [])]
    note = write_note(
        conn, role, path, content,
        kind="task_snapshot", scope="task", task_id=task_id,
        tags=[*task_tags, "snapshot"], repo=task["repo"],
        confidence=0.9, source="consolidate")
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (role, task_id, "session.context_digest",
         jdumps({"path": path, "trigger": safe_trigger, "run_id": run_id,
                 "chars": len(content)}), now),
    )
    conn.commit()
    return {**note, "chars": len(content), "trigger": safe_trigger}


def context_for_task(conn: sqlite3.Connection, role: str, task: dict | None,
                     *, task_id: str | None = None) -> dict:
    """组装投影上下文：MEMORY + 可复用知识 + 共享经验。

    任务隔离：task-scoped 只取当前 task；其它任务的行绝不进入投影。任务快照不进
    投影（one-shot 无 CIC 时由步骤 prompt 的检索提示覆盖），避免把历史快照当成
    当前事实。
    """
    task = dict(task or {})
    task_id = task_id or task.get("id")
    title = task.get("title") or ""
    tags = task.get("tags") or []
    scope = knowledge_binding.retrieval_scope(
        conn, task if task.get("id") else None)
    repos = scope.get("repos")
    query = " ".join([title, *[str(t) for t in tags]]).strip()
    task_hits = knowledge_service.search_knowledge(
        conn, role, query, task_id=task_id, scope="auto",
        limit=6, snippet_chars=800, repo_scope="bound_or_shared",
        repos=repos) if query else []
    role_hits = knowledge_service.search_knowledge(
        conn, role, query, task_id=None, scope="role",
        limit=6, snippet_chars=800, repo_scope="bound_or_shared",
        repos=repos) if query else []
    merged: list[dict] = []
    seen: set[str] = set()
    for hit in [*task_hits, *role_hits]:
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        merged.append(hit)
        if len(merged) >= 8:
            break
    notes_text = knowledge_service.format_hits(
        merged, max_item_chars=800, max_total_chars=4000)
    lessons = shared_lessons(conn, tags, repo=task.get("repo"), repos=repos, limit=8)
    lesson_lines: list[str] = []
    used = 0
    for lesson in lessons:
        line = f"- {str(lesson.get('content') or '')[:600]}"
        if used + len(line) + 1 > 2000:
            break
        lesson_lines.append(line)
        used += len(line) + 1
    return {
        "memory": read_memory_file(role),
        # 保留键位以兼容 project_instruction 调用方；按计划投影不注入任务快照。
        "snapshot": "",
        "notes": [notes_text] if notes_text else [],
        "lessons": lesson_lines,
        "knowledge_repos": repos or [],
        "retrieval_scope": scope.get("mode"),
    }


# ---- shared lessons（升格审批） -------------------------------------------


def create_lesson(conn: sqlite3.Connection, content: str, tags: list[str],
                  repo: str = "shared", source_task: str | None = None,
                  source_role: str | None = None) -> dict:
    lid = new_id()
    conn.execute(
        "INSERT INTO lessons (id, content, tags_json, source_task, source_role, repo, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (lid, content, jdumps(tags), source_task, source_role, repo or "shared",
         "pending", now_iso()),
    )
    conn.commit()
    _invalidate_repo_catalog()
    return get_lesson(conn, lid)  # type: ignore[return-value]


def get_lesson(conn: sqlite3.Connection, lid: str) -> dict | None:
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
    return _lesson_row(row) if row else None


def list_lessons(conn: sqlite3.Connection, status: str | None = None,
                 repo: str | None = None, q: str | None = None) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if repo:
        clauses.append("repo=?")
        params.append(repo)
    if q:
        clauses.append("content LIKE ?")
        params.append(f"%{q}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM lessons{where} ORDER BY created_at DESC", params
    ).fetchall()
    return [_lesson_row(r) for r in rows]


def update_lesson(conn: sqlite3.Connection, lid: str, *, content: str | None = None,
                  tags: list[str] | None = None, repo: str | None = None) -> dict | None:
    lesson = get_lesson(conn, lid)
    if lesson is None:
        return None
    fields: dict = {}
    if content is not None:
        fields["content"] = content
    if tags is not None:
        fields["tags_json"] = jdumps(tags)
    if repo is not None:
        fields["repo"] = repo
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE lessons SET {cols} WHERE id=?", (*fields.values(), lid))
        conn.commit()
        _invalidate_repo_catalog()
    return get_lesson(conn, lid)


def approve_lesson(conn: sqlite3.Connection, lid: str) -> dict | None:
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
    if row is None:
        return None
    if row["status"] == "approved":
        return _lesson_row(row)
    repo = row["repo"] or "shared"
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM lessons WHERE status='approved' AND repo=?", (repo,)
    ).fetchone()["n"]
    if count >= LESSON_CAP:
        raise ConflictError(
            f"lessons cap {LESSON_CAP} reached for repo {repo}; delete one before approving"
        )
    conn.execute(
        "UPDATE lessons SET status='approved', approved_at=? WHERE id=?", (now_iso(), lid)
    )
    _append_lesson_file(repo, row["content"], jloads(row["tags_json"], []), row["source_task"])
    conn.commit()
    _invalidate_repo_catalog()
    return get_lesson(conn, lid)


def reject_lesson(conn: sqlite3.Connection, lid: str) -> dict | None:
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE lessons SET status='rejected' WHERE id=?", (lid,))
    conn.commit()
    _invalidate_repo_catalog()
    return get_lesson(conn, lid)


def delete_lesson(conn: sqlite3.Connection, lid: str) -> bool:
    cur = conn.execute("DELETE FROM lessons WHERE id=?", (lid,))
    conn.commit()
    _invalidate_repo_catalog()
    return cur.rowcount > 0


def shared_lessons(conn: sqlite3.Connection, tags: list[str] | None = None,
                   limit: int = 20, *, repo: str | None = None,
                   repos: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    """按任务标签/repo 筛选已批准的共享经验；无标签时取最近批准的。

    repos 是任务的知识检索范围（绑定仓库 + 执行仓库 + shared）。
    """
    rows = conn.execute(
        "SELECT * FROM lessons WHERE status='approved' "
        "ORDER BY COALESCE(approved_at, created_at) DESC"
    ).fetchall()
    wanted = {str(t).lower() for t in (tags or [])}
    allowed = {str(x) for x in repos} if repos is not None else None
    out: list[dict] = []
    for r in rows:
        item_repo = r["repo"] or "shared"
        if repo and item_repo not in (repo, "shared"):
            continue
        if allowed is not None and item_repo != "shared" and item_repo not in allowed:
            continue
        item_tags = {str(t).lower() for t in jloads(r["tags_json"], [])}
        if wanted and item_tags and not (wanted & item_tags):
            continue
        out.append(_lesson_row(r))
        if len(out) >= limit:
            break
    return out


def _lesson_row(row) -> dict:
    d = dict(row)
    d["tags"] = jloads(d.pop("tags_json"), [])
    return d


def _safe_repo(repo: str) -> str:
    """repo 名称来自 API/Agent，限制为单段安全字符，避免路径穿越。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", repo or "shared")


def _lesson_path(repo: str) -> Path:
    if repo in (None, "", "shared"):
        return config.DATA_DIR / "shared" / "lessons.md"
    return config.REPOS_DIR / _safe_repo(repo) / ".anvil" / "shared" / "lessons.md"


def _append_lesson_file(repo: str, content: str, tags: list[str], source_task: str | None) -> None:
    target = _lesson_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    tag_text = " ".join(f"#{t}" for t in tags)
    source = f" (source: {source_task})" if source_task else ""
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(f"\n- {content} {tag_text}{source}\n")


def list_shared_lesson_lines(repo: str = "shared") -> list[str]:
    p = _lesson_path(repo)
    try:
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


# ---- proposals（角色演化审批，§9） ----------------------------------------


def create_proposal(conn: sqlite3.Connection, role: str, field: str, proposed: dict,
                    rationale: str | None, risk: str | None) -> dict:
    pid = new_id()
    conn.execute(
        "INSERT INTO proposals (id, role_slug, field, proposed_json, rationale, risk, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (pid, role, field, jdumps(proposed), rationale, risk, "pending", now_iso()),
    )
    conn.commit()
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _proposal_row(r)


def list_proposals(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM proposals ORDER BY created_at DESC").fetchall()
    return [_proposal_row(r) for r in rows]


def approve_proposal(conn: sqlite3.Connection, pid: str) -> dict | None:
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if r is None:
        return None
    if r["status"] != "approved":
        _apply_proposal(conn, r)
        conn.execute("UPDATE proposals SET status='approved', decided_at=? WHERE id=?", (now_iso(), pid))
        conn.commit()
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _proposal_row(r)


def reject_proposal(conn: sqlite3.Connection, pid: str) -> dict | None:
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if r is None:
        return None
    conn.execute("UPDATE proposals SET status='rejected', decided_at=? WHERE id=?", (now_iso(), pid))
    conn.commit()
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _proposal_row(r)


def _proposal_row(row) -> dict:
    d = dict(row)
    d["proposed"] = jloads(d.pop("proposed_json"), {})
    return d


def _apply_proposal(conn: sqlite3.Connection, row) -> None:
    """审批通过后把提案落到角色配置（§9.2 审批档字段）。"""
    from . import roles as roles_service  # 惰性导入避免循环

    slug = row["role_slug"]
    field = row["field"]
    proposed = jloads(row["proposed_json"], {})
    role = roles_service.get(conn, slug)
    if role is None:
        return

    if field == "tools":
        roles_service.update_tools(
            conn, slug,
            proposed.get("preset") if isinstance(proposed, dict) else None,
            proposed.get("allowlist") if isinstance(proposed, dict) else None,
            proposed.get("max_tool_tokens") if isinstance(proposed, dict) else None,
        )
        return

    definition = jloads(role["definition_json"], {})
    if field == "capabilities" and isinstance(proposed, dict) and isinstance(definition.get("capabilities"), dict):
        definition["capabilities"] = {**definition["capabilities"], **proposed}
    else:
        definition[field] = proposed
    roles_service.update(conn, slug, definition=definition)


def write_instruction(conn: sqlite3.Connection, role: str, content: str) -> dict:
    """`anvil self edit-instruction`（自主档）：直接写 instruction.md。"""
    home = config.AGENTS_DIR / role
    home.mkdir(parents=True, exist_ok=True)
    (home / "instruction.md").write_text(content, encoding="utf-8")
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
        (role, None, "self.edit_instruction", jdumps({"chars": len(content)}), now_iso()),
    )
    conn.commit()
    return {"role": role, "accepted": True, "path": str(home / "instruction.md")}


def _read_home_file(role: str, rel_path: str) -> str:
    """读角色 home 内文件；不存在/不可读时返回空串（调用方回退 DB）。"""
    home = (config.AGENTS_DIR / role).resolve()
    target = (home / rel_path).resolve()
    if not target.is_relative_to(home):
        raise ValueError("path escapes role home")
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def _append_to_home_file(role: str, rel_path: str, content: str) -> None:
    """追加写文件（文件是真相）；失败不吞 OSError，直接抛出。"""
    home = (config.AGENTS_DIR / role).resolve()
    target = (home / rel_path).resolve()
    if not target.is_relative_to(home):
        raise ValueError("path escapes role home")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n" + content + "\n")


def _replace_home_file(role: str, rel_path: str, content: str) -> None:
    """原子替换文件（replace 模式）；先写同目录临时文件再 os.replace。"""
    home = (config.AGENTS_DIR / role).resolve()
    target = (home / rel_path).resolve()
    if not target.is_relative_to(home):
        raise ValueError("path escapes role home")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, target)
