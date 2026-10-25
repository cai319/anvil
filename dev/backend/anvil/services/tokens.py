"""Token 消耗统计：来自 CLI result.usage（输入/输出/cache），替代不准确的成本口径。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..util import jdumps, now_iso


def record(conn: sqlite3.Connection, role: str | None, task_id: str | None,
           run_id: str | None, usage: dict) -> None:
    def n(key: str) -> int:
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    conn.execute(
        "INSERT INTO token_usage (role_slug, task_id, run_id, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
        (role, task_id, run_id, n("input_tokens"), n("output_tokens"),
         n("cache_read_input_tokens"), n("cache_creation_input_tokens"),
         now_iso()),
    )
    conn.commit()


def reset_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='token_reset_at'").fetchone()
    return row["value"] if row and row["value"] else None


def reset(conn: sqlite3.Connection) -> dict:
    """Reset the displayed global counter without deleting usage history."""
    previous = reset_at(conn)
    now = now_iso()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('token_reset_at', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (now, now))
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("human", None, "tokens.reset",
         jdumps({"previous_reset_at": previous, "reset_at": now}), now))
    conn.commit()
    return {"reset_at": now, "previous_reset_at": previous}


def summary(conn: sqlite3.Connection, scope: str | None = None,
            date_from: str | None = None, date_to: str | None = None,
            since_reset: bool = False) -> dict:
    clauses, params = [], []
    reset = reset_at(conn) if since_reset else None
    if reset:
        clauses.append("recorded_at >= ?")
        params.append(reset)
    if scope:
        clauses.append("role_slug=?")
        params.append(scope)
    if date_from:
        clauses.append("recorded_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("recorded_at <= ?")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def agg(row) -> dict:
        inp = int(row["input"] or 0) + int(row["cache_read"] or 0) + int(row["cache_creation"] or 0)
        out = int(row["output"] or 0)
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                "cache_read_tokens": int(row["cache_read"] or 0),
                "cache_creation_tokens": int(row["cache_creation"] or 0)}

    total_row = conn.execute(
        f"""SELECT COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output,
                   COALESCE(SUM(cache_read_tokens),0) AS cache_read,
                   COALESCE(SUM(cache_creation_tokens),0) AS cache_creation
              FROM token_usage{where}""", params).fetchone()
    by_role = [{"role": r["role_slug"] or "unknown", **agg(r)} for r in conn.execute(
        f"""SELECT role_slug, SUM(input_tokens) AS input, SUM(output_tokens) AS output,
                   SUM(cache_read_tokens) AS cache_read, SUM(cache_creation_tokens) AS cache_creation
              FROM token_usage{where} GROUP BY role_slug ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC""",
        params)]
    by_day = [{"day": r["day"], **agg(r)} for r in conn.execute(
        f"""SELECT substr(recorded_at,1,10) AS day, SUM(input_tokens) AS input,
                   SUM(output_tokens) AS output, SUM(cache_read_tokens) AS cache_read,
                   SUM(cache_creation_tokens) AS cache_creation
              FROM token_usage{where} GROUP BY day ORDER BY day""", params)]
    today_key = datetime.now(timezone.utc).date().isoformat()
    today_row = next((d for d in by_day if d["day"] == today_key), None)
    return {"scope": scope, **agg(total_row),
            "today_total_tokens": today_row["total_tokens"] if today_row else 0,
            "reset_at": reset,
            "by_role": by_role, "by_day": by_day}
