"""Memory / notes / lessons / proposals (design §8 / §9).

立场（§8.1）：**文件是真相，SQLite FTS5 是索引**。MEMORY.md / notes 由角色自主维护，
DB 里的 memories/notes 只是检索索引；写入时两者同步。

lesson 升格（§8.3）：pending → 人工审批 → approved，写入共享仓库的
`.anvil/shared/lessons.md`，并设 80 条硬上限（到上限必须先淘汰）。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso

LESSON_CAP = 80


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


def write_note(conn: sqlite3.Connection, role: str, path: str, content: str) -> dict:
    nid = new_id()
    conn.execute(
        "INSERT INTO notes (id, role_slug, path, content, created_at) VALUES (?,?,?,?,?)",
        (nid, role, path, content, now_iso()),
    )
    _index_fts(conn, role, content)
    _append_to_home_file(role, f"notes/{path}", content)
    conn.commit()
    return {"id": nid, "role_slug": role, "path": path}


def _index_fts(conn: sqlite3.Connection, role: str, content: str) -> None:
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


def search_memory(conn: sqlite3.Connection, role: str, q: str, limit: int = 8) -> list[dict]:
    """FTS5 检索自己的 memories/notes（+ shared）；不可用时回退 LIKE。"""
    if not q.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT role_slug, content, bm25(memory_fts) AS score FROM memory_fts "
            "WHERE memory_fts MATCH ? AND role_slug IN (?, 'shared') "
            "ORDER BY score LIMIT ?",
            (_fts_query(q), role, limit),
        ).fetchall()
        return [{"role_slug": r["role_slug"], "content": r["content"], "score": r["score"]} for r in rows]
    except sqlite3.OperationalError:
        pass

    like = f"%{q}%"
    out: list[dict] = []
    for r in conn.execute(
        "SELECT role_slug, content, created_at FROM memories "
        "WHERE owner IN (?, 'shared') AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (role, like, limit),
    ).fetchall():
        out.append({"role_slug": r["role_slug"], "content": r["content"], "score": None})
    for r in conn.execute(
        "SELECT role_slug, content, created_at FROM notes "
        "WHERE role_slug=? AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (role, like, limit),
    ).fetchall():
        out.append({"role_slug": r["role_slug"], "content": r["content"], "score": None})
    return out[:limit]


def read_memory_file(role: str, max_chars: int = 4000) -> str:
    """读角色 MEMORY.md（尾部 max_chars），用于注入上下文。"""
    p = config.AGENTS_DIR / role / "MEMORY.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def context_for_task(conn: sqlite3.Connection, role: str, task: dict | None) -> dict:
    """组装注入上下文：MEMORY.md + FTS 命中的 notes + 按标签匹配的共享经验。"""
    title = (task or {}).get("title") or ""
    tags = (task or {}).get("tags") or []
    query = " ".join([title, *[str(t) for t in tags]]).strip()
    hits = search_memory(conn, role, query, limit=5) if query else []
    lessons = shared_lessons(conn, tags, limit=20)
    return {
        "memory": read_memory_file(role),
        "notes": [h["content"] for h in hits],
        "lessons": [l["content"] for l in lessons],
    }


# ---- shared lessons（升格审批） -------------------------------------------


def create_lesson(conn: sqlite3.Connection, content: str, tags: list[str],
                  repo: str = "shared", source_task: str | None = None) -> dict:
    lid = new_id()
    conn.execute(
        "INSERT INTO lessons (id, content, tags_json, source_task, repo, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (lid, content, jdumps(tags), source_task, repo or "shared", "pending", now_iso()),
    )
    conn.commit()
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
    return get_lesson(conn, lid)


def reject_lesson(conn: sqlite3.Connection, lid: str) -> dict | None:
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE lessons SET status='rejected' WHERE id=?", (lid,))
    conn.commit()
    return get_lesson(conn, lid)


def delete_lesson(conn: sqlite3.Connection, lid: str) -> bool:
    cur = conn.execute("DELETE FROM lessons WHERE id=?", (lid,))
    conn.commit()
    return cur.rowcount > 0


def shared_lessons(conn: sqlite3.Connection, tags: list[str] | None = None,
                   limit: int = 20) -> list[dict]:
    """按任务标签筛选已批准的共享经验；无标签时取最近批准的。"""
    rows = conn.execute(
        "SELECT * FROM lessons WHERE status='approved' ORDER BY COALESCE(approved_at, created_at) DESC"
    ).fetchall()
    wanted = {str(t).lower() for t in (tags or [])}
    out: list[dict] = []
    for r in rows:
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


def _append_to_home_file(role: str, rel_path: str, content: str) -> None:
    """追加写文件（文件是真相）；失败不吞 OSError，直接抛出。"""
    home = config.AGENTS_DIR / role
    target = home / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n" + content + "\n")
