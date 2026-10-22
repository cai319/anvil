"""治理（M2）：租约回收 / 全场冻结 / 成本闸门 / 并发闸门 / 审计。

owner 口径：成本「不限制」→ 成本闸门是软闸门（未设阈值 = 不限；设了才拦）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso

# 四层闸门的默认值（design §5.6）。仅当 settings 中显式写入时覆盖。
DEFAULT_MAX_PARALLEL_SYSTEM = 6
DEFAULT_MAX_PARALLEL_RUNTIME = 4


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), now_iso()),
    )
    conn.commit()


def get_limits(conn: sqlite3.Connection) -> dict:
    """系统级 / 运行时级并发上限 + 日成本预算（None = 不限）。"""
    def as_int(key: str, default: int) -> int:
        raw = get_setting(conn, key)
        try:
            return int(raw) if raw is not None and raw != "" else default
        except (TypeError, ValueError):
            return default

    raw_budget = get_setting(conn, "daily_cost_budget_usd")
    try:
        daily_budget = float(raw_budget) if raw_budget not in (None, "") else None
    except (TypeError, ValueError):
        daily_budget = None
    return {
        "max_parallel_system": as_int("max_parallel_system", DEFAULT_MAX_PARALLEL_SYSTEM),
        "max_parallel_runtime": as_int("max_parallel_runtime", DEFAULT_MAX_PARALLEL_RUNTIME),
        "daily_cost_budget_usd": daily_budget,
    }


def update_limits(conn: sqlite3.Connection, *, max_parallel_system=None,
                  max_parallel_runtime=None, daily_cost_budget_usd=None) -> dict:
    if max_parallel_system is not None:
        set_setting(conn, "max_parallel_system", int(max_parallel_system))
    if max_parallel_runtime is not None:
        set_setting(conn, "max_parallel_runtime", int(max_parallel_runtime))
    if daily_cost_budget_usd is not None:
        set_setting(conn, "daily_cost_budget_usd", "" if daily_cost_budget_usd == "" else float(daily_cost_budget_usd))
    return get_limits(conn)


def runtime_of_role(conn: sqlite3.Connection, role: str) -> str | None:
    row = conn.execute("SELECT definition_json FROM roles WHERE slug=?", (role,)).fetchone()
    if not row:
        return None
    definition = jloads(row["definition_json"], {})
    rt = definition.get("runtime")
    if isinstance(rt, dict):
        return rt.get("id")
    return rt or None


# 并发额度只统计真正占用进程的阶段；suspended/submitted/reviewing 都是空闲等待，
# 不应把角色/系统额度占满（design §5.6 “同时最多几个运行”）。
CONCURRENCY_RUN_STATUSES = ("created", "projecting", "running")


def _active_run_rows(conn: sqlite3.Connection):
    """当前活跃 run（正在创建/投影/运行）；空闲 claim 与挂起会话不占并发额度。"""
    return conn.execute(
        "SELECT role_slug, task_id FROM runs WHERE status IN (?,?,?)",
        CONCURRENCY_RUN_STATUSES,
    ).fetchall()


def _active_pairs(conn: sqlite3.Connection) -> set[tuple[str, str | None]]:
    """活跃工作单元 (role, task)：以 run 为准；空闲 claim 不占并发额度。"""
    pairs: set[tuple[str, str | None]] = set()
    for r in _active_run_rows(conn):
        if r["role_slug"]:
            pairs.add((r["role_slug"], r["task_id"]))
    return pairs


def _active_count(conn: sqlite3.Connection, runtime_id: str | None = None) -> int:
    rows = _active_run_rows(conn)
    if runtime_id is None:
        return len(rows)
    roles = {r["role_slug"] for r in rows if r["role_slug"]}
    by_runtime = {role: runtime_of_role(conn, role) for role in roles}
    return sum(
        1 for r in rows
        if r["role_slug"] and by_runtime.get(r["role_slug"]) == runtime_id
    )


def _role_active_count(conn: sqlite3.Connection, role: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE role_slug=? AND status IN (?,?,?)",
        (role, *CONCURRENCY_RUN_STATUSES),
    ).fetchone()
    return int(row["n"])


def daily_cost_usd(conn: sqlite3.Connection) -> float:
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(usd),0) AS s FROM cost_ledger WHERE recorded_at >= ?", (day_start,)
    ).fetchone()
    return float(row["s"] or 0.0)


def tick(conn: sqlite3.Connection, force: bool = False, dispatch: bool = True,
         plans_only: bool = False) -> dict:
    """完整调度循环（§6.1）：回收租约 → 扫 todo → 匹配 eligible agent → wake → 饥饿升级。

    force 用于测试后门（fake clock）：跳过 lease 到期判断，直接回收全部 in_progress。
    dispatch=False 时只回收租约、不自动 wake todo（派发完全由「指派并执行」手动决定）。
    """
    now = now_iso()
    if force:
        rows = conn.execute("SELECT id FROM tasks WHERE status='in_progress'").fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='in_progress' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at < ? AND NOT EXISTS ("
            "  SELECT 1 FROM workflows w WHERE w.task_id=tasks.id AND w.status='running')",
            (now,),
        ).fetchall()
    for r in rows:
        tid = r["id"]
        conn.execute(
            "UPDATE tasks SET status='todo', assignee=NULL, claimed_at=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
            (now, tid),
        )
        _terminate_runs(conn, tid)  # 回收前终止仍存活的子进程（防孤儿烧钱）
        conn.execute(
            "UPDATE runs SET status='abandoned', ended_at=? WHERE task_id=? AND status NOT IN ('completed','failed','abandoned')",
            (now, tid),
        )
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, updated_at=? "
            "WHERE task_id=? AND status='working'", (now, tid)
        )

    # 团队任务的角色级租约：某个角色失联不应拖住其他正在工作的角色。
    expired_claims = conn.execute(
        "SELECT task_id, role_slug FROM task_role_claims "
        "WHERE status='working' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
        (now,),
    ).fetchall()
    for c in expired_claims:
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, updated_at=? "
            "WHERE task_id=? AND role_slug=?", (now, c["task_id"], c["role_slug"])
        )
        _terminate_runs(conn, c["task_id"], c["role_slug"])  # 角色级回收同样终止子进程
        conn.execute(
            "UPDATE runs SET status='abandoned', ended_at=? WHERE task_id=? AND role_slug=? "
            "AND status NOT IN ('completed','failed','abandoned')",
            (now, c["task_id"], c["role_slug"]),
        )
        other = conn.execute(
            "SELECT role_slug FROM task_role_claims WHERE task_id=? AND status='working' "
            "ORDER BY claimed_at LIMIT 1", (c["task_id"],)
        ).fetchone()
        task_row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (c["task_id"],)).fetchone()
        if task_row and task_row["assignee"] == c["role_slug"]:
            conn.execute(
                "UPDATE tasks SET assignee=?, updated_at=? WHERE id=?",
                (other["role_slug"] if other else None, now, c["task_id"]),
            )
        conn.execute(
            "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
            (c["role_slug"], c["task_id"], "lease_expired", jdumps({}), now),
        )

    _cleaner_check(conn)

    # 到期的 reminder（design §6.4）：cron / at / when 三种形态，触发时创建无任务 run。
    from . import reminders as reminders_service  # 惰性导入避免循环
    fired = reminders_service.fire_due_reminders(conn)

    # 扫 todo → wake eligible agent。依赖感知：只派发「所有 depends_on 均已 done/closed」的任务
    # （下游等上游完成才开工）。有显式绑定角色的按角色派发；无角色的自动挑 eligible agent。
    woken = 0
    starved = 0
    if dispatch:
        for t in conn.execute(
            "SELECT * FROM tasks WHERE status='todo' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on "
            "  WHERE d.task_id = tasks.id AND dt.status NOT IN ('done','closed')"
            ")"
        ).fetchall():
            task_dict = dict(t)
            roles = _assigned_roles(conn, t["id"])
            # 计划先行落板的子任务（有 parent_id）自动按依赖执行；顶层任务仍由人工「派发」触发，
            # 否则新建任务会绕过 Cindy 规划直接开跑。
            # plans_only=True（后台调度器）：只自动跑计划子任务；手动 /control/tick 保持原语义。
            auto_ok = (not plans_only) or config.AUTO_DISPATCH or bool(t["parent_id"])
            if roles:
                if not auto_ok:
                    continue
                woke = False
                for role in roles:
                    if not _claim_allowed(conn, task_dict, role):
                        continue
                    if _wake(conn, role, t["id"]):
                        woken += 1
                        woke = True
                if not woke:
                    conn.execute(
                        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
                        (None, t["id"], "starvation",
                         jdumps({"reason": "assigned roles blocked by governance gates"}), now),
                    )
                    starved += 1
                continue
            if plans_only and not config.AUTO_DISPATCH:
                continue  # 后台调度器不自动认领无主任务（避免绕过人工派发）
            role = _pick_eligible_agent(conn, task_dict)
            if role:
                if _wake(conn, role, t["id"]):
                    woken += 1
            else:
                # 饥饿升级：无 eligible agent → 审计告警
                conn.execute(
                    "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
                    (None, t["id"], "starvation", jdumps({"reason": "no eligible agent"}), now),
                )
                starved += 1
    conn.commit()
    return {"reclaimed": len(rows), "woken": woken, "starved": starved, "reminders_fired": fired}


def _terminate_runs(conn: sqlite3.Connection, task_id: str, role_slug: str | None = None) -> None:
    """回收/废弃前终止仍存活的子进程（防孤儿进程继续烧 token）。"""
    from ..platform import factory  # 惰性导入避免循环

    q = ("SELECT native_session_id FROM runs WHERE task_id=? "
         "AND status NOT IN ('completed','failed','abandoned')")
    params: list = [task_id]
    if role_slug:
        q += " AND role_slug=?"
        params.append(role_slug)
    try:
        pm = factory.get_platform()[0]
    except Exception:  # noqa: BLE001
        return
    for r in conn.execute(q, params).fetchall():
        if r["native_session_id"]:
            try:
                pm.terminate(r["native_session_id"])
            except Exception:  # noqa: BLE001
                pass


def _cleaner_check(conn: sqlite3.Connection) -> int:
    """MEMORY.md 体积监控（零 token）：超限时给角色排一条自修剪提醒，每天至多一次。"""
    if not config.CLEANER_ENABLED:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    now = now_iso()
    n = 0
    for role in conn.execute("SELECT slug FROM roles WHERE enabled=1").fetchall():
        slug = role["slug"]
        key = f"cleaner:{slug}"
        if get_setting(conn, key) == today:
            continue
        path = Path(config.AGENTS_DIR) / slug / "MEMORY.md"
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= config.CLEANER_MAX_BYTES:
            continue
        prompt = (
            f"你的 MEMORY.md 已有 {size} 字节，超过建议大小（{config.CLEANER_MAX_BYTES} 字节）。"
            "请回顾近期任务与 notes：合并重复要点、删除过时内容、保留仍然准确的事实，"
            "把值得共享的经验用 `anvil lesson propose` 提出，保持记忆精简（建议 ≤300 行）。"
        )
        conn.execute(
            "INSERT INTO reminders (id, role_slug, kind, fire_at, prompt, status, next_fire_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), slug, "at", now, prompt, "pending", now, now),
        )
        set_setting(conn, key, today)
        n += 1
    return n


def _claim_allowed(conn: sqlite3.Connection, task: dict, role: str) -> bool:
    try:
        check_claim_allowed(conn, task, role)
        return True
    except ConflictError:
        return False


def _pick_eligible_agent(conn: sqlite3.Connection, task: dict) -> str | None:
    """匹配 eligible agent：频道成员（write/admin）∩ 四层闸门；不满足就换下一个。"""
    wid = task.get("workspace_id")
    if wid:
        rows = conn.execute(
            "SELECT role_slug AS slug FROM workspace_members "
            "WHERE workspace_id=? AND access IN ('write','admin') ORDER BY role_slug", (wid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT slug FROM roles WHERE enabled=1 ORDER BY slug").fetchall()
    for r in rows:
        if _claim_allowed(conn, task, r["slug"]):
            return r["slug"]
    return None


def _assigned_roles(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """任务显式绑定的参与角色：优先 task_assignees，否则单负责人 assignee。"""
    roles = [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=?", (task_id,)
    )]
    if roles:
        return roles
    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id=? AND assignee IS NOT NULL AND assignee<>''",
        (task_id,),
    ).fetchone()
    return [row["assignee"]] if row else []


def _wake(conn: sqlite3.Connection, role: str, task_id: str) -> bool:
    """唤醒：为该任务创建 run（created，快照+worktree）并记审计；已有活跃 run 则跳过。"""
    from . import runs as runs_service  # 惰性导入避免 tasks↔governance↔runs 循环

    existing = conn.execute(
        "SELECT id FROM runs WHERE task_id=? AND role_slug=? "
        "AND status IN ('created','projecting','running','submitted','reviewing')",
        (task_id, role),
    ).fetchone()
    if existing:
        return False
    runs_service.create_run(conn, role, task_id)
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
        (role, task_id, "wake", jdumps({"hint": task_id}), now_iso()),
    )
    return True


def is_frozen(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key='freeze'").fetchone()
    return bool(row and row["value"] == "1")


def set_freeze(conn: sqlite3.Connection, frozen: bool) -> dict:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('freeze', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("1" if frozen else "0", now_iso()),
    )
    conn.commit()
    return {"frozen": frozen}


def check_claim_allowed(conn: sqlite3.Connection, task: dict, role: str) -> None:
    """认领前的治理闸门：全场冻结 / 成本达限 / 并发达限 → ConflictError(409)。"""
    if is_frozen(conn):
        raise ConflictError("global freeze active")

    # 任务级成本：本任务 cost_used 达 cost_budget_usd
    budget = task.get("cost_budget_usd")
    if budget is not None and task.get("cost_used_usd", 0) >= budget:
        raise ConflictError(f"cost budget {budget} exhausted")

    # 角色级成本：角色已有 in_progress 任务超预算 → 停新认领（进行中允许收尾）
    over = conn.execute(
        "SELECT id FROM tasks WHERE assignee=? AND status='in_progress' "
        "AND cost_budget_usd IS NOT NULL AND cost_used_usd >= cost_budget_usd",
        (role,),
    ).fetchone()
    if over:
        raise ConflictError(f"role {role} has an over-budget task {over['id']}")

    limits = get_limits(conn)

    # 日成本闸门（软闸门：未设阈值 = 不限）
    budget = limits["daily_cost_budget_usd"]
    if budget is not None and daily_cost_usd(conn) >= budget:
        raise ConflictError(f"daily cost budget ${budget} exhausted")

    # 并发闸门：角色级 max_parallel（按活跃 run 计数；空闲 claim 不占额度）
    max_parallel = _role_max_parallel(conn, role)
    if max_parallel is not None:
        active = _role_active_count(conn, role)
        if active >= max_parallel:
            raise ConflictError(f"role {role} concurrency limit {max_parallel} reached")

    # 并发闸门：运行时级（避免同一订阅被打爆）
    runtime_id = runtime_of_role(conn, role)
    if runtime_id and _active_count(conn, runtime_id) >= limits["max_parallel_runtime"]:
        raise ConflictError(
            f"runtime {runtime_id} concurrency limit {limits['max_parallel_runtime']} reached"
        )

    # 并发闸门：系统级
    if _active_count(conn) >= limits["max_parallel_system"]:
        raise ConflictError(f"system concurrency limit {limits['max_parallel_system']} reached")


def inject_cost(conn: sqlite3.Connection, role: str, usd: float, source: str = "reported") -> dict:
    """成本注入（测试后门 + 成本上报）：记 cost_ledger + 累加到角色 in_progress 任务。"""
    conn.execute(
        "INSERT INTO cost_ledger (role_slug, usd, source, recorded_at) VALUES (?,?,?,?)",
        (role, usd, source, now_iso()),
    )
    conn.execute(
        "UPDATE tasks SET cost_used_usd = cost_used_usd + ? WHERE assignee=? AND status='in_progress'",
        (usd, role),
    )
    conn.commit()
    return budget_status(conn, role)


def report_cost(conn: sqlite3.Connection, task_id: str, role: str | None, usd: float, source: str = "reported") -> dict:
    """记成本账 + 累加指定任务 cost_used_usd。"""
    conn.execute(
        "INSERT INTO cost_ledger (role_slug, task_id, usd, source, recorded_at) VALUES (?,?,?,?,?)",
        (role, task_id, usd, source, now_iso()),
    )
    conn.execute("UPDATE tasks SET cost_used_usd = cost_used_usd + ?, updated_at=? WHERE id=?", (usd, now_iso(), task_id))
    conn.commit()
    row = conn.execute("SELECT cost_used_usd FROM tasks WHERE id=?", (task_id,)).fetchone()
    return {"task_id": task_id, "cost_used_usd": row["cost_used_usd"] if row else 0.0}


def budget_status(conn: sqlite3.Connection, role: str | None = None) -> dict:
    if role:
        spent = conn.execute(
            "SELECT COALESCE(SUM(usd),0) AS s FROM cost_ledger WHERE role_slug=?", (role,)
        ).fetchone()["s"]
    else:
        spent = conn.execute(
            "SELECT COALESCE(SUM(usd),0) AS s FROM cost_ledger"
        ).fetchone()["s"]
    return {
        "role": role,
        "cost_used_usd": spent,
        "cost_today_usd": daily_cost_usd(conn),
        **get_limits(conn),
    }


def costs(conn: sqlite3.Connection, scope: str | None = None,
          date_from: str | None = None, date_to: str | None = None) -> dict:
    """成本聚合：按角色 / 按日（design §6.5，用于发现异常趋势而非财务核算）。"""
    clauses, params = [], []
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
    by_role = [
        dict(r) for r in conn.execute(
            f"SELECT COALESCE(role_slug,'unknown') AS role, COALESCE(SUM(usd),0) AS usd, COUNT(*) AS entries "
            f"FROM cost_ledger{where} GROUP BY role_slug ORDER BY usd DESC", params
        )
    ]
    by_day = [
        dict(r) for r in conn.execute(
            f"SELECT substr(recorded_at,1,10) AS day, COALESCE(SUM(usd),0) AS usd "
            f"FROM cost_ledger{where} GROUP BY day ORDER BY day", params
        )
    ]
    total = sum(r["usd"] for r in by_role)
    return {"scope": scope, "total_usd": total, "by_role": by_role, "by_day": by_day}


def list_audit(conn: sqlite3.Connection, task_id: str | None = None, limit: int = 100,
               role: str | None = None, action: str | None = None,
               date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    clauses, params = [], []
    if task_id:
        clauses.append("task_id=?"); params.append(task_id)
    if role:
        clauses.append("role_slug=?"); params.append(role)
    if action:
        clauses.append("action=?"); params.append(action)
    if date_from:
        clauses.append("created_at >= ?"); params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?"); params.append(date_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = jloads(d.pop("payload_json"), {})
        out.append(d)
    return out


def role_can_write(conn: sqlite3.Connection, role: str | None, repo: str | None) -> bool:
    """角色对 repo 的写权限（三交集之一）。无 role/repo → 不限；repos 清单非空则为白名单。"""
    if not role or not repo:
        return True
    row = conn.execute("SELECT definition_json FROM roles WHERE slug=?", (role,)).fetchone()
    if not row:
        return True
    definition = jloads(row["definition_json"], {})
    repos = (definition.get("workspace") or {}).get("repos") or []
    if not repos:
        return True
    for r in repos:
        if r.get("name") == repo:
            return r.get("access") != "read"
    return False


def _role_max_parallel(conn: sqlite3.Connection, role: str) -> int | None:
    row = conn.execute("SELECT definition_json FROM roles WHERE slug=?", (role,)).fetchone()
    if not row:
        return None
    definition = jloads(row["definition_json"], {})
    concurrency = definition.get("concurrency") or {}
    # design §5.6：角色级默认 2
    return concurrency.get("max_parallel", 2)
