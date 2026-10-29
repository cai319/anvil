"""Reminder 调度（design §6.4）：cron / at / when 三种形态。

到期的 reminder 生成一个无任务的 run，把 prompt 作为唤醒内容注入。Agent 侧的
`anvil reminder add` 与人工创建的 reminder 走同一条路径。

cron 解析不引第三方依赖：支持 `*`、`*/n`、`a,b`、`a-b`、`a-b/n` 五段表达式。
时间按本机时区计算（与用户对「每天早九点」的直觉一致），存储为带偏移的 ISO。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from ..util import ConflictError, jdumps, now_iso


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = max(1, int(raw_step))
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
        else:
            start = end = int(part)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron field out of range: {field}")
        values.update(range(start, end + 1, step))
    return values


def _parse_cron(expr: str):
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron 表达式需要 5 段：分 时 日 月 周")
    minute = _parse_field(fields[0], 0, 59)
    hour = _parse_field(fields[1], 0, 23)
    dom = _parse_field(fields[2], 1, 31)
    month = _parse_field(fields[3], 1, 12)
    raw_dow = _parse_field(fields[4], 0, 7)
    dow = {0 if d == 7 else d for d in raw_dow}
    return minute, hour, dom, month, dow


def _cron_matches(parsed, dt: datetime) -> bool:
    minute, hour, dom, month, dow = parsed
    # Python: Monday=0 … Sunday=6；cron: Sunday=0 … Saturday=6
    cron_dow = (dt.weekday() + 1) % 7
    return (
        dt.minute in minute and dt.hour in hour and dt.day in dom
        and dt.month in month and cron_dow in dow
    )


def next_cron_fire(expr: str, after: datetime | None = None) -> str:
    """从 after（默认现在）开始逐分钟寻找下一次命中，最多向后找 366 天。"""
    parsed = _parse_cron(expr)
    t = (after or datetime.now().astimezone()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if _cron_matches(parsed, t):
            return t.isoformat()
        t += timedelta(minutes=1)
    raise ValueError(f"cron 表达式在 366 天内无命中: {expr}")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def create_reminder(conn: sqlite3.Connection, *, role_slug: str | None, kind: str = "cron",
                    cron_expr: str | None = None, fire_at: str | None = None,
                    when_condition: str | None = None, prompt: str | None = None) -> dict:
    kind = (kind or "cron").lower()
    if kind not in ("cron", "at", "when"):
        raise ConflictError(f"unknown reminder kind: {kind}")
    if kind == "cron":
        if not cron_expr:
            raise ConflictError("cron reminder requires cron_expr")
        next_fire = next_cron_fire(cron_expr)
    elif kind == "at":
        dt = _parse_iso(fire_at)
        if dt is None:
            raise ConflictError("at reminder requires ISO fire_at")
        next_fire = dt.isoformat()
    else:
        if not when_condition:
            raise ConflictError("when reminder requires when_condition (task id)")
        next_fire = now_iso()

    rid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO reminders (id, role_slug, kind, cron_expr, fire_at, when_condition, prompt, "
        "status, next_fire_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, role_slug, kind, cron_expr, fire_at, when_condition, prompt, "pending", next_fire, now_iso()),
    )
    conn.commit()
    return get_reminder(conn, rid)  # type: ignore[return-value]


def get_reminder(conn: sqlite3.Connection, rid: str) -> dict | None:
    row = conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def list_reminders(conn: sqlite3.Connection, role: str | None = None) -> list[dict]:
    if role:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE role_slug=? ORDER BY created_at DESC", (role,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reminders ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_reminder(conn: sqlite3.Connection, rid: str, **fields) -> dict | None:
    if get_reminder(conn, rid) is None:
        return None
    allowed = {k: v for k, v in fields.items() if k in (
        "role_slug", "kind", "cron_expr", "fire_at", "when_condition", "prompt", "status"
    ) and v is not None}
    if not allowed:
        return get_reminder(conn, rid)
    if "cron_expr" in allowed:
        allowed["next_fire_at"] = next_cron_fire(allowed["cron_expr"])
    elif "fire_at" in allowed:
        allowed["next_fire_at"] = allowed["fire_at"]
    cols = ", ".join(f"{k}=?" for k in allowed)
    conn.execute(f"UPDATE reminders SET {cols} WHERE id=?", (*allowed.values(), rid))
    conn.commit()
    return get_reminder(conn, rid)


def delete_reminder(conn: sqlite3.Connection, rid: str) -> bool:
    cur = conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()
    return cur.rowcount > 0


def _condition_met(conn: sqlite3.Connection, condition: str) -> bool:
    """when_condition 形如 `T-1042` 或 `task:T-1042:done`。

    裸任务 ID 与 `task:<id>` 默认要求 `done`；显式状态则精确匹配。`closed` 是
    人工终止（取消），不是成功完成，不能作为完成信号。
    """
    tid = condition
    wanted = "done"
    if condition.startswith("task:"):
        parts = condition.split(":")
        tid = parts[1] if len(parts) > 1 else ""
        if len(parts) > 2 and parts[2].strip():
            wanted = parts[2].strip()
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    return bool(row and row["status"] == wanted)


def create_reminder_run(conn: sqlite3.Connection, role_slug: str, prompt: str) -> dict:
    """创建 reminder run，并记录触发前角色是否已有进程。

    reminder 结束后，daemon 只回收 was_alive=0 的临时进程；原本在干活的角色
    继续处理自己的任务队列（知识管理不抢占、也不终止原有工作）。
    """
    from . import daemon as daemon_service  # 惰性导入避免循环
    from . import runs as runs_service  # 惰性导入避免循环

    was_alive = False
    mgr = daemon_service.get_manager()
    if mgr is not None:
        try:
            was_alive = bool(mgr.status(role_slug).get("alive"))
        except Exception:  # noqa: BLE001 —— 状态查询失败不影响提醒本身
            was_alive = False

    run = runs_service.create_run(conn, role_slug, None, prompt=prompt)
    conn.execute(
        "INSERT OR REPLACE INTO reminder_wakes (run_id, role_slug, was_alive, created_at) "
        "VALUES (?,?,?,?)",
        (run["id"], role_slug, 1 if was_alive else 0, now_iso()),
    )
    conn.commit()
    return run


def fire_due_reminders(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """触发所有到期 reminder，返回触发数量。由治理 tick 每轮调用。"""
    now_dt = now or datetime.now().astimezone()
    now_iso_str = now_dt.isoformat()
    # 清理异常中断（未走到 turn result）的历史唤醒记录，避免表无限增长。
    conn.execute("DELETE FROM reminder_wakes WHERE created_at < ?",
                 ((now_dt - timedelta(days=7)).isoformat(),))
    fired = 0
    rows = conn.execute("SELECT * FROM reminders WHERE status='pending'").fetchall()
    for r in rows:
        due = False
        if r["kind"] == "cron":
            next_at = _parse_iso(r["next_fire_at"])
            due = next_at is not None and next_at <= now_dt
        elif r["kind"] == "at":
            fire_at = _parse_iso(r["fire_at"] or r["next_fire_at"])
            due = fire_at is not None and fire_at <= now_dt
        elif r["kind"] == "when":
            due = _condition_met(conn, r["when_condition"] or "")

        if not due:
            continue

        role = r["role_slug"]
        if not role or conn.execute(
                "SELECT 1 FROM roles WHERE slug=? AND enabled=1", (role,)).fetchone() is None:
            conn.execute("UPDATE reminders SET status='error' WHERE id=?", (r["id"],))
            continue
        create_reminder_run(conn, role, r["prompt"] or "定时提醒")
        conn.execute(
            "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
            (role, None, "reminder", jdumps({"reminder_id": r["id"], "prompt": r["prompt"]}), now_iso()),
        )
        if r["kind"] == "cron" and r["cron_expr"]:
            try:
                nxt = next_cron_fire(r["cron_expr"], now_dt)
                conn.execute(
                    "UPDATE reminders SET next_fire_at=? WHERE id=?", (nxt, r["id"])
                )
            except ValueError:
                conn.execute("UPDATE reminders SET status='error' WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE reminders SET status='fired' WHERE id=?", (r["id"],))
        fired += 1
    conn.commit()
    return fired
