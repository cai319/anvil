"""频道知识：知识卡 CRUD、审批、版本快照、冲突/老化、从任务收割。"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from ..util import jdumps, jloads, new_id, now_iso
from . import knowledge_card
from .knowledge import tokenize_for_match
from .tasks import ConflictError, get as get_task

# 复核周期（天）：到期进入「知识复盘」，不自动删除。
REVIEW_DAYS = {
    "convention": 90,
    "decision": 180,
    "workflow": 180,
    "insight": 365,
    "role_requirement": 180,
    "task_finding": 180,
    "personal_lesson": 180,
}
# 相反模态词：高文本重叠 + 相反模态 → 疑似矛盾，交 human 裁决。
_MODAL_PAIRS = (
    ("必须", "禁止"), ("必须", "不得"), ("允许", "禁止"),
    ("启用", "禁用"), ("应该", "不应"), ("推荐", "避免"),
)
_COMPACT_RE = re.compile(r"\s+")


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["fields"] = jloads(d.pop("fields_json", "{}"), {})
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    d["related"] = jloads(d.pop("related_json", "[]"), [])
    d["conflicts"] = jloads(d.pop("conflicts_json", "[]"), [])
    d["source_knowledge"] = jloads(d.pop("source_knowledge_json", "[]"), [])
    d["is_expired"] = _is_expired(d.get("expires_at"))
    d["freshness"] = freshness(d)
    return d


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text + "T00:00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_days(value, now: datetime | None = None) -> float:
    ts = _parse_ts(value)
    if ts is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 86400)


def _is_expired(value) -> bool:
    ts = _parse_ts(value)
    return bool(ts and ts <= datetime.now(timezone.utc))


def freshness(card: dict, now: datetime | None = None) -> float:
    """新鲜度 0–1，仅用于排序/复盘，不自动删除。

    recency 指数衰减（90 天半衰期近似）、命中次数对数归一、完整度；
    outcome_signal 暂无跨任务因果数据，取中性 0.5。
    """
    age = _age_days(card.get("created_at"), now)
    recency = math.exp(-age / 90.0)
    hits = math.log1p(float(card.get("hit_count") or 0)) / math.log(51.0)
    outcome = 0.5
    completeness = min(1.0, max(0.0, float(card.get("completeness") or 0.0)))
    return round(0.4 * recency + 0.25 * min(1.0, hits)
                 + 0.2 * outcome + 0.15 * completeness, 4)


def _default_review_at(kind: str) -> str:
    days = REVIEW_DAYS.get(kind, 180)
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def _shingles(text: str) -> set[str]:
    compact = _COMPACT_RE.sub("", str(text or "").lower())
    grams = {compact[i:i + 4] for i in range(0, max(0, len(compact) - 3))}
    return grams | set(tokenize_for_match(text))


def _similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _has_opposite_modality(a: str, b: str) -> bool:
    for left, right in _MODAL_PAIRS:
        if (left in a and right in b) or (right in a and left in b):
            return True
    return False


def _card_text(card: dict) -> str:
    return " ".join([
        str(card.get("title") or ""),
        str(card.get("tldr") or ""),
        str(card.get("body_md") or ""),
        " ".join(str(v) for v in (card.get("fields") or {}).values()),
    ])


def detect_conflicts(conn: sqlite3.Connection, workspace_id: str, *,
                     exclude_id: str, kind: str, title: str, tldr: str,
                     body_md: str, fields: dict, tags: list[str],
                     supersedes_id: str | None = None) -> list[dict]:
    """重复/矛盾检测：返回 [{id, title, reason, similarity}]，不自动合并。"""
    text = " ".join([title, tldr, body_md]
                    + [str(v) for v in (fields or {}).values()])
    tag_set = {str(t).lower() for t in (tags or [])}
    rows = conn.execute(
        "SELECT * FROM workspace_knowledge WHERE workspace_id=? AND id<>? "
        "AND status IN ('draft','proposed','approved') "
        "ORDER BY created_at DESC LIMIT 300",
        (workspace_id, exclude_id),
    ).fetchall()
    found: list[dict] = []
    for row in rows:
        other = _row(row)
        if other is None:
            continue
        # 新提案本就可能与被取代的旧卡高度相似，不应判为冲突。
        if supersedes_id and other["id"] == supersedes_id:
            continue
        other_tags = {str(t).lower() for t in other.get("tags") or []}
        if other["kind"] != kind and not (tag_set & other_tags):
            continue
        sim = _similarity(text, _card_text(other))
        if sim < 0.2:
            continue
        contradiction = sim >= 0.2 and _has_opposite_modality(text, _card_text(other))
        if contradiction:
            found.append({"id": other["id"], "title": other["title"],
                          "reason": "contradiction", "similarity": round(sim, 3)})
        elif sim >= 0.62:
            found.append({"id": other["id"], "title": other["title"],
                          "reason": "duplicate", "similarity": round(sim, 3)})
    return found


def _replace_conflicts(conn: sqlite3.Connection, workspace_id: str, knowledge_id: str,
                       conflicts: list[dict]) -> None:
    """重建冲突链接：先移除本卡在其它卡中的引用，再按检测结果双向标记矛盾。"""
    for row in conn.execute(
        "SELECT id, conflicts_json FROM workspace_knowledge "
        "WHERE workspace_id=? AND id<>?", (workspace_id, knowledge_id)
    ).fetchall():
        ids = jloads(row["conflicts_json"], [])
        if knowledge_id in ids:
            ids = [i for i in ids if i != knowledge_id]
            conn.execute("UPDATE workspace_knowledge SET conflicts_json=? WHERE id=?",
                         (jdumps(ids), row["id"]))
    conn.execute("UPDATE workspace_knowledge SET conflicts_json=? WHERE id=?",
                 (jdumps([c["id"] for c in conflicts]), knowledge_id))
    for conflict in conflicts:
        if conflict["reason"] != "contradiction":
            continue
        other = get(conn, conflict["id"])
        if other is None:
            continue
        ids = list(dict.fromkeys((other.get("conflicts") or []) + [knowledge_id]))
        conn.execute("UPDATE workspace_knowledge SET conflicts_json=? WHERE id=?",
                     (jdumps(ids), conflict["id"]))


def _detach_conflicts(conn: sqlite3.Connection, workspace_id: str, knowledge_id: str) -> None:
    """拒绝/删除/裁决后，从双方冲突列表中移除该卡。"""
    _replace_conflicts(conn, workspace_id, knowledge_id, [])


def get(conn: sqlite3.Connection, knowledge_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM workspace_knowledge WHERE id=?", (knowledge_id,)
    ).fetchone())


def list_for_workspace(conn: sqlite3.Connection, workspace_id: str, *,
                       status: str | None = None, kind: str | None = None,
                       sort: str | None = None) -> list[dict]:
    clauses = ["workspace_id=?"]
    params: list = [workspace_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    rows = conn.execute(
        f"SELECT * FROM workspace_knowledge WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC",
        params,
    ).fetchall()
    cards = [_row(r) for r in rows]  # type: ignore[list-item]
    if sort == "freshness":
        cards.sort(key=lambda c: (-(c["freshness"] or 0), c["created_at"]))
    return cards


def _like_pattern(text: str) -> str:
    escaped = (str(text).replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


def list_all(conn: sqlite3.Connection, *, status: str | None = None,
             kind: str | None = None, workspace_id: str | None = None,
             q: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    """跨频道知识列表（人工审计视图）。"""
    limit = max(1, min(500, int(limit or 200)))
    offset = max(0, int(offset or 0))
    clauses: list[str] = []
    params: list = []
    if status and status != "all":
        clauses.append("wk.status=?")
        params.append(status)
    if kind:
        clauses.append("wk.kind=?")
        params.append(kind)
    if workspace_id:
        clauses.append("wk.workspace_id=?")
        params.append(workspace_id)
    if q and str(q).strip():
        pattern = _like_pattern(str(q).strip())
        clauses.append(
            "(wk.title LIKE ? ESCAPE '\\' OR wk.tldr LIKE ? ESCAPE '\\' "
            "OR wk.body_md LIKE ? ESCAPE '\\' OR wk.tags_json LIKE ? ESCAPE '\\' "
            "OR COALESCE(w.name, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 5)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM workspace_knowledge wk "
        "LEFT JOIN workspaces w ON w.id=wk.workspace_id "
        f"{where}", params,
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT wk.*, w.name AS workspace_name FROM workspace_knowledge wk "
        "LEFT JOIN workspaces w ON w.id=wk.workspace_id "
        f"{where} "
        "ORDER BY CASE wk.status WHEN 'proposed' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, "
        "wk.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = [_row(r) for r in rows]  # type: ignore[list-item]
    return {"items": items, "total": total, "limit": limit,
            "offset": offset, "has_more": offset + len(items) < total}


def _snapshot(conn: sqlite3.Connection, knowledge: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO workspace_knowledge_versions "
        "(knowledge_id, version, title, tldr, body_md, fields_json, authority, status, "
        "source_task_id, source_subtask_id, origin, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            knowledge["id"], knowledge["version"], knowledge["title"], knowledge["tldr"],
            knowledge["body_md"], jdumps(knowledge["fields"]), knowledge["authority"],
            knowledge["status"], knowledge.get("source_task_id"),
            knowledge.get("source_subtask_id"), knowledge.get("origin", "human"),
            knowledge["created_at"], knowledge.get("created_by"),
        ),
    )


def _default_authority(kind: str) -> str:
    return "normative" if kind == "convention" else "reference"


def create(conn: sqlite3.Connection, workspace_id: str, *,
           title: str, tldr: str, kind: str,
           authority: str | None = None,
           fields: dict | None = None,
           tags: list[str] | None = None,
           source_task_id: str | None = None,
           source_subtask_id: str | None = None,
           owner: str | None = None,
           review_at: str | None = None,
           expires_at: str | None = None,
           related: list[str] | None = None,
           supersedes_id: str | None = None,
           actor: str | None = "human",
           origin: str = "human",
           status: str | None = None) -> dict:
    if kind not in knowledge_card.KINDS:
        raise ConflictError(f"unknown knowledge kind: {kind}")
    fields = knowledge_card.normalize_fields(fields)
    body_md = knowledge_card.render_markdown(title, tldr, kind, fields)
    digest = knowledge_card.content_hash(title, tldr, body_md)
    if source_task_id:
        existing = conn.execute(
            "SELECT * FROM workspace_knowledge WHERE workspace_id=? AND source_task_id=? "
            "AND kind=? AND content_hash=? LIMIT 1",
            (workspace_id, source_task_id, kind, digest),
        ).fetchone()
        if existing is not None:
            return _row(existing)  # type: ignore[return-value]
    authority = authority or _default_authority(kind)
    if authority not in ("normative", "reference"):
        raise ConflictError(f"invalid authority: {authority}")
    if status is None:
        status = "approved" if actor in (None, "human") else "proposed"
    if status not in ("draft", "proposed", "approved", "rejected", "deprecated", "expired"):
        raise ConflictError(f"invalid status: {status}")
    from . import workspace as workspace_service
    ws = workspace_service.get_channel(conn, workspace_id)
    if ws is None:
        raise LookupError(workspace_id)
    repo = ws.get("knowledge_repo")
    for_approval = status == "approved"
    if for_approval:
        check = knowledge_card.validate_card(kind, fields, for_approval=True)
        if check["errors"]:
            raise ConflictError("; ".join(check["errors"]))
    check = knowledge_card.validate_card(kind, fields, for_approval=False)
    kid = new_id()
    now = now_iso()
    if not review_at:
        review_at = _default_review_at(kind)
    conn.execute(
        "INSERT INTO workspace_knowledge "
        "(id, workspace_id, repo, kind, authority, title, tldr, body_md, fields_json, "
        "tags_json, source_task_id, source_subtask_id, source_knowledge_json, owner, "
        "related_json, supersedes_id, content_hash, status, version, created_by, origin, "
        "created_at, updated_at, approved_at, approved_by, expires_at, review_at, "
        "conflicts_json, completeness, template_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            kid, workspace_id, repo, kind, authority, title, tldr, body_md,
            jdumps(fields), jdumps(tags or []), source_task_id, source_subtask_id, "[]",
            owner, jdumps(related or []), supersedes_id, digest, status or "proposed", 1,
            actor, origin, now, now,
            now if status == "approved" else None,
            actor if status == "approved" else None,
            expires_at, review_at, "[]", check["completeness"], 1,
        ),
    )
    conflicts = detect_conflicts(
        conn, workspace_id, exclude_id=kid, kind=kind, title=title, tldr=tldr,
        body_md=body_md, fields=fields, tags=tags or [],
        supersedes_id=supersedes_id,
    )
    if conflicts:
        _replace_conflicts(conn, workspace_id, kid, conflicts)
    knowledge = get(conn, kid)
    assert knowledge is not None
    _snapshot(conn, knowledge)
    conn.commit()
    return knowledge


def update(conn: sqlite3.Connection, knowledge_id: str, *,
           expected_version: int | None = None, actor: str | None = "human",
           title: str | None = None, tldr: str | None = None, kind: str | None = None,
           authority: str | None = None, fields: dict | None = None,
           tags: list[str] | None = None, owner: str | None = None,
           review_at: str | None = None, expires_at: str | None = None,
           related: list[str] | None = None) -> dict:
    old = get(conn, knowledge_id)
    if old is None:
        raise LookupError(knowledge_id)
    if expected_version is not None and int(expected_version) != int(old["version"]):
        raise ConflictError(
            f"knowledge version changed: expected {expected_version}, current {old['version']}"
        )
    if old["status"] == "approved":
        # 已批准卡不可原地改：生成新的 proposed 卡，保留旧版本。
        return create(
            conn, old["workspace_id"],
            title=title or old["title"], tldr=tldr or old["tldr"], kind=kind or old["kind"],
            authority=authority or old["authority"],
            fields=fields if fields is not None else old["fields"],
            tags=tags if tags is not None else old["tags"],
            source_task_id=old.get("source_task_id"),
            source_subtask_id=old.get("source_subtask_id"),
            owner=owner if owner is not None else old.get("owner"),
            review_at=review_at if review_at is not None else old.get("review_at"),
            expires_at=expires_at if expires_at is not None else old.get("expires_at"),
            related=related if related is not None else old["related"],
            supersedes_id=old["id"], actor=actor, origin="human", status="proposed",
        )
    new_kind = kind or old["kind"]
    new_fields = fields if fields is not None else old["fields"]
    new_title = title or old["title"]
    new_tldr = tldr or old["tldr"]
    body_md = knowledge_card.render_markdown(new_title, new_tldr, new_kind, new_fields)
    check = knowledge_card.validate_card(new_kind, new_fields, for_approval=False)
    conn.execute(
        "UPDATE workspace_knowledge SET title=?, tldr=?, kind=?, authority=?, body_md=?, "
        "fields_json=?, tags_json=?, owner=?, review_at=?, expires_at=?, related_json=?, "
        "content_hash=?, completeness=?, version=version+1, updated_at=? WHERE id=?",
        (
            new_title, new_tldr, new_kind, authority or old["authority"], body_md,
            jdumps(knowledge_card.normalize_fields(new_fields)),
            jdumps(tags if tags is not None else old["tags"]),
            owner if owner is not None else old.get("owner"),
            review_at if review_at is not None else old.get("review_at"),
            expires_at if expires_at is not None else old.get("expires_at"),
            jdumps(related if related is not None else old["related"]),
            knowledge_card.content_hash(new_title, new_tldr, body_md),
            check["completeness"], now_iso(), knowledge_id,
        ),
    )
    conflicts = detect_conflicts(
        conn, old["workspace_id"], exclude_id=knowledge_id, kind=new_kind,
        title=new_title, tldr=new_tldr, body_md=body_md,
        fields=knowledge_card.normalize_fields(new_fields),
        tags=tags if tags is not None else old["tags"],
    )
    _replace_conflicts(conn, old["workspace_id"], knowledge_id, conflicts)
    current = get(conn, knowledge_id)
    assert current is not None
    _snapshot(conn, current)
    conn.commit()
    return current


def approve(conn: sqlite3.Connection, knowledge_id: str, *,
            expected_version: int | None = None, actor: str | None = "human") -> dict:
    current = get(conn, knowledge_id)
    if current is None:
        raise LookupError(knowledge_id)
    if current["status"] not in ("draft", "proposed"):
        raise ConflictError(f"only draft/proposed knowledge can be approved, current {current['status']}")
    if current.get("conflicts"):
        raise ConflictError(
            "unresolved conflicts with: " + ", ".join(current["conflicts"])
            + "; resolve or reject the conflicting card first"
        )
    check = knowledge_card.validate_card(current["kind"], current["fields"], for_approval=True)
    if check["errors"]:
        raise ConflictError("; ".join(check["errors"]))
    if expected_version is not None and int(expected_version) != int(current["version"]):
        raise ConflictError(
            f"knowledge version changed: expected {expected_version}, current {current['version']}"
        )
    cur = conn.execute(
        "UPDATE workspace_knowledge SET status='approved', version=version+1, "
        "approved_at=?, approved_by=?, completeness=?, updated_at=? "
        "WHERE id=? AND version=?",
        (now_iso(), actor, check["completeness"], now_iso(), knowledge_id, current["version"]),
    )
    if cur.rowcount == 0:
        raise ConflictError("knowledge version changed; reload and retry")
    approved = get(conn, knowledge_id)
    assert approved is not None
    superseded = approved.get("supersedes_id")
    if superseded:
        old = get(conn, superseded)
        if old is not None and old["status"] != "deprecated":
            conn.execute(
                "UPDATE workspace_knowledge SET status='deprecated', version=version+1, "
                "updated_at=? WHERE id=? AND version=?",
                (now_iso(), superseded, old["version"]),
            )
            old = get(conn, superseded)
            if old is not None:
                _snapshot(conn, old)
    _snapshot(conn, approved)
    conn.commit()
    return approved


def reject(conn: sqlite3.Connection, knowledge_id: str, *,
           expected_version: int | None = None, actor: str | None = "human") -> dict:
    current = get(conn, knowledge_id)
    if current is None:
        raise LookupError(knowledge_id)
    if current["status"] not in ("draft", "proposed"):
        raise ConflictError(f"only draft/proposed knowledge can be rejected, current {current['status']}")
    if expected_version is not None and int(expected_version) != int(current["version"]):
        raise ConflictError(
            f"knowledge version changed: expected {expected_version}, current {current['version']}"
        )
    _detach_conflicts(conn, current["workspace_id"], knowledge_id)
    conn.execute(
        "UPDATE workspace_knowledge SET status='rejected', conflicts_json='[]', "
        "version=version+1, updated_at=? WHERE id=? AND version=?",
        (now_iso(), knowledge_id, current["version"]),
    )
    rejected = get(conn, knowledge_id)
    assert rejected is not None
    _snapshot(conn, rejected)
    conn.commit()
    return rejected


def delete(conn: sqlite3.Connection, knowledge_id: str) -> bool:
    current = get(conn, knowledge_id)
    if current is not None:
        _detach_conflicts(conn, current["workspace_id"], knowledge_id)
    conn.execute("DELETE FROM workspace_knowledge_versions WHERE knowledge_id=?", (knowledge_id,))
    cur = conn.execute("DELETE FROM workspace_knowledge WHERE id=?", (knowledge_id,))
    conn.commit()
    return cur.rowcount > 0


def resolve_conflicts(conn: sqlite3.Connection, knowledge_id: str, *,
                      actor: str | None = "human") -> dict:
    """人工裁决：清除本卡与其它卡的冲突链接，恢复注入资格。"""
    current = get(conn, knowledge_id)
    if current is None:
        raise LookupError(knowledge_id)
    _detach_conflicts(conn, current["workspace_id"], knowledge_id)
    current = get(conn, knowledge_id)
    assert current is not None
    conn.commit()
    return current


def review_candidates(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """知识复盘：过期、复核到期、长期未命中、冲突未裁决。"""
    cards = list_for_workspace(conn, workspace_id, status="approved")
    now = datetime.now(timezone.utc)
    items: list[dict] = []
    counts: dict[str, int] = {}
    for card in cards:
        reasons: list[str] = []
        age = _age_days(card.get("created_at"), now)
        if card.get("is_expired"):
            reasons.append("expired")
        else:
            review_at = _parse_ts(card.get("review_at"))
            if review_at is not None and review_at <= now:
                reasons.append("review_due")
            elif review_at is None and age > REVIEW_DAYS.get(card["kind"], 180):
                reasons.append("stale")
        if card.get("conflicts"):
            reasons.append("conflict")
        if not card.get("hit_count") and age >= 30:
            reasons.append("low_hit")
        if not reasons:
            continue
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
        items.append({"card": card, "reasons": reasons,
                      "freshness": card["freshness"]})
    items.sort(key=lambda item: (-len(item["reasons"]), item["freshness"]))
    return {"items": items, "counts": counts, "total": len(items)}


def render_for_task(conn: sqlite3.Connection, task: dict, *,
                    budget_chars: int = 1200) -> str:
    """渲染当前任务可见的频道知识；绑定频道 → convention 为约束，否则参考。"""
    from .. import config
    if not config.CHANNEL_KNOWLEDGE_ENABLED:
        return ""
    task = dict(task or {})
    ws_id = task.get("workspace_id")
    ws = None
    if ws_id:
        ws = conn.execute(
            "SELECT repo, knowledge_repo FROM workspaces WHERE id=?", (ws_id,)
        ).fetchone()
    repos: list[str] = []
    try:
        from . import knowledge_binding
        scope = knowledge_binding.retrieval_scope(conn, task)
        repos.extend(scope.get("repos") or [])
    except Exception:  # noqa: BLE001 —— 检索范围失败不能阻断 CIC
        pass
    if task.get("repo"):
        repos.append(task["repo"])
    if ws is not None:
        if ws["repo"]:
            repos.append(ws["repo"])
        if ws["knowledge_repo"]:
            repos.append(ws["knowledge_repo"])
    repos = list(dict.fromkeys(r for r in repos if r))
    repo_clauses = []
    params: list = [now_iso()]
    if ws_id:
        repo_clauses.append("workspace_id=?")
        params.append(ws_id)
    if repos:
        repo_clauses.append("repo IN (" + ",".join("?" for _ in repos) + ")")
        params.extend(repos)
    if not repo_clauses:
        return ""
    rows = conn.execute(
        "SELECT * FROM workspace_knowledge WHERE status='approved' "
        "AND (expires_at IS NULL OR expires_at='' OR expires_at > ?) "
        "AND (conflicts_json IS NULL OR conflicts_json IN ('', '[]')) "
        f"AND ({' OR '.join(repo_clauses)}) ORDER BY created_at DESC LIMIT 40",
        params,
    ).fetchall()
    cards = [_row(r) for r in rows]
    kind_order = {"convention": 0, "workflow": 1, "role_requirement": 2,
                  "decision": 3, "insight": 4}
    def _key(card: dict):
        bound = bool(ws_id and card["workspace_id"] == ws_id)
        normative = bound and card["authority"] == "normative" and card["kind"] == "convention"
        return (0 if normative else 1, kind_order.get(card["kind"], 9),
                -float(card.get("freshness") or 0))
    cards.sort(key=_key)
    lines: list[str] = []
    used = 0
    for card in cards:
        bound = bool(ws_id and card["workspace_id"] == ws_id)
        normative = bound and card["authority"] == "normative" and card["kind"] == "convention"
        label = "约束（必须遵守）" if normative else "参考"
        line = f"- [{label}] {card['title']}：{card['tldr']}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
        card["last_hit_at"] = now_iso()
        conn.execute(
            "UPDATE workspace_knowledge SET last_hit_at=?, hit_count=hit_count+1 WHERE id=?",
            (card["last_hit_at"], card["id"]),
        )
    if not lines:
        return ""
    conn.commit()
    return (
        "## 频道知识（已批准）\n"
        "- 标记为「约束（必须遵守）」的条目必须执行；「参考」条目仅作提示。\n"
        + "\n".join(lines)
    )


def harvest_from_task(conn: sqlite3.Connection, workspace_id: str, task_id: str, *,
                      actor: str | None = "human") -> dict:
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    if task.get("workspace_id") != workspace_id:
        raise ConflictError("task does not belong to this workspace")
    if task.get("status") not in ("done", "closed"):
        raise ConflictError("only done/closed tasks can be harvested")
    parent_id = task.get("parent_id")
    source_task_id = parent_id or task_id
    source_subtask_id = task_id if parent_id else None
    title = f"{task['title']}：可复用经验"
    tldr = (task.get("description") or task["title"]).strip()[:200]
    fields = {
        "observation": task["title"],
        "evidence": task.get("description") or f"任务 {task['id']} 已完成",
        "lesson": task.get("description") or task["title"],
        "applicability": "、".join(task.get("tags") or []) or task.get("repo") or "当前频道",
        "counterexamples": "暂无已知反例",
    }
    status = "approved" if actor in (None, "human") else "proposed"
    return create(
        conn, workspace_id, title=title, tldr=tldr, kind="insight",
        authority="reference", fields=fields, tags=task.get("tags") or [],
        source_task_id=source_task_id, source_subtask_id=source_subtask_id,
        owner=actor, actor=actor, origin="human" if status == "approved" else "system",
        status=status,
    )
