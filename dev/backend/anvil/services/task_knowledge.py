"""任务共享知识：任务参与角色共享；子任务知识回流父任务并打标记。

文件真相：任务级知识卡写入 `<task-workspace>/knowledge/<local-repo>/cards/`；
没有 local repo 时写入 `<task-workspace>/knowledge/temp/cards/`。DB 是索引。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ..util import jdumps, jloads, new_id, now_iso
from . import knowledge_card, knowledge_quality
from .tasks import ConflictError, get as get_task


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["fields"] = jloads(d.pop("fields_json", "{}"), {})
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    stored_quality = jloads(d.pop("quality_json", "{}"), {})
    d["quality"] = knowledge_quality.evaluate_row(d, "task_knowledge")
    if isinstance(stored_quality, dict) and stored_quality.get("llm"):
        d["quality"]["llm"] = stored_quality["llm"]
    d["legacy"] = bool(d["quality"].get("legacy"))
    d["completeness"] = d.get("completeness") or knowledge_card.completeness(
        d.get("kind") or "", d["fields"])
    return d


def get(conn: sqlite3.Connection, knowledge_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM task_knowledge WHERE id=?", (knowledge_id,)
    ).fetchone())


def list_for_task(conn: sqlite3.Connection, task_id: str, *,
                  include_subtasks: bool = False, deep: bool = False) -> list[dict]:
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    ids = [task_id]
    if include_subtasks or deep:
        if deep:
            frontier = [task_id]
            seen = {task_id}
            while frontier:
                nxt = []
                for parent in frontier:
                    for row in conn.execute(
                        "SELECT id FROM tasks WHERE parent_id=?", (parent,)
                    ).fetchall():
                        if row["id"] not in seen:
                            seen.add(row["id"])
                            ids.append(row["id"])
                            nxt.append(row["id"])
                frontier = nxt
        else:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE parent_id=?", (task_id,)
            ).fetchall()
            ids.extend(r["id"] for r in rows)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM task_knowledge WHERE task_id IN ({placeholders}) "
        "AND status='active' ORDER BY created_at DESC",
        ids,
    ).fetchall()
    return [_row(r) for r in rows]  # type: ignore[list-item]


def _like_pattern(text: str) -> str:
    escaped = (str(text).replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


def list_all(conn: sqlite3.Connection, *, status: str | None = None,
             workspace_id: str | None = None, q: str | None = None,
             limit: int = 200, offset: int = 0) -> dict:
    """跨任务知识列表（人工审计视图）。"""
    limit = max(1, min(500, int(limit or 200)))
    offset = max(0, int(offset or 0))
    clauses: list[str] = []
    params: list = []
    if status and status != "all":
        clauses.append("tk.status=?")
        params.append(status)
    if workspace_id:
        clauses.append("t.workspace_id=?")
        params.append(workspace_id)
    if q and str(q).strip():
        pattern = _like_pattern(str(q).strip())
        clauses.append(
            "(tk.title LIKE ? ESCAPE '\\' OR tk.tldr LIKE ? ESCAPE '\\' "
            "OR tk.body_md LIKE ? ESCAPE '\\' OR tk.tags_json LIKE ? ESCAPE '\\' "
            "OR COALESCE(t.title, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 5)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM task_knowledge tk "
        "LEFT JOIN tasks t ON t.id=tk.task_id "
        f"{where}", params,
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT tk.*, t.title AS task_title, t.status AS task_status, "
        "t.workspace_id AS workspace_id, t.archived_at AS task_archived_at, "
        "w.name AS workspace_name "
        "FROM task_knowledge tk "
        "LEFT JOIN tasks t ON t.id=tk.task_id "
        "LEFT JOIN workspaces w ON w.id=t.workspace_id "
        f"{where} ORDER BY tk.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = [_row(r) for r in rows]  # type: ignore[list-item]
    return {"items": items, "total": total, "limit": limit,
            "offset": offset, "has_more": offset + len(items) < total}


def _local_repo_binding(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT repo, repo_id, path FROM task_knowledge_repos "
        "WHERE task_id=? AND scope='local' AND status='applied' "
        "ORDER BY is_primary DESC, score DESC LIMIT 1", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def _card_dir(conn: sqlite3.Connection, task_id: str) -> tuple[Path | None, dict | None]:
    binding = _local_repo_binding(conn, task_id)
    if binding and binding.get("path"):
        return Path(binding["path"]) / "cards", binding
    from . import task_workspace
    ws = task_workspace.resolve(conn, task_id, create=True)
    if not ws:
        return None, binding
    return Path(ws) / "knowledge" / "temp" / "cards", binding


def _write_card_file(conn: sqlite3.Connection, task_id: str, record: dict) -> str:
    target_dir, binding = _card_dir(conn, task_id)
    if target_dir is None:
        return ""
    status = str(record.get("status") or "active")
    if status in ("proposed", "draft"):
        target_dir = target_dir.parent / "drafts"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{record['id']}.md"
    meta = {
        "id": record["id"], "task_id": task_id,
        "repo": (binding or {}).get("repo"),
        "repo_id": (binding or {}).get("repo_id"),
        "kind": record.get("kind"), "title": record.get("title"),
        "tldr": record.get("tldr"), "tags": record.get("tags") or [],
        "fields": record.get("fields") or {},
        "completeness": record.get("completeness", 0),
        "quality_score": record.get("quality_score", 0),
        "status": record.get("status") or "active",
        "source_role": record.get("source_role"),
        "review_at": record.get("review_at"),
        "created_at": record.get("created_at"),
    }
    text = f"<!-- anvil: {json.dumps(meta, ensure_ascii=False)} -->\n{record.get('body_md') or ''}\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return str(target)


def create(conn: sqlite3.Connection, task_id: str, *,
           title: str, tldr: str, kind: str,
           fields: dict | None = None, tags: list[str] | None = None,
           source_role: str | None = None,
           review_at: str | None = None,
           status: str = "active") -> dict:
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    if kind not in knowledge_card.KINDS:
        raise ConflictError(f"unknown knowledge kind: {kind}")
    fields = knowledge_card.normalize_fields(fields)
    body_md = knowledge_card.render_markdown(title, tldr, kind, fields)
    check = knowledge_card.validate_card(kind, fields, for_approval=False)
    parent_task_id = task.get("parent_id")
    kid = new_id()
    now = now_iso()
    status = status if status in ("active", "proposed", "archived") else "active"
    digest = knowledge_card.content_hash(title, tldr, body_md)
    quality = knowledge_quality.evaluate_row({
        "kind": kind, "title": title, "tldr": tldr, "body_md": body_md,
        "fields": fields, "status": status, "source_task_id": task_id,
        "review_at": review_at,
    }, "task_knowledge")
    conn.execute(
        "INSERT INTO task_knowledge "
        "(id, task_id, parent_task_id, source_subtask_id, origin, kind, title, tldr, "
        "body_md, fields_json, tags_json, source_role, status, completeness, "
        "content_hash, quality_json, quality_score, quality_checked_at, review_at, "
        "created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            kid, task_id, parent_task_id,
            task_id if parent_task_id else None,
            "subtask" if parent_task_id else "task",
            kind, title, tldr, body_md, jdumps(fields), jdumps(tags or []),
            source_role, status, check["completeness"], digest, jdumps(quality),
            quality["score"], quality["checked_at"], review_at, now, now,
        ),
    )
    path = _write_card_file(conn, task_id, {
        "id": kid, "kind": kind, "title": title, "tldr": tldr,
        "body_md": body_md, "tags": tags or [], "status": status,
        "source_role": source_role, "fields": fields,
        "completeness": check["completeness"], "quality_score": quality["score"],
        "review_at": review_at, "created_at": now,
    })
    binding = _local_repo_binding(conn, task_id)
    conn.execute(
        "UPDATE task_knowledge SET repo_id=?, repo_key=?, path=?, status=? "
        "WHERE id=?",
        ((binding or {}).get("repo_id"), (binding or {}).get("repo_id"),
         path, status, kid),
    )
    record = get(conn, kid)
    assert record is not None
    conn.commit()
    return record


def update(conn: sqlite3.Connection, knowledge_id: str, *,
           title: str | None = None, tldr: str | None = None,
           fields: dict | None = None, tags: list[str] | None = None,
           review_at: str | None = None, expected_version: int | None = None,
           actor: str = "human") -> dict:
    old = get(conn, knowledge_id)
    if old is None:
        raise LookupError(knowledge_id)
    if old.get("status") != "proposed":
        raise ConflictError("only proposed task knowledge can be edited")
    if expected_version is not None and old.get("version") and int(expected_version) != int(old["version"]):
        raise ConflictError("task knowledge changed; reload and retry")
    new_title = title if title is not None else old["title"]
    new_tldr = tldr if tldr is not None else old["tldr"]
    new_fields = knowledge_card.normalize_fields(
        fields if fields is not None else old["fields"])
    body_md = knowledge_card.render_markdown(
        new_title, new_tldr, old["kind"], new_fields)
    quality = knowledge_quality.evaluate_row({
        **old, "title": new_title, "tldr": new_tldr, "body_md": body_md,
        "fields": new_fields, "review_at": review_at,
    }, "task_knowledge")
    now = now_iso()
    conn.execute(
        "UPDATE task_knowledge SET title=?, tldr=?, body_md=?, fields_json=?, "
        "tags_json=?, completeness=?, content_hash=?, quality_json=?, quality_score=?, "
        "quality_checked_at=?, review_at=?, updated_at=? WHERE id=?",
        (new_title, new_tldr, body_md, jdumps(new_fields),
         jdumps(tags if tags is not None else old.get("tags") or []),
         knowledge_card.completeness(old["kind"], new_fields),
         knowledge_card.content_hash(new_title, new_tldr, body_md),
         jdumps(quality), quality["score"], quality["checked_at"],
         review_at if review_at is not None else old.get("review_at"), now,
         knowledge_id),
    )
    current = get(conn, knowledge_id)
    assert current is not None
    path = _write_card_file(conn, current["task_id"], current)
    conn.execute("UPDATE task_knowledge SET path=? WHERE id=?", (path, knowledge_id))
    conn.commit()
    return get(conn, knowledge_id) or current


def approve(conn: sqlite3.Connection, knowledge_id: str, *,
            actor: str = "human") -> dict:
    """人工批准任务级知识卡：proposed → active，文件从 drafts/ 移到 cards/。"""
    card = get(conn, knowledge_id)
    if card is None:
        raise LookupError(knowledge_id)
    if card.get("status") == "active":
        return card
    if card.get("status") != "proposed":
        raise ConflictError(
            f"only proposed task knowledge can be approved, current {card.get('status')}")
    check = knowledge_card.validate_card(
        card["kind"], card["fields"], for_approval=True)
    quality = knowledge_quality.evaluate_row(card, "task_knowledge")
    blocking = list(dict.fromkeys(check["errors"] + quality.get("blocking", [])))
    if blocking:
        raise knowledge_quality.QualityGateError("; ".join(blocking), quality)
    now = now_iso()
    conn.execute(
        "UPDATE task_knowledge SET status='active', completeness=?, quality_json=?, "
        "quality_score=?, quality_checked_at=?, updated_at=? WHERE id=?",
        (check["completeness"], jdumps(quality), quality["score"],
         quality["checked_at"], now, knowledge_id))
    card = get(conn, knowledge_id)
    assert card is not None
    path = _write_card_file(conn, card["task_id"], card)
    conn.execute("UPDATE task_knowledge SET path=? WHERE id=?", (path, knowledge_id))
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, card.get("task_id"), "knowledge.approve",
         jdumps({"table": "task_knowledge", "card_id": knowledge_id,
                 "quality_score": quality["score"]}), now))
    conn.commit()
    return get(conn, knowledge_id) or card


def archive(conn: sqlite3.Connection, knowledge_id: str) -> bool:
    if get(conn, knowledge_id) is None:
        return False
    conn.execute(
        "UPDATE task_knowledge SET status='archived', updated_at=? WHERE id=?",
        (now_iso(), knowledge_id))
    conn.commit()
    return True


def render_for_task(conn: sqlite3.Connection, task_id: str, *,
                    budget_chars: int = 800) -> str:
    """渲染任务共享知识（含直接子任务来源）。"""
    from .. import config
    if not config.TASK_KNOWLEDGE_ENABLED:
        return ""
    cards = list_for_task(conn, task_id, include_subtasks=True)
    lines: list[str] = []
    used = 0
    now = now_iso()
    hit_ids: list[str] = []
    for card in cards:
        origin = (f"来自子任务 {card['source_subtask_id']}"
                  if card.get("source_subtask_id") else "任务共享")
        line = f"- [{origin}] {card['title']}：{card['tldr']}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        hit_ids.append(card["id"])
        used += len(line) + 1
    if not lines:
        return ""
    for knowledge_id in hit_ids:
        conn.execute(
            "UPDATE task_knowledge SET hit_count=COALESCE(hit_count,0)+1, "
            "last_hit_at=? WHERE id=?",
            (now, knowledge_id),
        )
    conn.commit()
    return "## 任务共享知识\n" + "\n".join(lines)


def archive_for_task(conn: sqlite3.Connection, task_id: str) -> None:
    """任务删除时保留知识行，仅标记 archived。"""
    conn.execute(
        "UPDATE task_knowledge SET status='archived', updated_at=? "
        "WHERE task_id=? OR parent_task_id=?",
        (now_iso(), task_id, task_id),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, knowledge_id: str,
           task_id: str | None = None) -> bool:
    if task_id is not None:
        row = conn.execute(
            "SELECT task_id FROM task_knowledge WHERE id=?", (knowledge_id,)).fetchone()
        if row is None or row["task_id"] != task_id:
            return False
    cur = conn.execute("DELETE FROM task_knowledge WHERE id=?", (knowledge_id,))
    conn.commit()
    return cur.rowcount > 0
