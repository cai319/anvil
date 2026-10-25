"""Database-backed leases and fencing tokens.

SQLite 状态表仍是权威；leases 只负责“谁现在有权执行/写入”。唯一活跃租约
（partial unique index）保证多进程/多线程不会同时启动同一步骤或占用同一路径。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import config
from ..util import ConflictError, jdumps, new_id, now_iso

RESOURCE_TYPES = ("step", "slice", "path", "task")


def _ttl(ttl_seconds: float | None) -> int:
    return max(1, int(ttl_seconds or config.LEASE_TTL_SECONDS))


def _expiry(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def active(conn: sqlite3.Connection, resource_type: str,
           resource_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM leases WHERE resource_type=? AND resource_key=? "
        "AND status='active'", (resource_type, resource_key)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("expires_at") and d["expires_at"] <= now_iso():
        conn.execute("UPDATE leases SET status='expired' WHERE id=?", (d["id"],))
        return None
    return d


def acquire(conn: sqlite3.Connection, resource_type: str, resource_key: str, *,
            holder_type: str = "role", holder: str | None = None,
            run_id: str | None = None, ttl_seconds: float | None = None,
            meta: dict | None = None, commit: bool = True) -> dict:
    if resource_type not in RESOURCE_TYPES:
        raise ConflictError(f"invalid lease resource type: {resource_type}")
    now = now_iso()
    seconds = _ttl(ttl_seconds)
    existing = active(conn, resource_type, resource_key)
    if existing is not None:
        raise ConflictError(
            f"lease_held: {resource_type} {resource_key} is held by "
            f"{existing.get('holder') or existing.get('holder_type')} "
            f"until {existing.get('expires_at')}")
    fence = conn.execute(
        "SELECT COALESCE(MAX(fence), 0) + 1 AS f FROM leases "
        "WHERE resource_type=? AND resource_key=?", (resource_type, resource_key)
    ).fetchone()["f"]
    lease_id = new_id()
    try:
        conn.execute(
            "INSERT INTO leases (id, resource_type, resource_key, holder_type, holder, "
            "run_id, fence, status, acquired_at, renewed_at, expires_at, meta_json) "
            "VALUES (?,?,?,?,?,?,?, 'active', ?,?,?,?)",
            (lease_id, resource_type, resource_key, holder_type, holder, run_id,
             fence, now, now, _expiry(seconds), jdumps(meta or {})))
    except sqlite3.IntegrityError:
        raise ConflictError(
            f"lease_held: {resource_type} {resource_key} was acquired concurrently")
    if commit:
        conn.commit()
    return _row(conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()) or {}


def renew(conn: sqlite3.Connection, lease_id: str,
          ttl_seconds: float | None = None, *, commit: bool = True) -> dict:
    now = now_iso()
    row = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if row is None or row["status"] != "active":
        raise ConflictError(f"lease_not_active: {lease_id}")
    conn.execute(
        "UPDATE leases SET renewed_at=?, expires_at=? WHERE id=?",
        (now, _expiry(_ttl(ttl_seconds)), lease_id))
    if commit:
        conn.commit()
    return _row(conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()) or {}


def release(conn: sqlite3.Connection, lease_id: str | None,
            status: str = "released", *, commit: bool = True) -> None:
    if not lease_id:
        return
    conn.execute(
        "UPDATE leases SET status=? WHERE id=? AND status='active'", (status, lease_id))
    if commit:
        conn.commit()


def assert_step(conn: sqlite3.Connection, step: dict,
                run_id: str | None = None) -> None:
    """Validate the step lease/fence when the step was dispatched with one."""
    lease_id = step.get("lease_id")
    if not lease_id:
        return
    row = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if row is None or row["status"] != "active":
        raise ConflictError(f"lease_expired: step {step.get('step_id')} lease is not active")
    if row["expires_at"] and row["expires_at"] <= now_iso():
        conn.execute("UPDATE leases SET status='expired' WHERE id=?", (lease_id,))
        raise ConflictError(f"lease_expired: step {step.get('step_id')} lease expired")
    if int(step.get("fence_token") or 0) != int(row["fence"]):
        raise ConflictError(f"lease_fenced: step {step.get('step_id')} fence mismatch")
    if run_id and row["run_id"] and str(run_id) != str(row["run_id"]):
        raise ConflictError(f"lease_fenced: step {step.get('step_id')} run changed")


def bind_run(conn: sqlite3.Connection, lease_id: str | None,
             run_id: str | None, *, commit: bool = False) -> None:
    if not lease_id or not run_id:
        return
    conn.execute("UPDATE leases SET run_id=? WHERE id=? AND status='active'",
                 (run_id, lease_id))
    if commit:
        conn.commit()


def reap_expired(conn: sqlite3.Connection, *, commit: bool = True) -> int:
    cur = conn.execute(
        "UPDATE leases SET status='expired' WHERE status='active' "
        "AND expires_at IS NOT NULL AND expires_at <= ?", (now_iso(),))
    if commit:
        conn.commit()
    return cur.rowcount or 0


def for_workflow(conn: sqlite3.Connection, workflow_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM leases WHERE resource_type IN ('step','slice') "
        "AND resource_key LIKE ? ORDER BY acquired_at DESC",
        (f"{workflow_id}:%",)).fetchall()
    return [dict(r) for r in rows]


def for_task(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM leases WHERE resource_type='path' AND resource_key LIKE ? "
        "ORDER BY acquired_at DESC", (f"{task_id}:%",)).fetchall()
    return [dict(r) for r in rows]


def normalize_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or any(p == ".." for p in value.split("/")):
        raise ConflictError(f"unsafe path reservation: {path!r}")
    while value.startswith("./"):
        value = value[2:]
    return value


def _path_key(task_id: str, path: str) -> str:
    return f"{task_id}:{normalize_path(path)}"


def check_paths(conn: sqlite3.Connection, task_id: str, paths: list[str],
                holder: str | None = None) -> list[dict]:
    requested = [normalize_path(p) for p in paths]
    conflicts = []
    for row in for_task(conn, task_id):
        if row["status"] != "active":
            continue
        if row["expires_at"] and row["expires_at"] <= now_iso():
            continue
        if holder and row.get("holder") == holder:
            continue
        reserved = str(row["resource_key"]).split(":", 1)[1]
        for path in requested:
            if (path == reserved or path.startswith(reserved.rstrip("/") + "/")
                    or reserved.startswith(path.rstrip("/") + "/")):
                conflicts.append({"path": path, "reserved": reserved,
                                  "holder": row.get("holder"),
                                  "expires_at": row.get("expires_at")})
    return conflicts


def reserve_paths(conn: sqlite3.Connection, task_id: str, paths: list[str], *,
                  holder: str, ttl_seconds: float | None = None) -> list[dict]:
    unique = list(dict.fromkeys(normalize_path(p) for p in paths))
    if not unique:
        return []
    conflicts = check_paths(conn, task_id, unique, holder=holder)
    if conflicts:
        raise ConflictError(f"path_conflict: {conflicts[0]}")
    out = []
    try:
        for path in unique:
            out.append(acquire(
                conn, "path", _path_key(task_id, path), holder_type="role",
                holder=holder, ttl_seconds=ttl_seconds,
                meta={"task_id": task_id, "path": path}, commit=False))
    except ConflictError:
        for lease in out:
            release(conn, lease["id"], commit=False)
        raise
    conn.commit()
    return out


def release_paths(conn: sqlite3.Connection, task_id: str, paths: list[str],
                  holder: str) -> int:
    count = 0
    for path in paths:
        row = active(conn, "path", _path_key(task_id, path))
        if row and row.get("holder") == holder:
            release(conn, row["id"], commit=False)
            count += 1
    conn.commit()
    return count
