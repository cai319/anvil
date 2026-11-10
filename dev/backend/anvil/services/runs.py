"""Run 生命周期 + 配置快照冻结 + 分支隔离（M2 7–8）。

`create_run` 在 run 创建瞬间冻结 profile 快照（config_snapshots + runs.profile_snapshot），
并按 design §4.3 做 worktree 分支隔离（`agent/<slug>/<task>` 分支，best-effort）。

运行 8 态由 LangGraph 编排控制面（`anvil.graph`）迁移；本模块只提供 `create_run`（status=created）
与 `set_status`/`set_native_session`/`find_active_run` 等最小写原语——`running` 归 spawn 节点。
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

from .. import config
from ..util import decode_child_output, jdumps, jloads, new_id, now_iso
from . import ledger
from . import roles as roles_service
from . import tasks as tasks_service


def create_run(conn: sqlite3.Connection, role_slug: str, task_id: str | None = None,
               prompt: str | None = None, step_id: str | None = None,
               workflow_id: str | None = None, slice_key: str | None = None,
               dispatch_id: str | None = None,
               origin: str | None = None) -> dict:
    """冻结快照 + 插入 run（status='created'）+ best-effort worktree + 建日志文件。不拉起子进程。

    `prompt` 用于无任务的 run（reminder / 直接消息），有任务时由任务内容组装提示。
    """
    effective = roles_service.effective_config(conn, role_slug) or {}
    payload = jdumps(effective)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO config_snapshots (digest, payload_json, created_at) VALUES (?,?,?)",
        (digest, payload, now_iso()),
    )

    rid = new_id()
    worktree = _worktree_path(rid)
    log_path = _log_path(role_slug, rid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            f"# run {rid}  role={role_slug}  task={task_id}  slice={slice_key or '-'}"
            f"  started={now_iso()}\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, profile_snapshot, worktree_path, "
        "log_path, prompt, step_id, workflow_id, slice_key, dispatch_id, origin, started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, role_slug, task_id, "created", digest, str(worktree), str(log_path), prompt,
         step_id, workflow_id, slice_key, dispatch_id, origin, now_iso()),
    )
    conn.commit()

    _create_worktree(conn, rid, role_slug, task_id, worktree)
    return get_run(conn, rid)  # type: ignore[return-value]


def start_run(conn: sqlite3.Connection, role_slug: str, task_id: str | None = None) -> dict:
    """兼容旧调用点：等价于 create_run。"""
    return create_run(conn, role_slug, task_id)


def get_run(conn: sqlite3.Connection, rid: str) -> dict | None:
    r = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


# 活跃会话（进程可能活着）：主页「实时会话」只展示这些。
ACTIVE_RUN_STATUSES = ("created", "projecting", "running", "submitted", "reviewing", "suspended")
# Run 终态：进入后不得再被投递/复活（人工 reopen 必须走显式路径）。
TERMINAL_RUN_STATUSES = ("completed", "failed", "abandoned")
# 只有这些状态可以被角色队列投递；submitted/reviewing 是控制面 HITL 状态。
DISPATCHABLE_RUN_STATUSES = ("created", "projecting", "running", "suspended")


def is_terminal_status(status: str | None) -> bool:
    return str(status or "") in TERMINAL_RUN_STATUSES


def is_dispatchable_status(status: str | None) -> bool:
    return str(status or "") in DISPATCHABLE_RUN_STATUSES


def list_runs(conn: sqlite3.Connection, role_slug: str | None = None,
              task_id: str | None = None,
              statuses: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    q = "SELECT * FROM runs"
    clauses, params = [], []
    if role_slug:
        clauses.append("role_slug=?"); params.append(role_slug)
    if task_id:
        clauses.append("task_id=?"); params.append(task_id)
    if statuses:
        clauses.append("status IN (" + ",".join("?" * len(statuses)) + ")")
        params.extend(statuses)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY started_at DESC"
    return [dict(r) for r in conn.execute(q, params)]


_ANY_SLICE = object()


def find_active_run(conn: sqlite3.Connection, task_id: str,
                    role_slug: str | None = None,
                    slice_key=_ANY_SLICE) -> dict | None:
    """任务当前非终态 run（可按角色/切片过滤，取最新一个）。

    注意：切片化后同一角色可能同时有多个 suspended run（不同 slice）。
    需要精确上下文时必须传 slice_key；省略表示“任意 slice 的最新一个”，
    只适合展示/兼容路径，不应当作唯一活跃 run 的依据。
    slice_key=None 只匹配 task 级 run。
    """
    q = "SELECT * FROM runs WHERE task_id=? AND status NOT IN ('completed','failed','abandoned')"
    params: list = [task_id]
    if role_slug:
        q += " AND role_slug=?"
        params.append(role_slug)
    if slice_key is not _ANY_SLICE:
        if slice_key is None:
            q += " AND slice_key IS NULL"
        else:
            q += " AND slice_key=?"
            params.append(slice_key)
    q += " ORDER BY started_at DESC LIMIT 1"
    r = conn.execute(q, params).fetchone()
    return dict(r) if r else None


def set_status(conn: sqlite3.Connection, run_id: str, status: str, ended_at: str | None = None,
               failure_class: str | None = None,
               from_statuses: tuple[str, ...] | list[str] | None = None) -> bool:
    """迁移 run 状态，并把迁移作为 activity 事件落库（供任务时间线/会话视图消费）。

    `from_statuses` 提供 compare-and-set 语义：只有当前状态在允许集合内才写入，
    否则返回 False。用于 turn 结束、投递、溢出恢复等可能与 scheduler 竞争的路径。

    failure_class 仅在显式传入时更新（不清空语义：终态后再迁移不抹掉失败归因）。
    """
    prev = conn.execute(
        "SELECT status, task_id, role_slug, workflow_id, step_id, slice_key "
        "FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if prev is None:
        return False
    where = "id=?"
    params: list = [run_id]
    if from_statuses:
        allowed = tuple(str(s) for s in from_statuses)
        where += " AND status IN (" + ",".join("?" * len(allowed)) + ")"
        params.extend(allowed)
    if failure_class is not None:
        cur = conn.execute(
            f"UPDATE runs SET status=?, ended_at=?, failure_class=? WHERE {where}",
            (status, ended_at, failure_class, *params))
    else:
        cur = conn.execute(
            f"UPDATE runs SET status=?, ended_at=? WHERE {where}",
            (status, ended_at, *params))
    if cur.rowcount != 1:
        conn.commit()
        return False
    if prev and prev["status"] != status:
        execution_no = None
        if prev["workflow_id"]:
            wf = conn.execute(
                "SELECT execution_no FROM workflows WHERE id=?", (prev["workflow_id"],)
            ).fetchone()
            execution_no = wf["execution_no"] if wf else None
        ledger.emit(
            conn, "run.status_changed", task_id=prev["task_id"],
            workflow_id=prev["workflow_id"], execution_no=execution_no,
            step_id=prev["step_id"], run_id=run_id,
            actor_type="role" if prev["role_slug"] else "system",
            actor=prev["role_slug"],
            payload={"status": status, "prev": prev["status"],
                     "slice_key": prev["slice_key"]},
            mirror_activity=True, activity_kind="run",
            activity_role=prev["role_slug"],
        )
    conn.commit()
    return True


def mark_spawned(conn: sqlite3.Connection, run_id: str, native_session_id: str,
                 timeout_seconds: float | None = None) -> None:
    """spawn 成功：写会话与 run 级超时，并重置 started_at（续跑重新计时，修 P0-1）。"""
    conn.execute(
        "UPDATE runs SET native_session_id=?, timeout_seconds=?, started_at=?, ended_at=NULL "
        "WHERE id=?",
        (native_session_id, timeout_seconds, now_iso(), run_id),
    )
    conn.commit()


def note_context_rotation(conn: sqlite3.Connection, run_id: str, *,
                          max_rotations: int | None = None) -> str:
    """记录一次上下文轮换；超过连续上限则把 run 判为失败。

    只用于 turn-end 高水位释放等“已经决定换会话”的路径；真正重投状态由调用方
    通过 `reset_run_for_redelivery` 设置。返回 `ok` / `failed` / `skip`。
    """
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or is_terminal_status(row["status"]):
        return "skip"
    count = int(row["context_rotation_count"] or 0) + 1
    if max_rotations is not None and count > max(0, int(max_rotations)):
        conn.execute("UPDATE runs SET context_rotation_count=? WHERE id=?", (count, run_id))
        if set_status(conn, run_id, "failed", ended_at=now_iso(),
                      failure_class="context_rotation_loop",
                      from_statuses=ACTIVE_RUN_STATUSES):
            conn.execute(
                "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (row["role_slug"], row["task_id"], "session.context_rotation_failed",
                 jdumps({"run_id": run_id, "count": count}), now_iso()))
            conn.commit()
        return "failed"
    conn.execute("UPDATE runs SET context_rotation_count=? WHERE id=?", (count, run_id))
    conn.commit()
    return "ok"


def reset_run_for_redelivery(conn: sqlite3.Connection, run_id: str, *,
                             reason: str = "context rotation",
                             max_rotations: int | None = None) -> str:
    """主动换会话（非错误）：清 run 的会话标记并重新投递，不消耗失败预算。

    与 `register_context_overflow` 的区别：这是正常上下文管理（达到水位主动换新），
    不应计入“溢出重试次数”，也不会因此通知人类；但连续轮换必须有上限，
    防止 CIC 本身超过硬阈值时无限 kill→CIC→spawn。
    """
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or is_terminal_status(row["status"]):
        return "skip"
    count = int(row["context_rotation_count"] or 0) + 1
    if max_rotations is not None and count > max(0, int(max_rotations)):
        conn.execute("UPDATE runs SET context_rotation_count=? WHERE id=?", (count, run_id))
        if set_status(conn, run_id, "failed", ended_at=now_iso(),
                      failure_class="context_rotation_loop",
                      from_statuses=ACTIVE_RUN_STATUSES):
            conn.execute(
                "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (row["role_slug"], row["task_id"], "session.context_rotation_failed",
                 jdumps({"run_id": run_id, "reason": reason, "count": count}),
                 now_iso()))
            conn.commit()
        return "failed"
    # 更新 started_at，使 reset 前被 monitor 捕获的 process_exit 因 observed_started_at
    # 陈旧而让路；不能只把状态改回 created。
    cur = conn.execute(
        "UPDATE runs SET context_rotation_count=?, native_session_id=NULL, "
        "started_at=?, ended_at=NULL WHERE id=? AND status NOT IN "
        "('completed','failed','abandoned')",
        (count, now_iso(), run_id))
    conn.commit()
    if cur.rowcount != 1:
        return "skip"
    if not set_status(conn, run_id, "created", from_statuses=ACTIVE_RUN_STATUSES):
        return "skip"
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (row["role_slug"], row["task_id"], "session.context_rotation",
         jdumps({"run_id": run_id, "reason": reason, "count": count}), now_iso()),
    )
    conn.commit()
    return "retry"


def register_context_overflow(conn: sqlite3.Connection, run_id: str, *,
                              max_retries: int, reason: str = "") -> str:
    """上下文溢出后的 run 处置：清会话回 created 重投，或超限判失败。

    返回 `retry` / `failed` / `skip`（run 不存在或已终态）。调用方在 `retry` 时
    泵 `wake`，`spawn` 会因 `cic_pending`/高水位走新会话 + CIC。
    """
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or is_terminal_status(row["status"]):
        return "skip"
    attempt = int(row["context_reset_count"] or 0) + 1
    cur = conn.execute(
        "UPDATE runs SET context_reset_count=?, native_session_id=NULL, "
        "started_at=?, ended_at=NULL WHERE id=? AND status NOT IN "
        "('completed','failed','abandoned')",
        (attempt, now_iso(), run_id),
    )
    conn.commit()
    if cur.rowcount != 1:
        return "skip"
    outcome = "retry" if attempt <= max(0, int(max_retries)) else "failed"
    if outcome == "retry":
        if not set_status(conn, run_id, "created", from_statuses=ACTIVE_RUN_STATUSES):
            outcome = "skip"
    else:
        if not set_status(conn, run_id, "failed", ended_at=now_iso(),
                          failure_class="context_overflow",
                          from_statuses=ACTIVE_RUN_STATUSES):
            outcome = "skip"
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (row["role_slug"], row["task_id"], "session.context_overflow",
         jdumps({"run_id": run_id, "reason": reason, "attempt": attempt,
                 "outcome": outcome}), now_iso()),
    )
    conn.commit()
    return outcome


def suspend_active(conn: sqlite3.Connection) -> int:
    """优雅停机：活跃 run（running/projecting）终止子进程并转 suspended（可 @mention 唤醒续跑）。

    suspended 不是失败：不写 ended_at、不写 failure_class；无进程的「挂起等待」run 同样归入。
    """
    from ..platform import factory  # 惰性导入，避免 services→platform 顶层循环

    pm = factory.get_platform()[0]
    n = 0
    for r in conn.execute(
        "SELECT id, native_session_id FROM runs WHERE status IN ('running','projecting')"
    ).fetchall():
        if r["native_session_id"]:
            try:
                pm.terminate(r["native_session_id"])
            except Exception:  # noqa: BLE001 —— 停机路径尽力而为
                pass
        set_status(conn, r["id"], "suspended")
        n += 1
    conn.commit()
    return n


def heal_stale_runs(conn: sqlite3.Connection) -> dict:
    """启动自愈：上一实例崩溃遗留的 running/projecting run 转 suspended（先终止孤儿进程）。"""
    from ..platform import factory

    pm = factory.get_platform()[0]
    healed = terminated = 0
    for r in conn.execute(
        "SELECT id, native_session_id FROM runs WHERE status IN ('running','projecting')"
    ).fetchall():
        sid = r["native_session_id"]
        if sid:
            try:
                if pm.probe(sid).alive:
                    pm.terminate(sid)  # 孤儿进程（其 CLI 回调已无服务端可寻）
                    terminated += 1
            except Exception:  # noqa: BLE001
                pass
        set_status(conn, r["id"], "suspended")
        healed += 1
    conn.commit()
    return {"healed": healed, "terminated": terminated}


def set_native_session(conn: sqlite3.Connection, run_id: str, native_session_id: str | None) -> None:
    conn.execute("UPDATE runs SET native_session_id=? WHERE id=?", (native_session_id, run_id))
    conn.commit()


def _worktree_path(run_id: str) -> Path:
    return config.DATA_DIR / "worktrees" / run_id


def _log_path(role_slug: str, run_id: str) -> Path:
    return config.AGENTS_DIR / role_slug / "logs" / f"run-{run_id}.log"


def run_timeline(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """该 run 的生命周期迁移事件（kind='run' 的 activities），供运行会话详情追溯。"""
    out = []
    for r in conn.execute(
        "SELECT id, role_slug, task_id, run_id, kind, payload_json, created_at "
        "FROM activities WHERE run_id=? AND kind='run' ORDER BY created_at, id", (run_id,)
    ).fetchall():
        out.append({
            "key": f"activity-{r['id']}",
            "ts": r["created_at"],
            "kind": r["kind"],
            "role": r["role_slug"],
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "payload": jloads(r["payload_json"], {}),
        })
    return out


def get_run_log(conn: sqlite3.Connection, run_id: str) -> dict:
    """读该 run 的 stdout/stderr 日志文件（末尾最多 200KB，避免大文件撑爆响应）。"""
    run = get_run(conn, run_id)
    if not run:
        return {"run_id": run_id, "content": "", "exists": False}
    p = Path(run["log_path"]) if run.get("log_path") else None
    if not p or not p.exists():
        return {"run_id": run_id, "content": "", "exists": False}
    data = p.read_bytes()
    if len(data) > 200_000:
        data = data[-200_000:]
        prefix = f"... (truncated, showing last {len(data)} bytes)\n".encode()
        data = prefix + data
    return {"run_id": run_id, "log_path": str(p), "exists": True,
            "content": decode_child_output(data)}


def get_snapshot(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """解析该 run 冻结的 profile 快照（config_snapshots by digest）。"""
    run = get_run(conn, run_id)
    if not run or not run.get("profile_snapshot"):
        return None
    row = conn.execute(
        "SELECT payload_json FROM config_snapshots WHERE digest=?", (run["profile_snapshot"],)
    ).fetchone()
    return jloads(row["payload_json"], {}) if row else None


def _create_worktree(conn: sqlite3.Connection, run_id: str, role_slug: str, task_id: str | None, worktree: Path) -> bool:
    """分支隔离（§4.3）：在共享 repo 上开独立 worktree + agent/<slug>/<task> 分支。

    best-effort：repo 不存在或非 git checkout 时返回 False，run 保持 status='created'，
    后续 spawn 节点回退到 agent home 投影/执行。不再在此置 running（running 归 spawn 节点）。
    """
    if not task_id:
        return False
    task = tasks_service.get(conn, task_id)
    # 执行数据来源优先取 task workspace.source_repo；旧数据/直接插入回退 tasks.repo。
    ws = conn.execute(
        "SELECT source_repo FROM task_workspaces WHERE task_id=?", (task_id,)
    ).fetchone()
    repo_name = (ws["source_repo"] if ws is not None else None) \
        or (task or {}).get("repo")
    if not repo_name:
        return False
    repo_path = config.REPOS_DIR / "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in repo_name
    )
    if not (repo_path / ".git").exists():
        return False
    branch = f"agent/{role_slug}/{task_id}"
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(worktree)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
