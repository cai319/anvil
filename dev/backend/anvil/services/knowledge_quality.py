"""知识质量门：确定性评估、持久化、主题库质量/构成汇总。

LLM 不在这里判定有效性；这里只做规则检查，LLM 清洗在 knowledge_clean。
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from ..util import ConflictError, jdumps, jloads, now_iso
from . import knowledge_card

class QualityGateError(ConflictError):
    """审批被质量门阻断；携带结构化 quality 供 API/UI 展示。"""

    def __init__(self, message: str, quality: dict):
        super().__init__(message)
        self.quality = quality


_TABLES = ("repo_knowledge", "task_knowledge", "workspace_knowledge")
_TABLE_QUALITY = {
    "repo_knowledge": ("quality_json", "quality_score", "quality_checked_at"),
    "task_knowledge": ("quality_json", "quality_score", "quality_checked_at"),
    "workspace_knowledge": ("quality_json", "quality_score", "quality_checked_at"),
}


def evaluate_row(card: dict, table: str | None = None) -> dict:
    """从 DB 行/卡片 dict 计算质量，不写库。"""
    fields = card.get("fields")
    if fields is None:
        fields = jloads(card.get("fields_json") or "{}", {})
    status = str(card.get("status") or "proposed")
    checked_at = card.get("quality_checked_at")
    legacy = bool(status == "approved" and not checked_at)
    quality = knowledge_card.evaluate_quality(
        str(card.get("kind") or ""),
        str(card.get("title") or ""),
        str(card.get("tldr") or ""),
        str(card.get("body_md") or ""),
        fields,
        authority=str(card.get("authority") or "reference"),
        status=status,
        source_task_id=card.get("source_task_id"),
        source_step_id=card.get("source_step_id"),
        source_role=card.get("source_role"),
        review_at=card.get("review_at"),
        expires_at=card.get("expires_at"),
        legacy=legacy,
    )
    stored = card.get("quality")
    if not isinstance(stored, dict):
        stored = jloads(card.get("quality_json") or "{}", {})
    llm = stored.get("llm") if isinstance(stored, dict) else None
    if llm:
        quality["llm"] = llm
    quality["legacy"] = legacy
    return quality


def enrich(rows: list[dict], table: str | None = None) -> list[dict]:
    """给列表行附加 quality/legacy；不改变数据库。"""
    for row in rows:
        if not isinstance(row.get("fields"), dict):
            row["fields"] = jloads(row.get("fields_json") or "{}", {})
        if not isinstance(row.get("tags"), list):
            row["tags"] = jloads(row.get("tags_json") or "[]", [])
        quality = evaluate_row(row, table)
        row["quality"] = quality
        row["legacy"] = bool(quality.get("legacy"))
        if row.get("completeness") is None:
            row["completeness"] = knowledge_card.completeness(
                str(row.get("kind") or ""), row["fields"])
    return rows


def persist(conn: sqlite3.Connection, table: str, card_id: str,
            quality: dict, *, extra: dict | None = None,
            commit: bool = False) -> dict:
    """持久化质量缓存；extra 用于合并 LLM 清洗元数据。"""
    if table not in _TABLE_QUALITY:
        raise ValueError(f"unsupported knowledge table: {table}")
    q = dict(quality)
    if extra:
        q.update(extra)
    json_col, score_col, checked_col = _TABLE_QUALITY[table]
    conn.execute(
        f"UPDATE {table} SET {json_col}=?, {score_col}=?, {checked_col}=? WHERE id=?",
        (jdumps(q), float(q.get("score") or 0), q.get("checked_at") or now_iso(),
         card_id))
    if commit:
        conn.commit()
    return q


def refresh(conn: sqlite3.Connection, table: str | None = None) -> dict:
    """重新计算并写回质量缓存；不改变审批状态。"""
    tables = (table,) if table else _TABLES
    scanned = updated = 0
    for name in tables:
        if name not in _TABLE_QUALITY:
            continue
        rows = conn.execute(f"SELECT * FROM {name}").fetchall()
        for raw in rows:
            row = dict(raw)
            scanned += 1
            quality = evaluate_row(row, name)
            persist(conn, name, str(row["id"]), quality, commit=False)
            updated += 1
    conn.commit()
    return {"scanned": scanned, "updated": updated}


def _feedback_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    try:
        rows = conn.execute(
            "SELECT card_table, card_id, signal, COUNT(*) AS n "
            "FROM knowledge_feedback GROUP BY card_table, card_id, signal"
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[tuple[str, str], dict] = defaultdict(dict)
    for row in rows:
        out[(str(row["card_table"]), str(row["card_id"]))][str(row["signal"])] = int(row["n"])
    return out


FEEDBACK_SIGNALS = ("applied", "helpful", "unhelpful", "no_effect")
REVIEW_DAYS = {
    "convention": 90, "decision": 180, "workflow": 180,
    "insight": 365, "role_requirement": 180, "task_finding": 180,
    "personal_lesson": 180,
}


def default_review_at(kind: str) -> str:
    from datetime import datetime, timedelta, timezone
    days = int(REVIEW_DAYS.get(str(kind or ""), 180))
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def add_feedback(conn: sqlite3.Connection, card_table: str, card_id: str,
                 signal: str, *, task_id: str | None = None,
                 run_id: str | None = None, role_slug: str | None = None,
                 note: str = "") -> dict:
    if signal not in FEEDBACK_SIGNALS:
        raise ValueError(f"invalid feedback signal: {signal}")
    from ..util import new_id
    fid = new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO knowledge_feedback "
        "(id, card_table, card_id, task_id, run_id, role_slug, signal, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, card_table, card_id, task_id, run_id, role_slug, signal,
         str(note or "")[:2000], now))
    conn.commit()
    return {"id": fid, "card_table": card_table, "card_id": card_id,
            "signal": signal, "task_id": task_id, "run_id": run_id,
            "role_slug": role_slug, "note": note, "created_at": now}


def review_candidates(conn: sqlite3.Connection, repo: str | None = None,
                      limit: int = 50) -> list[dict]:
    from . import knowledge_repos
    repo_id = None
    if repo:
        row = knowledge_repos.get(conn, repo)
        repo_id = row["id"] if row else knowledge_repos.repo_key(repo)
    sql = "SELECT * FROM repo_knowledge WHERE status='approved'"
    params: list = []
    if repo_id:
        sql += " AND repo_id=?"
        params.append(repo_id)
    sql += " ORDER BY COALESCE(review_at, updated_at) ASC LIMIT ?"
    params.append(max(1, min(200, int(limit or 50))))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    enrich(rows, "repo_knowledge")
    feedback = _feedback_counts(conn)
    today = now_iso()[:10]
    out = []
    for row in rows:
        reasons = []
        review_at = str(row.get("review_at") or "")[:10]
        if review_at and review_at <= today:
            reasons.append("review_due")
        if row.get("expires_at") and str(row["expires_at"]) <= now_iso():
            reasons.append("expired")
        if row.get("legacy"):
            reasons.append("legacy")
        signals = feedback.get(("repo_knowledge", str(row.get("id"))), {})
        if signals.get("unhelpful", 0) > signals.get("helpful", 0):
            reasons.append("negative_feedback")
        if not row.get("hit_count") and not reasons:
            reasons.append("never_hit")
        if reasons:
            out.append({**row, "review_reasons": reasons, "feedback": signals})
    return out


def review_action(conn: sqlite3.Connection, card_id: str, action: str, *,
                  actor: str = "human", days: int | None = None) -> dict:
    from . import repo_knowledge
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if action not in ("keep", "deprecate", "refresh"):
        raise ValueError(f"invalid review action: {action}")
    if action == "deprecate":
        return repo_knowledge.deprecate(conn, card_id, actor=actor)
    days = int(days or REVIEW_DAYS.get(str(card.get("kind") or ""), 180))
    from datetime import datetime, timedelta, timezone
    review_at = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
    conn.execute(
        "UPDATE repo_knowledge SET review_at=?, updated_at=? WHERE id=?",
        (review_at, now_iso(), card_id))
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, card.get("source_task_id"), f"knowledge.review.{action}",
         jdumps({"card_id": card_id, "review_at": review_at}), now_iso()))
    conn.commit()
    return {**repo_knowledge.get(conn, card_id), "review_at": review_at}


def summary(conn: sqlite3.Connection, repo: str | None = None) -> dict:
    """主题知识库质量 + 数据构成；也返回任务/频道卡片的全局质量计数。"""
    from . import knowledge_repos

    repo_filter = None
    if repo:
        row = knowledge_repos.get(conn, repo)
        repo_filter = row["id"] if row else knowledge_repos.repo_key(repo)
    sql = "SELECT * FROM repo_knowledge"
    params: list = []
    if repo_filter:
        sql += " WHERE repo_id=?"
        params.append(repo_filter)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    enrich(rows, "repo_knowledge")
    feedback = _feedback_counts(conn)
    today = now_iso()[:10]
    hash_counts = Counter(str(r.get("content_hash") or "") for r in rows
                          if r.get("content_hash"))
    duplicate_hashes = {h for h, n in hash_counts.items() if n > 1}

    buckets: dict[str, dict] = {}
    for row in rows:
        repo_id = str(row.get("repo_id") or "")
        bucket = buckets.setdefault(repo_id, {
            "repo_id": repo_id,
            "repo": row.get("repo") or repo_id,
            "total": 0, "approved": 0, "proposed": 0, "rejected": 0,
            "legacy": 0, "duplicates": 0, "review_due": 0,
            "scores": [], "kind_counts": Counter(),
            "use_when": 0, "expected_effect": 0, "evidence": 0,
            "approved_use_when": 0, "approved_expected_effect": 0,
            "approved_evidence": 0,
            "missing": Counter(), "feedback": Counter(),
        })
        bucket["total"] += 1
        status = str(row.get("status") or "")
        if status in bucket:
            bucket[status] += 1
        if row.get("legacy"):
            bucket["legacy"] += 1
        if str(row.get("content_hash") or "") in duplicate_hashes:
            bucket["duplicates"] += 1
        review_at = str(row.get("review_at") or "")[:10]
        expires_at = str(row.get("expires_at") or "")
        if (review_at and review_at <= today) or (expires_at and expires_at <= now_iso()):
            bucket["review_due"] += 1
        quality = row.get("quality") or {}
        bucket["scores"].append(int(quality.get("score") or 0))
        bucket["kind_counts"][str(row.get("kind") or "unknown")] += 1
        for canonical in knowledge_card.QUALITY_REQUIRED:
            _key, value = knowledge_card.resolve_quality_field(
                row.get("fields") or {}, canonical)
            if value:
                bucket[canonical] += 1
                if status == "approved":
                    bucket[f"approved_{canonical}"] += 1
            else:
                bucket["missing"][canonical] += 1
        for signal, count in feedback.get(("repo_knowledge", str(row.get("id"))), {}).items():
            bucket["feedback"][signal] += int(count)

    repos = []
    for bucket in buckets.values():
        total = bucket["total"] or 1
        approved = bucket["approved"] or 1
        repos.append({
            "repo_id": bucket["repo_id"],
            "repo": bucket["repo"],
            "total": bucket["total"],
            "approved": bucket["approved"],
            "proposed": bucket["proposed"],
            "rejected": bucket["rejected"],
            "legacy": bucket["legacy"],
            "duplicates": bucket["duplicates"],
            "review_due": bucket["review_due"],
            "avg_score": round(sum(bucket["scores"]) / total, 1) if bucket["scores"] else 0,
            "coverage": {
                "use_when": round(bucket["use_when"] / total, 4),
                "expected_effect": round(bucket["expected_effect"] / total, 4),
                "evidence": round(bucket["evidence"] / total, 4),
            },
            "missing": dict(bucket["missing"]),
            "kind_counts": dict(bucket["kind_counts"]),
            "feedback": dict(bucket["feedback"]),
            "approved_coverage": {
                "use_when": round(bucket["approved_use_when"] / approved, 4),
                "expected_effect": round(
                    bucket["approved_expected_effect"] / approved, 4),
                "evidence": round(bucket["approved_evidence"] / approved, 4),
            } if bucket["approved"] else {},
        })
    repos.sort(key=lambda item: (-item["total"], item["repo"].casefold()))

    other = {}
    for table in ("task_knowledge", "workspace_knowledge"):
        try:
            table_rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM {table}").fetchall()]
        except sqlite3.Error:
            table_rows = []
        enrich(table_rows, table)
        other[table] = {
            "total": len(table_rows),
            "approved_or_active": sum(
                1 for r in table_rows if r.get("status") in ("approved", "active")),
            "avg_score": round(
                sum(int((r.get("quality") or {}).get("score") or 0)
                    for r in table_rows) / len(table_rows), 1) if table_rows else 0,
            "legacy": sum(1 for r in table_rows if r.get("legacy")),
        }

    return {
        "repos": repos,
        "totals": {
            "repos": len(repos),
            "cards": sum(item["total"] for item in repos),
            "approved": sum(item["approved"] for item in repos),
            "proposed": sum(item["proposed"] for item in repos),
            "avg_score": round(
                sum(item["avg_score"] * item["total"] for item in repos)
                / max(1, sum(item["total"] for item in repos)), 1),
            **{f"other_{k}": v for k, v in other.items()},
        },
        "generated_at": now_iso(),
    }
