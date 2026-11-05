"""主题知识库卡片：文件是真相，SQLite/FTS 是索引。

目录：
    data/knowledge-repos/<repo_id>/
      index.md
      cards/<card-id>.md       # approved/active
      proposed/<card-id>.md    # proposed/draft（人工批准后移动到 cards/）

文件头使用与 notes 一致的 `<!-- anvil: {...} -->` 元数据；正文为 Markdown。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import knowledge, knowledge_card, knowledge_quality, knowledge_repos


def repo_dir(repo_id: str) -> Path:
    return Path(config.KNOWLEDGE_REPOS_DIR) / str(repo_id)


def _card_paths(repo_id: str, card_id: str, status: str) -> Path:
    sub = "cards" if status in ("approved", "active") else "proposed"
    return repo_dir(repo_id) / sub / f"{card_id}.md"


def _write_card_file(repo_id: str, card: dict, status: str) -> str:
    target = _card_paths(repo_id, card["id"], status)
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": card["id"], "repo_id": repo_id, "repo": card.get("repo"),
        "kind": card.get("kind"), "authority": card.get("authority", "reference"),
        "status": status, "title": card.get("title"), "tldr": card.get("tldr"),
        "tags": card.get("tags") or [], "fields": card.get("fields") or {},
        "completeness": card.get("completeness", 0),
        "quality_score": card.get("quality_score", 0),
        "confidence": card.get("confidence", 0.5),
        "source_task_id": card.get("source_task_id"),
        "source_step_id": card.get("source_step_id"),
        "source_role": card.get("source_role"),
        "source_event": card.get("source_event"),
        "content_hash": card.get("content_hash"),
        "version": card.get("version", 1),
        "review_at": card.get("review_at"),
        "expires_at": card.get("expires_at"),
        "created_at": card.get("created_at") or now_iso(),
        "updated_at": now_iso(),
    }
    text = f"<!-- anvil: {json.dumps(meta, ensure_ascii=False)} -->\n{card.get('body_md') or ''}\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    # approved 后移除 proposed 副本，避免 reindex 把旧状态读回来。
    other = _card_paths(
        repo_id, card["id"],
        "proposed" if status in ("approved", "active") else "approved")
    if other.exists():
        try:
            other.unlink()
        except OSError:
            pass
    return str(target.relative_to(repo_dir(repo_id)))


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["fields"] = jloads(d.pop("fields_json", "{}"), {})
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    d["conflicts"] = jloads(d.pop("conflicts_json", "[]"), [])
    stored_quality = jloads(d.pop("quality_json", "{}"), {})
    d["quality"] = knowledge_quality.evaluate_row(d, "repo_knowledge")
    if isinstance(stored_quality, dict) and stored_quality.get("llm"):
        d["quality"]["llm"] = stored_quality["llm"]
    d["legacy"] = bool(d["quality"].get("legacy"))
    d["completeness"] = d.get("completeness") or knowledge_card.completeness(
        d.get("kind") or "", d["fields"])
    return d


def get(conn: sqlite3.Connection, card_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM repo_knowledge WHERE id=?", (card_id,)).fetchone())


def list_cards(conn: sqlite3.Connection, *, repo: str | None = None,
               status: str | None = None, q: str | None = None,
               limit: int = 200) -> list[dict]:
    clauses, params = [], []
    if repo:
        repo_row = knowledge_repos.get(conn, repo)
        key = (repo_row or {}).get("id") or knowledge_repos.repo_key(repo)
        clauses.append("repo_id=?")
        params.append(key)
    if status:
        clauses.append("status=?")
        params.append(status)
    needle = str(q or "").strip()
    if needle:
        pattern = f"%{needle}%"
        clauses.append(
            "(title LIKE ? OR tldr LIKE ? OR body_md LIKE ? OR tags_json LIKE ?)")
        params.extend([pattern] * 4)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM repo_knowledge {where} ORDER BY updated_at DESC LIMIT ?",
        [*params, max(1, min(1000, int(limit or 200)))]).fetchall()
    return [_row(r) or {} for r in rows]


def create(conn: sqlite3.Connection, repo: str, *,
           kind: str, title: str, tldr: str, body_md: str,
           fields: dict | None = None, tags: list | None = None,
           authority: str = "reference", status: str = "proposed",
           source_task_id: str | None = None,
           source_step_id: str | None = None,
           source_role: str | None = None,
           source_event: str | None = None,
           confidence: float = 0.5,
           review_at: str | None = None,
           expires_at: str | None = None,
           created_by: str = "agent",
           auto_created: bool = False,
           allow_duplicate: bool = True,
           commit: bool = True) -> dict:
    if kind not in knowledge_card.KINDS:
        raise ConflictError(f"unknown knowledge kind: {kind}")
    fields = knowledge_card.normalize_fields(fields)
    if not body_md:
        body_md = knowledge_card.render_markdown(title, tldr, kind, fields)
    repo_row = knowledge_repos.ensure(
        conn, repo, created_by=created_by, source_task_id=source_task_id,
        auto_created=auto_created, commit=False)
    repo_id = repo_row["id"]
    display = repo_row["display_name"]
    content_hash = knowledge_card.content_hash(title, tldr, body_md)
    duplicate = conn.execute(
        "SELECT * FROM repo_knowledge WHERE repo_id=? AND kind=? "
        "AND content_hash=? ORDER BY created_at LIMIT 1",
        (repo_id, kind, content_hash)).fetchone()
    if duplicate is not None:
        if not allow_duplicate:
            raise ConflictError(
                f"duplicate knowledge card already exists: {duplicate['id']}")
        return {**(_row(duplicate) or {}), "deduped": True}

    cid = new_id()
    now = now_iso()
    review_at = review_at or knowledge_quality.default_review_at(kind)
    row = {
        "id": cid, "repo_id": repo_id, "repo": display, "kind": kind,
        "authority": authority, "status": status, "title": title, "tldr": tldr,
        "body_md": body_md, "fields": fields, "tags": tags or [],
        "source_task_id": source_task_id, "source_step_id": source_step_id,
        "source_role": source_role, "source_event": source_event,
        "content_hash": content_hash, "version": 1,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "completeness": knowledge_card.completeness(kind, fields),
        "review_at": review_at, "expires_at": expires_at,
        "created_at": now, "updated_at": now,
    }
    quality = knowledge_quality.evaluate_row(row, "repo_knowledge")
    path = _write_card_file(repo_id, {**row, "quality_score": quality["score"]}, status)
    conn.execute(
        "INSERT INTO repo_knowledge "
        "(id, repo_id, repo, kind, authority, status, title, tldr, body_md, fields_json, "
        " tags_json, source_task_id, source_step_id, source_role, source_event, "
        " provenance_json, content_hash, version, confidence, completeness, "
        " quality_json, quality_score, quality_checked_at, review_at, expires_at, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, repo_id, display, kind, authority, status, title, tldr, body_md,
         jdumps(fields), jdumps(tags or []), source_task_id, source_step_id,
         source_role, source_event, "{}", content_hash, 1,
         row["confidence"], row["completeness"], jdumps(quality),
         quality["score"], quality["checked_at"], review_at, expires_at, now, now),
    )
    _snapshot(conn, cid, 1, row, created_by)
    if commit:
        conn.commit()
    result = get(conn, cid) or {**row, "quality": quality}
    return {**result, "path": path}


def update(conn: sqlite3.Connection, card_id: str, *,
           title: str | None = None, tldr: str | None = None,
           fields: dict | None = None, tags: list | None = None,
           authority: str | None = None, review_at: str | None = None,
           expires_at: str | None = None, expected_version: int | None = None,
           actor: str = "human", quality_extra: dict | None = None,
           commit: bool = True) -> dict:
    old = get(conn, card_id)
    if old is None:
        raise LookupError(card_id)
    if old.get("status") not in ("draft", "proposed"):
        raise ConflictError("only draft/proposed repo cards can be edited")
    if expected_version is not None and int(expected_version) != int(old["version"]):
        raise ConflictError(
            f"knowledge version changed: expected {expected_version}, "
            f"current {old['version']}")
    new_title = title if title is not None else old["title"]
    new_tldr = tldr if tldr is not None else old["tldr"]
    new_fields = knowledge_card.normalize_fields(
        fields if fields is not None else old["fields"])
    new_tags = tags if tags is not None else old.get("tags") or []
    new_authority = authority or old.get("authority") or "reference"
    new_review = review_at if review_at is not None else old.get("review_at")
    new_expires = expires_at if expires_at is not None else old.get("expires_at")
    body_md = knowledge_card.render_markdown(
        new_title, new_tldr, old["kind"], new_fields)
    digest = knowledge_card.content_hash(new_title, new_tldr, body_md)
    temp = {
        **old, "title": new_title, "tldr": new_tldr, "body_md": body_md,
        "fields": new_fields, "tags": new_tags, "authority": new_authority,
        "review_at": new_review, "expires_at": new_expires,
        "content_hash": digest,
    }
    quality = knowledge_quality.evaluate_row(temp, "repo_knowledge")
    if quality_extra:
        quality.update(quality_extra)
    now = now_iso()
    version = int(old["version"]) + 1
    conn.execute(
        "UPDATE repo_knowledge SET title=?, tldr=?, body_md=?, fields_json=?, "
        "tags_json=?, authority=?, content_hash=?, version=?, completeness=?, "
        "quality_json=?, quality_score=?, quality_checked_at=?, review_at=?, "
        "expires_at=?, updated_at=? WHERE id=?",
        (new_title, new_tldr, body_md, jdumps(new_fields), jdumps(new_tags),
         new_authority, digest, version,
         knowledge_card.completeness(old["kind"], new_fields), jdumps(quality),
         quality["score"], quality["checked_at"], new_review, new_expires, now,
         card_id),
    )
    current = get(conn, card_id)
    assert current is not None
    path = _write_card_file(current["repo_id"], current, current["status"])
    _snapshot(conn, card_id, version, current, actor)
    conn.commit()
    return {**current, "path": path}


def approve(conn: sqlite3.Connection, card_id: str, *,
            expected_version: int | None = None, actor: str = "human",
            commit: bool = True) -> dict:
    card = get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card["status"] not in ("draft", "proposed"):
        raise ConflictError(
            f"only draft/proposed repo cards can be approved, current {card['status']}")
    if card.get("conflicts"):
        raise ConflictError(
            "unresolved conflicts with: " + ", ".join(card["conflicts"]))
    if expected_version is not None and int(expected_version) != int(card["version"]):
        raise ConflictError(
            f"knowledge version changed: expected {expected_version}, "
            f"current {card['version']}")
    check = knowledge_card.validate_card(
        card["kind"], card["fields"], for_approval=True)
    quality = knowledge_quality.evaluate_row(card, "repo_knowledge")
    blocking = list(dict.fromkeys(check["errors"] + quality.get("blocking", [])))
    if blocking:
        raise knowledge_quality.QualityGateError(
            "; ".join(blocking), quality)
    now = now_iso()
    version = int(card["version"]) + 1
    conn.execute(
        "UPDATE repo_knowledge SET status='approved', approved_at=?, approved_by=?, "
        "version=?, completeness=?, quality_json=?, quality_score=?, "
        "quality_checked_at=?, updated_at=? WHERE id=?",
        (now, actor, version, check["completeness"], jdumps(quality),
         quality["score"], quality["checked_at"], now, card_id),
    )
    approved = get(conn, card_id)
    assert approved is not None
    path = _write_card_file(approved["repo_id"], approved, "approved")
    _snapshot(conn, card_id, version, approved, actor)
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, approved.get("source_task_id"), "knowledge.approve",
         jdumps({"table": "repo_knowledge", "card_id": card_id,
                 "repo_id": approved.get("repo_id"),
                 "quality_score": quality["score"]}), now))
    if commit:
        conn.commit()
    return {**approved, "path": path}


def _snapshot(conn: sqlite3.Connection, card_id: str, version: int,
              card: dict, actor: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO repo_knowledge_versions "
        "(knowledge_id, version, title, tldr, body_md, fields_json, authority, "
        " status, source_task_id, source_step_id, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (card_id, version, card.get("title"), card.get("tldr"),
         card.get("body_md"), jdumps(card.get("fields") or {}),
         card.get("authority"), card.get("status"), card.get("source_task_id"),
         card.get("source_step_id"), now_iso(), actor),
    )


def reject(conn: sqlite3.Connection, card_id: str, *,
           actor: str = "human", commit: bool = True) -> dict:
    card = get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    conn.execute(
        "UPDATE repo_knowledge SET status='rejected', updated_at=? WHERE id=?",
        (now_iso(), card_id))
    if commit:
        conn.commit()
    return get(conn, card_id) or {}


def deprecate(conn: sqlite3.Connection, card_id: str, *,
              actor: str = "human", commit: bool = True) -> dict:
    if get(conn, card_id) is None:
        raise LookupError(card_id)
    conn.execute(
        "UPDATE repo_knowledge SET status='deprecated', updated_at=? WHERE id=?",
        (now_iso(), card_id))
    if commit:
        conn.commit()
    return get(conn, card_id) or {}


def render_for_task(conn: sqlite3.Connection, task: dict, repo_ids: list[str], *,
                    budget_chars: int = 2000, limit: int = 8) -> str:
    """渲染任务可见的主题知识卡（只取 approved、未过期、无冲突）。"""
    ids = [str(x) for x in (repo_ids or []) if x]
    if not ids:
        return ""
    clauses = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM repo_knowledge WHERE status='approved' AND repo_id IN ({clauses}) "
        "AND (expires_at IS NULL OR expires_at='' OR expires_at > ?) "
        "AND (conflicts_json IS NULL OR conflicts_json IN ('', '[]')) "
        "ORDER BY hit_count DESC, updated_at DESC LIMIT ?",
        [*ids, now_iso(), max(1, min(50, int(limit or 8)))]).fetchall()
    if not rows:
        return ""
    lines = ["## 主题知识库（已批准）"]
    used = 0
    for row in rows:
        card = _row(row) or {}
        tags = ",".join(card.get("tags") or [])[:80]
        label = "约束（必须遵守）" if card.get("authority") == "normative" else "参考"
        line = (f"- [{label} · {card.get('kind')}"
                + (f" · tags={tags}" if tags else "")
                + f"] {card.get('title')}：{card.get('tldr')} "
                  f"(card:{card.get('id')})")
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
        conn.execute(
            "UPDATE repo_knowledge SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
            (now_iso(), card.get("id")))
    conn.commit()
    return "\n".join(lines) if len(lines) > 1 else ""


def record_hits(conn: sqlite3.Connection, card_ids: list[str]) -> int:
    ids = list(dict.fromkeys(str(x) for x in card_ids if x))
    if not ids:
        return 0
    now = now_iso()
    for cid in ids:
        conn.execute(
            "UPDATE repo_knowledge SET hit_count=COALESCE(hit_count,0)+1, "
            "last_hit_at=? WHERE id=?", (now, cid))
    conn.commit()
    return len(ids)


def reindex(conn: sqlite3.Connection, repo_id: str | None = None) -> dict:
    """从文件重建索引（文件是真相）。"""
    roots = [repo_dir(repo_id)] if repo_id else (
        [p for p in Path(config.KNOWLEDGE_REPOS_DIR).iterdir() if p.is_dir()]
        if Path(config.KNOWLEDGE_REPOS_DIR).exists() else [])
    scanned = 0
    upserted = 0
    for root in roots:
        rid = root.name
        for folder in ("cards", "proposed"):
            for path in (root / folder).glob("*.md"):
                scanned += 1
                raw = path.read_text(encoding="utf-8")
                meta = _parse_meta(raw)
                if not meta.get("id"):
                    continue
                body = _strip_meta(raw)
                status = str(meta.get("status") or "proposed")
                fields = knowledge_card.normalize_fields(meta.get("fields") or {})
                completeness = knowledge_card.completeness(
                    meta.get("kind") or "note", fields)
                card = {
                    "id": meta["id"], "repo_id": rid, "repo": meta.get("repo") or rid,
                    "kind": meta.get("kind") or "note",
                    "authority": meta.get("authority") or "reference",
                    "status": status, "title": meta.get("title") or "",
                    "tldr": meta.get("tldr") or "", "body_md": body,
                    "fields": fields, "tags": meta.get("tags") or [],
                    "source_task_id": meta.get("source_task_id"),
                    "source_step_id": meta.get("source_step_id"),
                    "source_role": meta.get("source_role"),
                    "source_event": meta.get("source_event"),
                    "content_hash": meta.get("content_hash"),
                    "version": int(meta.get("version") or 1),
                    "confidence": float(meta.get("confidence") or 0.5),
                    "review_at": meta.get("review_at"),
                    "expires_at": meta.get("expires_at"),
                    "created_at": meta.get("created_at") or now_iso(),
                }
                quality = knowledge_quality.evaluate_row(card, "repo_knowledge")
                conn.execute(
                    "INSERT INTO repo_knowledge "
                    "(id, repo_id, repo, kind, authority, status, title, tldr, body_md, "
                    " fields_json, tags_json, source_task_id, source_step_id, source_role, "
                    " source_event, provenance_json, content_hash, version, confidence, "
                    " completeness, quality_json, quality_score, quality_checked_at, "
                    " review_at, expires_at, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                    "title=excluded.title, tldr=excluded.tldr, body_md=excluded.body_md, "
                    "fields_json=excluded.fields_json, tags_json=excluded.tags_json, "
                    "completeness=excluded.completeness, quality_json=excluded.quality_json, "
                    "quality_score=excluded.quality_score, "
                    "quality_checked_at=excluded.quality_checked_at, "
                    "review_at=excluded.review_at, expires_at=excluded.expires_at, "
                    "updated_at=excluded.updated_at",
                    (meta["id"], rid, meta.get("repo") or rid,
                     meta.get("kind") or "note", meta.get("authority") or "reference",
                     status, meta.get("title") or "", meta.get("tldr") or "", body,
                     jdumps(fields), jdumps(meta.get("tags") or []),
                     meta.get("source_task_id"), meta.get("source_step_id"),
                     meta.get("source_role"), meta.get("source_event"), "{}",
                     meta.get("content_hash"), int(meta.get("version") or 1),
                     float(meta.get("confidence") or 0.5), completeness,
                     jdumps(quality), quality["score"], quality["checked_at"],
                     meta.get("review_at"), meta.get("expires_at"),
                     meta.get("created_at") or now_iso(), now_iso()),
                )
                upserted += 1
    conn.commit()
    return {"scanned": scanned, "upserted": upserted}


def _parse_meta(raw: str) -> dict:
    marker = "<!-- anvil: "
    if not raw.startswith(marker):
        return {}
    end = raw.find(" -->", len(marker))
    if end < 0:
        return {}
    try:
        return json.loads(raw[len(marker):end])
    except json.JSONDecodeError:
        return {}


def _strip_meta(raw: str) -> str:
    marker = "<!-- anvil: "
    if not raw.startswith(marker):
        return raw
    end = raw.find(" -->", len(marker))
    return raw[end + 4:].lstrip("\n") if end >= 0 else raw
