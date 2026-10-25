"""Task CRUD + manual assignment + agent-side state transitions + 治理闸门（M2）。

状态机：todo → in_progress → in_review → done（成功终态）；
非终态 todo/in_progress/in_review → closed（人工终止）；closed → todo（重新打开）。
认领原子（BEGIN IMMEDIATE + 条件 UPDATE + rowcount）；治理闸门（冻结/Token 预算/并发）见
`services.governance`；关键转换写审计日志。
"""
from __future__ import annotations

import fnmatch
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import governance, knowledge, knowledge_binding, ledger, repo_catalog, workspace

LEASE_SECONDS = 1800  # design §5.5 租期 30 分钟
TERMINAL_TASK_STATUSES = ("done", "closed")
NON_TERMINAL_TASK_STATUSES = ("todo", "in_progress", "in_review")


def _assert_can_terminate(t: dict) -> None:
    if t["status"] not in NON_TERMINAL_TASK_STATUSES:
        raise ConflictError(
            f"task {t['id']} is {t['status']}; only todo/in_progress/in_review "
            "can be manually terminated")


def _assert_can_reopen(t: dict) -> None:
    if t["status"] != "closed":
        raise ConflictError(
            f"task {t['id']} is {t['status']}; only a terminated task can be reopened")


def _assert_not_terminal(t: dict, action: str) -> None:
    if t["status"] in TERMINAL_TASK_STATUSES:
        raise ConflictError(f"task {t['id']} is {t['status']}; cannot {action}")

# P2#6 预留：M1 用内置 deny 清单；M2 三交集 = scope_paths ∩ role access ∩ .anvilignore。
# deny 清单独立成常量，便于 M2 换成加载 .anvilignore + 角色 access。
DENIED_PREFIXES = (".anvil/", ".git/", ".env", "secrets/")

# 编排器自己投影产生的文件，不算 Agent 的改动（否则每次 submit 都会越界）。
def _is_anvil_managed(path: str) -> bool:
    p = path[2:] if path.startswith("./") else path
    # 只排除投影产物；`.anvil/` 仍是受保护目录（改它必须被拒）。
    return p in ("CLAUDE.md", "AGENTS.md", "GEMINI.md") or p.startswith((".claude/", ".gemini/"))


def _init_role_claims(conn: sqlite3.Connection, tid: str, roles: list[str], now: str) -> None:
    for role in roles:
        conn.execute(
            "INSERT OR IGNORE INTO task_role_claims (task_id, role_slug, status, updated_at) "
            "VALUES (?,?,?,?)",
            (tid, role, "pending", now),
        )


def _role_claims(conn: sqlite3.Connection, tid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role_slug, status, summary, run_id, claimed_at, lease_expires_at, submitted_at "
        "FROM task_role_claims WHERE task_id=? ORDER BY role_slug", (tid,)
    ).fetchall()
    return [dict(r) for r in rows]


def _role_claim(conn: sqlite3.Connection, tid: str, role: str):
    return conn.execute(
        "SELECT * FROM task_role_claims WHERE task_id=? AND role_slug=?", (tid, role)
    ).fetchone()


def is_team_task(conn: sqlite3.Connection, tid: str) -> bool:
    row = conn.execute("SELECT 1 FROM task_assignees WHERE task_id=? LIMIT 1", (tid,)).fetchone()
    return row is not None


def is_task_participant(conn: sqlite3.Connection, tid: str, role: str) -> bool:
    """角色是否为任务参与角色：团队 assignee 或单负责人。"""
    row = conn.execute(
        "SELECT 1 FROM task_assignees WHERE task_id=? AND role_slug=? LIMIT 1", (tid, role)
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE id=? AND assignee=? LIMIT 1", (tid, role)
    ).fetchone()
    return row is not None


def audit_create(conn: sqlite3.Connection, tid: str, actor: str | None, *,
                 parent_id: str | None = None, workspace_id: str | None = None,
                 repo: str | None = None) -> None:
    """记录任务创建审计，尤其是角色运行时创建的子任务。"""
    _audit(conn, actor, tid, "create", {
        "parent_id": parent_id,
        "workspace_id": workspace_id,
        "repo": repo,
    })
    conn.commit()


def create(conn: sqlite3.Connection, *, id: str | None = None, title: str,
           type: str | None = None, priority=None, tags: list[str] | None = None,
           repo: str | None = None, workspace_id: str | None = None, base_branch: str | None = None,
           scope_paths: list[str] | None = None, description: str | None = None,
           acceptance: list[str] | None = None, assignee: str | None = None,
           assignees: list[str] | None = None,
           plan_first: bool = False, planner: str | None = None,
           plan_seed: dict | None = None,
           created_by: str | None = None, parent_id: str | None = None,
           depends_on: list[str] | None = None,
           knowledge_repos: list | None = None,
           knowledge_repos_source: str = "human") -> dict:
    tid = id or new_id()
    if str(tid).startswith("__"):
        # `__global__` 是角色上下文槽的保留 key，不能与真实任务 id 冲突。
        raise ConflictError(f"task id {tid!r} is reserved")
    now = now_iso()
    tags = knowledge.normalize_tags(tags or [])
    _assert_assignable(conn, assignees or [])
    if parent_id:
        # 子任务从属于父任务：继承 workspace/repo/base_branch，禁止覆盖结构标记。
        parent = get(conn, parent_id)
        if parent is None:
            raise LookupError(parent_id)
        for field_name, supplied in (
            ("workspace_id", workspace_id),
            ("repo", repo),
            ("base_branch", base_branch),
        ):
            inherited = parent.get(field_name)
            if supplied is not None and supplied != inherited:
                raise ConflictError(
                    f"subtask {field_name} must inherit parent {parent_id}: "
                    f"expected {inherited!r}, got {supplied!r}"
                )
        repo = parent.get("repo")
        workspace_id = parent.get("workspace_id")
        base_branch = parent.get("base_branch")
    conn.execute(
        """INSERT INTO tasks (id, title, type, status, priority, tags_json, repo, workspace_id,
                              base_branch, scope_paths_json, description, acceptance_json,
                              assignee, plan_first, planner, plan_seed_json, created_by, parent_id,
                              created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, title, type, "todo", priority, jdumps(tags), repo, workspace_id, base_branch,
         jdumps(scope_paths or []), description, jdumps(acceptance or []),
         assignee, int(plan_first), planner, jdumps(plan_seed) if plan_seed else None,
         created_by, parent_id, now, now),
    )
    for d in (depends_on or []):
        conn.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)", (tid, d))
    for a in (assignees or []):
        conn.execute("INSERT OR IGNORE INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)", (tid, a, now))
    _init_role_claims(conn, tid, list(assignees or []), now)
    # 显式指定是人工决策：落库并加锁，后续 auto/Cindy 不再覆盖。
    if knowledge_repos:
        knowledge_binding.set_manual(
            conn, tid, knowledge_repos, source=knowledge_repos_source or "human",
            lock=True, commit=False)
    else:
        knowledge_binding.auto_bind(conn, {
            "id": tid, "title": title, "description": description, "tags": tags,
            "repo": repo, "workspace_id": workspace_id, "parent_id": parent_id,
        }, commit=False)
    conn.commit()
    repo_catalog.invalidate_catalog()
    return get(conn, tid)  # type: ignore[return-value]


def _assert_assignable(conn: sqlite3.Connection, roles: list[str]) -> None:
    """onboarding 助理是系统默认角色，不能作为任务参与成员。"""
    from . import roles as roles_service

    for slug in roles:
        if roles_service.is_onboarding(conn, slug):
            raise ConflictError(f"{slug} is the built-in onboarding assistant and cannot join tasks")


def get(conn: sqlite3.Connection, tid: str) -> dict | None:
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    d = _row(r)
    d["depends_on"] = [x["depends_on"] for x in conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id=?", (tid,))]
    d["assignees"] = [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at", (tid,))]
    d["role_claims"] = _role_claims(conn, tid)
    knowledge_binding.attach_bindings(conn, [d])
    return d


def assign_roles(conn: sqlite3.Connection, tid: str, roles: list[str]) -> dict:
    """绑定任务的参与角色（团队）。全量替换 task_assignees + 重置角色工作状态。"""
    if get(conn, tid) is None:
        raise LookupError(tid)
    _assert_assignable(conn, roles)
    now = now_iso()
    conn.execute("DELETE FROM task_assignees WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_role_claims WHERE task_id=?", (tid,))
    for a in roles:
        conn.execute("INSERT INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)", (tid, a, now))
    _init_role_claims(conn, tid, roles, now)
    # 重新绑定团队时，todo 任务清空 assignee（lead），让派发的 agent 能 claim
    conn.execute("UPDATE tasks SET assignee=NULL WHERE id=? AND status='todo'", (tid,))
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def add_assignees(conn: sqlite3.Connection, tid: str, roles: list[str]) -> dict:
    """追加任务参与角色（非破坏性，保留已有 assignees/claims）。"""
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_assignable(conn, roles)
    now = now_iso()
    existing = set(t["assignees"])
    added = [r for r in dict.fromkeys(roles) if r not in existing]
    for r in added:
        conn.execute(
            "INSERT OR IGNORE INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
            (tid, r, now),
        )
    _init_role_claims(conn, tid, added, now)
    if t.get("workspace_id"):
        from . import workspace as workspace_service
        for r in added:
            workspace_service.add_participant(conn, t["workspace_id"], r)
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def list_all(conn: sqlite3.Connection, status: str | None = None, assignee: str | None = None,
             workspace_id: str | None = None, type: str | None = None,
             tag: str | None = None, parent_id: str | None = None,
             include_archived: bool = False, archived_only: bool = False) -> list[dict]:
    q = "SELECT * FROM tasks"
    clauses, params = [], []
    if archived_only:
        clauses.append("archived_at IS NOT NULL")
    elif not include_archived:
        clauses.append("archived_at IS NULL")
    if status:
        clauses.append("status=?"); params.append(status)
    if assignee:
        clauses.append("assignee=?"); params.append(assignee)
    if workspace_id:
        clauses.append("workspace_id=?"); params.append(workspace_id)
    if type:
        clauses.append("type=?"); params.append(type)
    if parent_id:
        clauses.append("parent_id=?"); params.append(parent_id)
    if tag:
        # tags_json 是 JSON 数组，这里做精确元素匹配（带引号避免子串误命中）
        clauses.append("tags_json LIKE ?"); params.append(f'%"{tag}"%')
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    tasks = [_with_deps(conn, _row(r)) for r in conn.execute(q, params)]
    return knowledge_binding.attach_bindings(conn, tasks)


def update(conn: sqlite3.Connection, tid: str, **fields) -> dict | None:
    task = get(conn, tid)
    if task is None:
        return None
    now = now_iso()
    json_cols = {"tags": "tags_json", "scope_paths": "scope_paths_json", "acceptance": "acceptance_json"}
    if "tags" in fields and fields["tags"] is not None:
        fields["tags"] = knowledge.normalize_tags(fields["tags"])
    if "status" in fields and fields["status"] is not None:
        target = getattr(fields["status"], "value", fields["status"])
        target = str(target)
        current = task["status"]
        if target == current:
            fields.pop("status")
        elif current in TERMINAL_TASK_STATUSES:
            raise ConflictError(
                f"task {tid} is {current}; terminal tasks cannot change status")
        elif target in TERMINAL_TASK_STATUSES:
            raise ConflictError(
                f"task {tid} cannot be set to {target} via PATCH; "
                "use review to complete or the terminate/reopen endpoints")
        else:
            fields["status"] = target
    assign = {}
    for k, v in fields.items():
        if v is None or v == "":
            continue
        col = json_cols.get(k, k)
        assign[col] = jdumps(v) if col in json_cols.values() else v
    assign["updated_at"] = now
    cols = ", ".join(f"{c}=?" for c in assign)
    conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*assign.values(), tid))
    conn.commit()
    if "tags" in fields:
        repo_catalog.invalidate_catalog()
        task = get(conn, tid)
        if task is not None and not task.get("knowledge_repos_locked"):
            knowledge_binding.auto_bind(conn, task)
    return get(conn, tid)


def _active_run_for_role(conn: sqlite3.Connection, tid: str, role: str):
    return conn.execute(
        "SELECT id FROM runs WHERE task_id=? AND role_slug=? "
        "AND status NOT IN ('completed','failed','abandoned') "
        "ORDER BY started_at DESC LIMIT 1",
        (tid, role),
    ).fetchone()


def _complete_runs(conn: sqlite3.Connection, tid: str, role: str | None = None) -> None:
    """角色贡献已提交或任务收口时，结束对应 run（不再参与 monitor）。"""
    from . import runs as runs_service  # 惰性导入避免循环
    if role:
        rows = conn.execute(
            "SELECT id FROM runs WHERE task_id=? AND role_slug=? "
            "AND status NOT IN ('completed','failed','abandoned')", (tid, role)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM runs WHERE task_id=? AND status NOT IN ('completed','failed','abandoned')",
            (tid,),
        ).fetchall()
    for r in rows:
        runs_service.set_status(conn, r["id"], "completed", ended_at=now_iso())


def _team_all_submitted(conn: sqlite3.Connection, tid: str, assignees: list[str]) -> bool:
    if not assignees:
        return False
    rows = conn.execute(
        "SELECT role_slug, status FROM task_role_claims WHERE task_id=?", (tid,)
    ).fetchall()
    statuses = {r["role_slug"]: r["status"] for r in rows}
    return all(statuses.get(a) in ("submitted", "approved") for a in assignees)


def claim(conn: sqlite3.Connection, tid: str, role: str, paths: list[str] | None = None) -> dict:
    """认领任务。单负责人任务保持排他；多角色任务允许每个参与角色并行认领。"""
    from . import roles as roles_service
    if roles_service.is_onboarding(conn, role):
        raise ConflictError("onboarding assistant cannot claim tasks")
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, tid)
        if t is None:
            raise LookupError(tid)
        _assert_claimable(conn, t)
        governance.check_claim_allowed(conn, t, role)
        lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
        now = now_iso()
        if t["assignees"]:
            # 团队任务：每个 task_assignees 角色独立认领，互不排斥。
            if role not in t["assignees"]:
                raise ConflictError(f"role {role} is not an assignee of task {tid}")
            if t["status"] not in ("todo", "in_progress"):
                raise ConflictError(f"task {tid} is {t['status']}, cannot claim")
            existing = _role_claim(conn, tid, role)
            if existing and existing["status"] in ("submitted", "approved"):
                raise ConflictError(f"role {role} already submitted task {tid}")
            run = _active_run_for_role(conn, tid, role)
            conn.execute(
                "INSERT INTO task_role_claims (task_id, role_slug, status, run_id, claimed_at, "
                "lease_expires_at, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id, role_slug) DO UPDATE SET status='working', "
                "run_id=excluded.run_id, claimed_at=excluded.claimed_at, "
                "lease_expires_at=excluded.lease_expires_at, submitted_at=NULL, summary=NULL, "
                "updated_at=excluded.updated_at",
                (tid, role, "working", run["id"] if run else None, now, lease, now),
            )
            conn.execute(
                "UPDATE tasks SET status='in_progress', "
                "assignee=COALESCE(NULLIF(assignee,''), ?), "
                "claimed_at=COALESCE(claimed_at, ?), lease_expires_at=?, updated_at=? WHERE id=?",
                (role, now, lease, now, tid),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET status='in_progress', assignee=?, claimed_at=?, "
                "lease_expires_at=?, updated_at=? WHERE id=? AND status='todo' "
                "AND (assignee IS NULL OR assignee = '' OR assignee = ?)",
                (role, now, lease, now, tid, role),
            )
            if cur.rowcount == 0:
                if t["status"] not in ("todo", "in_progress"):
                    raise ConflictError(f"task {tid} is {t['status']}, cannot claim")
                owner = t.get("assignee") or ""
                if owner and owner != role:
                    raise ConflictError(
                        f"task {tid} is {t['status']} and exclusively claimed by {owner}; "
                        f"ask {owner} to `anvil task unclaim {tid}` or add you as a team "
                        f"assignee before claiming")
                raise ConflictError(f"task {tid} is not claimable (already claimed or not todo)")
        _audit(conn, role, tid, "claim", {"paths": paths, "team": bool(t["assignees"])})
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def unclaim(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_not_terminal(t, "unclaim")
    now = now_iso()
    if t["assignees"] and role:
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, "
            "submitted_at=NULL, updated_at=? WHERE task_id=? AND role_slug=?",
            (now, tid, role),
        )
        active = conn.execute(
            "SELECT role_slug FROM task_role_claims WHERE task_id=? AND status='working' "
            "ORDER BY claimed_at LIMIT 1", (tid,)
        ).fetchone()
        if active:
            conn.execute(
                "UPDATE tasks SET assignee=?, lease_expires_at=?, updated_at=? WHERE id=?",
                (active["role_slug"] if t["assignee"] == role else t["assignee"], now, now, tid),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status='todo', assignee=NULL, claimed_at=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE id=?", (now, tid),
            )
    else:
        # 单负责人任务，或人类对团队任务执行「整体放弃」
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, "
            "submitted_at=NULL, updated_at=? WHERE task_id=? AND status IN ('pending','working')",
            (now, tid),
        )
        conn.execute(
            "UPDATE tasks SET status='todo', assignee=NULL, claimed_at=NULL, "
            "lease_expires_at=NULL, updated_at=? WHERE id=?", (now, tid),
        )
    _audit(conn, role, tid, "unclaim")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def heartbeat(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    conn.execute("UPDATE tasks SET lease_expires_at=?, updated_at=? WHERE id=?", (lease, now_iso(), tid))
    if t["assignees"] and role:
        conn.execute(
            "UPDATE task_role_claims SET lease_expires_at=?, updated_at=? "
            "WHERE task_id=? AND role_slug=? AND status='working'",
            (lease, now_iso(), tid, role),
        )
    _audit(conn, role, tid, "heartbeat")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def submit(conn: sqlite3.Connection, tid: str, *, summary: str | None = None,
           branch: str | None = None, changed_paths: list[str] | None = None,
           role: str | None = None) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_submittable(conn, t)
    # 三交集之一：角色对 repo 无写权限 → 拒（scope ∩ role access ∩ .anvilignore）
    if not governance.role_can_write(conn, role, t["repo"]):
        raise ConflictError(f"role {role} has no write access to repo {t['repo']}")
    # 硬防线（§4.3）：不只信 Agent 自报的 changed_paths，尽量从 worktree/分支真实计算。
    changed = _collect_changed_paths(conn, t, role, changed_paths)
    patterns = _ignore_patterns(t)
    if changed:
        for p in changed:
            if _denied(p, patterns) or not _in_scope(p, t["scope_paths"]):
                raise ConflictError(f"path '{p}' out of scope or denied")
    if t["status"] != "in_progress":
        raise ConflictError(f"task {tid} is {t['status']}, expected in_progress to submit")

    now = now_iso()
    if t["assignees"]:
        # 团队任务：每个角色独立提交；全部参与角色提交后才进入任务级评审。
        force_finalize = role is None  # 人类可从 UI 直接收口
        if role is not None:
            if role not in t["assignees"]:
                raise ConflictError(f"role {role} is not an assignee of task {tid}")
            claim = _role_claim(conn, tid, role)
            if claim is None or claim["status"] not in ("working", "claimed"):
                raise ConflictError(f"role {role} has not claimed task {tid}")
            conn.execute(
                "UPDATE task_role_claims SET status='submitted', summary=?, submitted_at=?, "
                "lease_expires_at=NULL, updated_at=? WHERE task_id=? AND role_slug=?",
                (summary, now, now, tid, role),
            )
            _complete_runs(conn, tid, role)
        if force_finalize:
            conn.execute(
                "UPDATE task_role_claims SET status='submitted', "
                "submitted_at=COALESCE(submitted_at, ?), lease_expires_at=NULL, updated_at=? "
                "WHERE task_id=? AND status IN ('pending','working')", (now, now, tid)
            )
        if not force_finalize and not _team_all_submitted(conn, tid, t["assignees"]):
            _audit(conn, role, tid, "submit", {"summary": summary, "branch": branch,
                                               "changed_paths": changed,
                                               "reported_paths": changed_paths,
                                               "partial": True})
            conn.commit()
            return get(conn, tid)  # type: ignore[return-value]

        # 全部提交（或人工收口）→ 任务进入评审
        conn.execute(
            "UPDATE tasks SET status='in_review', lease_expires_at=NULL, updated_at=? WHERE id=?",
            (now, tid),
        )
        _complete_runs(conn, tid)
        mention(conn, tid, role or "system", "human",
                f"团队任务已全部提交，请评审（{', '.join(t['assignees'])}）")
        _audit(conn, role, tid, "submit", {"summary": summary, "branch": branch,
                                           "changed_paths": changed,
                                           "reported_paths": changed_paths,
                                           "final": True})
        conn.commit()
        return get(conn, tid)  # type: ignore[return-value]

    conn.execute("UPDATE tasks SET status='in_review', updated_at=? WHERE id=?", (now, tid))
    _audit(conn, role, tid, "submit", {"summary": summary, "branch": branch,
                                       "changed_paths": changed, "reported_paths": changed_paths})
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def review(conn: sqlite3.Connection, tid: str, approve: bool, role: str | None = None) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    if t["status"] != "in_review":
        raise ConflictError(f"task {tid} is {t['status']}, expected in_review")
    new_status = "done" if approve else "in_progress"
    lease = None if approve else t.get("lease_expires_at")
    conn.execute(
        "UPDATE tasks SET status=?, lease_expires_at=?, updated_at=? WHERE id=?",
        (new_status, lease, now_iso(), tid),
    )
    if t["assignees"]:
        if approve:
            conn.execute(
                "UPDATE task_role_claims SET status='approved', updated_at=? WHERE task_id=?",
                (now_iso(), tid),
            )
        else:
            # 驳回：已提交的角色回到 working，可重新派发/继续修改
            conn.execute(
                "UPDATE task_role_claims SET status='working', submitted_at=NULL, updated_at=? "
                "WHERE task_id=? AND status='submitted'", (now_iso(), tid)
            )
    _audit(conn, role, tid, "review", {"approve": approve})
    conn.commit()
    if approve:
        notify_downstream(conn, tid)  # 完成后 @提及下游角色，触发交接
    return get(conn, tid)  # type: ignore[return-value]


def cancel(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    """人工终止任务（design §10 tasks/{id}/cancel）：终态 closed，保留审计。

    仅允许从非终态进入；done 是成功终态，closed 已是终止态，均拒绝。
    刻意不终止 run / 常驻角色进程：closed 可通过 reopen 恢复，保留会话上下文
    以便续跑；只有物理删除（delete）才回收进程。
    """
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_can_terminate(t)
    conn.execute(
        "UPDATE tasks SET status='closed', lease_expires_at=NULL, updated_at=? WHERE id=?",
        (now_iso(), tid),
    )
    conn.execute(
        "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, updated_at=? "
        "WHERE task_id=? AND status IN ('pending','working')", (now_iso(), tid)
    )
    _audit(conn, role, tid, "cancel")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def reopen(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    """重新打开已人工终止的任务：closed → todo，清空认领信息。"""
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_can_reopen(t)
    conn.execute(
        "UPDATE tasks SET status='todo', assignee=NULL, claimed_at=NULL, "
        "lease_expires_at=NULL, archived_at=NULL, updated_at=? WHERE id=?",
        (now_iso(), tid),
    )
    conn.execute(
        "UPDATE task_role_claims SET status='pending', summary=NULL, run_id=NULL, "
        "claimed_at=NULL, lease_expires_at=NULL, submitted_at=NULL, updated_at=? WHERE task_id=?",
        (now_iso(), tid),
    )
    _audit(conn, role, tid, "reopen")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def archive(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    """归档终态任务（done/closed）；不删除任务、run 或 timeline。"""
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    if t["status"] not in ("done", "closed"):
        raise ConflictError(
            f"task {tid} is {t['status']}; only done/closed tasks can be archived"
        )
    now = now_iso()
    conn.execute(
        "UPDATE tasks SET archived_at=?, updated_at=? WHERE id=?",
        (now, now, tid),
    )
    _audit(conn, role, tid, "archive")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def unarchive(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    """取消归档；任务状态不变。"""
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    conn.execute(
        "UPDATE tasks SET archived_at=NULL, updated_at=? WHERE id=?",
        (now_iso(), tid),
    )
    _audit(conn, role, tid, "unarchive")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def archive_completed(conn: sqlite3.Connection, workspace_id: str | None = None,
                      role: str | None = None) -> list[dict]:
    """批量归档 done/closed 任务，可按频道过滤。"""
    clauses = ["status IN ('done','closed')", "archived_at IS NULL"]
    params: list = []
    if workspace_id:
        clauses.append("workspace_id=?")
        params.append(workspace_id)
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE {' AND '.join(clauses)}", params
    ).fetchall()
    ids = [r["id"] for r in rows]
    now = now_iso()
    for tid in ids:
        conn.execute(
            "UPDATE tasks SET archived_at=?, updated_at=? WHERE id=?",
            (now, now, tid),
        )
        _audit(conn, role, tid, "archive")
    conn.commit()
    return [get(conn, tid) for tid in ids]  # type: ignore[list-item]


def task_diff(conn: sqlite3.Connection, tid: str) -> dict:
    """任务当前改动与越界情况（design §10 tasks/{id}/diff 折叠区）。"""
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    run = conn.execute(
        "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (tid,)
    ).fetchone()
    if run is None:
        return {"task_id": tid, "run_id": None, "changed": [], "violations": [], "diff": ""}

    changed = _collect_changed_paths(conn, t, run["role_slug"], None)
    patterns = _ignore_patterns(t)
    violations = [p for p in changed if _denied(p, patterns) or not _in_scope(p, t["scope_paths"])]

    diff_text = ""
    wt = Path(run["worktree_path"]) if run["worktree_path"] else None
    if wt and wt.exists():
        diff_text = "\n".join(_git_lines(wt, ["diff"]))
        diff_text += "\n".join(_git_lines(wt, ["diff", "--cached"]))
        untracked = [p for p in changed if p]
        if untracked:
            diff_text += "\n# changed files:\n" + "\n".join(untracked)
    return {
        "task_id": tid,
        "run_id": run["id"],
        "role": run["role_slug"],
        "worktree": run["worktree_path"],
        "changed": changed,
        "violations": violations,
        "diff": diff_text.strip(),
    }


def task_config_snapshot(conn: sqlite3.Connection, tid: str) -> dict:
    """本任务最近一次 run 的冻结配置（design §10 config-snapshot）。"""
    row = conn.execute(
        "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (tid,)
    ).fetchone()
    if row is None:
        return {}
    from . import runs as runs_service  # 惰性导入
    return {"run_id": row["id"], "snapshot": runs_service.get_snapshot(conn, row["id"])}


def delete(conn: sqlite3.Connection, tid: str, *, force: bool = False,
           role: str | None = None) -> bool:
    """物理删除任务（区别于 cancel：cancel 保留历史）。

    - 有活跃 run 或子任务时默认拒绝（409）；`force=True` 终止活跃 run、把子任务挂到根。
    - 级联删除 task_deps / task_assignees / activities / 关联 runs / audit。
    """
    t = get(conn, tid)
    if t is None:
        return False

    runs = conn.execute(
        "SELECT id, role_slug, native_session_id, status, started_at "
        "FROM runs WHERE task_id=?", (tid,)
    ).fetchall()
    active = [r for r in runs if r["status"] not in ("completed", "failed", "abandoned")]
    if active and not force:
        raise ConflictError(f"task {tid} has {len(active)} active run(s); use force to delete")

    children = conn.execute("SELECT id FROM tasks WHERE parent_id=?", (tid,)).fetchall()
    if children and not force:
        raise ConflictError(f"task {tid} has child tasks; delete them first or use force")

    # 删除前先让每个参与角色固化本任务知识快照，再终止/清理其运行时资源。
    # 快照写入 notes/knowledge/<task>/<time>-task-deleted.md（文件 + FTS），
    # 删除 activities/runs 后仍然保留，不会随任务物理删除而丢失。
    latest_run: dict[str, tuple[str, str]] = {}
    for r in runs:
        role = r["role_slug"]
        if not role:
            continue
        stamp = r["started_at"] or ""
        if role not in latest_run or stamp > latest_run[role][0]:
            latest_run[role] = (stamp, r["id"])
    latest_run_id = {role: rid for role, (_stamp, rid) in latest_run.items()}
    slot_roles = {row["role_slug"] for row in conn.execute(
        "SELECT role_slug FROM role_task_sessions WHERE task_key=?", (tid,)).fetchall()}
    task_roles = set(latest_run_id) | slot_roles
    consolidated: list[str] = []
    consolidation_errors: list[str] = []
    if task_roles:
        from . import memory as memory_service
        for role_slug in sorted(task_roles):
            try:
                snap = memory_service.consolidate_context(
                    conn, role_slug, tid, "task-deleted", latest_run_id.get(role_slug))
                if snap:
                    consolidated.append(role_slug)
            except Exception as exc:  # noqa: BLE001 —— 快照失败不阻断删除，但写进删除审计
                consolidation_errors.append(f"{role_slug}: {exc}")

    # 任务共享知识不随任务物理删除：标记 archived，保留来源指针供频道/父任务追溯。
    try:
        from . import task_knowledge as task_knowledge_service
        task_knowledge_service.archive_for_task(conn, tid)
    except Exception:  # noqa: BLE001 —— 归档失败不阻断删除
        pass

    # 清理该任务的运行时进程：按角色去重，从所有 run/上下文槽中收集。
    # cancel_task 只会终止「正忙于此任务」或「空闲且最后处理的就是此任务」的常驻进程，
    # 不会误杀正在处理其它任务的角色；平台 one-shot 只终止活跃 run 的 pg: 进程，
    # 避免 PID 复用导致误杀。
    if task_roles:
        from . import daemon as daemon_service
        mgr = daemon_service.get_manager()
        if mgr is not None and mgr.enabled:
            for role_slug in sorted(task_roles):
                try:
                    mgr.cancel_task(role_slug, tid)
                except Exception:  # noqa: BLE001 —— 删除任务不因清理失败而回滚
                    pass
        # daemon 未启用/已停机时也要清掉角色镜像；enabled 时这里是幂等的兜底。
        for role_slug in sorted(task_roles):
            try:
                daemon_service.clear_session(
                    conn, role_slug, "task deleted",
                    task_key_value=daemon_service.task_key(tid))
            except Exception:  # noqa: BLE001
                pass
    if active:
        from ..platform import factory
        pm = None
        for r in active:
            sid = r["native_session_id"] or ""
            if not sid or sid.startswith("rs:"):
                continue
            if pm is None:
                try:
                    pm = factory.get_platform()[0]
                except Exception:  # noqa: BLE001
                    pm = None
            if pm:
                try:
                    pm.terminate(sid)
                except Exception:  # noqa: BLE001
                    pass
    if children and force:
        conn.execute("UPDATE tasks SET parent_id=NULL WHERE parent_id=?", (tid,))

    # 任务删除时一并清掉其 workflow，避免孤儿步骤继续被调度器推进。
    for wf in conn.execute("SELECT id FROM workflows WHERE task_id=?", (tid,)).fetchall():
        conn.execute("DELETE FROM workflow_steps WHERE workflow_id=?", (wf["id"],))
        conn.execute("DELETE FROM workflows WHERE id=?", (wf["id"],))
    conn.execute("DELETE FROM runs WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_deps WHERE task_id=? OR depends_on=?", (tid, tid))
    conn.execute("DELETE FROM task_assignees WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_knowledge_repos WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_role_claims WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM activities WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM sessions WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM role_inbox_cursors WHERE task_id=?", (tid,))
    # 任务上下文槽（daemon 未启用/角色已停机时也要清，避免孤儿槽被下次同名任务复用）
    conn.execute("DELETE FROM role_task_sessions WHERE task_key=?", (tid,))
    conn.execute("DELETE FROM audit WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
    # 留一条删除审计（task_id 仍可读，便于知道这个 id 曾经存在）
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) VALUES (?,?,?,?,?)",
        (role, tid, "delete", jdumps({
            "title": t.get("title"), "force": force,
            "consolidated_roles": consolidated,
            "consolidation_errors": consolidation_errors,
        }), now_iso()),
    )
    conn.commit()
    return True


def list_activities(conn: sqlite3.Connection, tid: str) -> list[dict]:
    """任务的评论 / @提及 / 运行迁移事件（与 audit 分离）。"""
    rows = conn.execute(
        "SELECT id, task_id, role_slug, run_id, kind, payload_json, created_at "
        "FROM activities WHERE task_id=? ORDER BY created_at, id", (tid,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = jloads(d.pop("payload_json"), {})
        out.append(d)
    return out


def add_dep(conn: sqlite3.Connection, tid: str, depends_on: list[str]) -> dict:
    for d in depends_on:
        conn.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)", (tid, d))
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def comment(conn: sqlite3.Connection, tid: str, role: str, body: str) -> dict:
    ledger.emit(
        conn, "message.comment", task_id=tid,
        actor_type="human" if role == "human" else "role", actor=role,
        payload={"body": body}, mirror_activity=True, activity_kind="comment",
        activity_role=role,
    )
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def mention(conn: sqlite3.Connection, tid: str, role: str, target: str, message: str) -> dict:
    """向目标（如 `human`）发起 @提及，用于人工确认点（HITL）的通知。"""
    ledger.emit(
        conn, "message.mention", task_id=tid,
        actor_type="human" if role == "human" else "role", actor=role,
        payload={"target": target, "message": message},
        mirror_activity=True, activity_kind="mention", activity_role=role,
    )
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def notify_downstream(conn: sqlite3.Connection, tid: str) -> int:
    """任务 done 后，@提及依赖它的下游任务的参与角色，作为「可以开工」的交接信号。"""
    downstream = conn.execute("SELECT task_id FROM task_deps WHERE depends_on=?", (tid,)).fetchall()
    for d in downstream:
        dtid = d["task_id"]
        roles = [x["role_slug"] for x in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (dtid,)
        )]
        for role in roles:
            mention(conn, dtid, "system", role, f"上游任务 {tid} 已完成，可以开始工作")
    return len(downstream)


def timeline(conn: sqlite3.Connection, tid: str) -> list[dict]:
    """任务会话时间线：合并 audit（任务动作）与 activities（评论 + 运行事件），按时间排序。

    这是「任务即会话」视图的数据源——多角色围绕一个任务的认领/处理/评论/交接/运行全记录。
    """
    out: list[dict] = []
    for r in conn.execute(
        "SELECT id, role_slug, task_id, NULL AS run_id, action AS kind, payload_json, created_at, "
        "NULL AS event_id, NULL AS workflow_id, NULL AS step_id, NULL AS slice_key, NULL AS execution_no "
        "FROM audit WHERE task_id=? ORDER BY created_at, id", (tid,)
    ).fetchall():
        out.append(_timeline_row("audit", r))
    for r in conn.execute(
        "SELECT id, role_slug, task_id, run_id, kind, payload_json, created_at, "
        "event_id, workflow_id, step_id, slice_key, execution_no "
        "FROM activities WHERE task_id=? ORDER BY created_at, id", (tid,)
    ).fetchall():
        out.append(_timeline_row("activity", r))
    out.sort(key=lambda x: (x["ts"], x["key"]))
    return out


def _timeline_row(src: str, r) -> dict:
    return {
        "key": f"{src}-{r['id']}",
        "ts": r["created_at"],
        "kind": r["kind"],
        "role": r["role_slug"],
        "task_id": r["task_id"],
        "run_id": r["run_id"],
        "seq": r["event_id"],
        "workflow_id": r["workflow_id"],
        "step_id": r["step_id"],
        "slice_key": r["slice_key"],
        "execution_no": r["execution_no"],
        "payload": jloads(r["payload_json"], {}),
    }


def _assert_claimable(conn: sqlite3.Connection, t: dict) -> None:
    """§5.4 认领守卫：depends_on 前置依赖须全部 done。"""
    for dep in t["depends_on"]:
        dt = get(conn, dep)
        if dt is None or dt["status"] != "done":
            raise ConflictError(f"dependency {dep} not done")


def _assert_submittable(conn: sqlite3.Connection, t: dict) -> None:
    """§5.1 提交守卫：父任务在所有子任务（parent_id==本任务）done 前不可 submit。"""
    _assert_claimable(conn, t)
    children = conn.execute("SELECT status FROM tasks WHERE parent_id=?", (t["id"],)).fetchall()
    if any(c["status"] != "done" for c in children):
        raise ConflictError("child tasks not all done")


def _assert_channel_member(conn: sqlite3.Connection, t: dict, role: str) -> None:
    """B 多频道认领守卫：任务有 workspace_id 时，角色须是 write/admin 成员。"""
    wid = t.get("workspace_id")
    if not wid:
        return
    access = workspace.member_access(conn, wid, role)
    if access not in ("write", "admin"):
        raise ConflictError(f"role {role} is not a write member of workspace {wid}")


def _audit(conn: sqlite3.Connection, role: str | None, task_id: str, action: str, payload: dict | None = None) -> None:
    ledger.emit(
        conn, f"audit.{action}", task_id=task_id,
        actor_type="human" if role == "human" else ("role" if role else "system"),
        actor=role, payload=payload or {}, mirror_audit=True, audit_action=action,
    )


def _denied(path: str, patterns: list[str] | None = None) -> bool:
    p = path[2:] if path.startswith("./") else path
    for prefix in (patterns or DENIED_PREFIXES):
        if "/" in prefix or "*" in prefix or "?" in prefix:
            if fnmatch.fnmatchcase(p, prefix):
                return True
        if p == prefix or p.startswith(prefix.rstrip("/") + "/"):
            return True
        if ("/" + prefix) in p:
            return True
    return False


def _safe_repo(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "")


def _ignore_patterns(t: dict) -> list[str]:
    """`.anvilignore`（仓库根 + 数据目录）叠加内置 deny 清单。"""
    patterns = list(DENIED_PREFIXES)
    bases: list[Path] = [config.DATA_DIR]
    if t.get("repo"):
        bases.insert(0, config.REPOS_DIR / _safe_repo(t["repo"]))
    for base in bases:
        p = base / ".anvilignore"
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def _git_lines(root: Path, args: list[str]) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20
        )
        return (r.stdout or "").splitlines() if r.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return []


def _collect_changed_paths(conn: sqlite3.Connection, t: dict, role: str | None,
                           provided: list[str] | None = None) -> list[str]:
    """合并 Agent 自报 + worktree 的 git status + 分支相对 base 的 diff。"""
    changed: set[str] = {p for p in (provided or []) if p}

    # worktree 未提交改动
    run = conn.execute(
        "SELECT worktree_path FROM runs WHERE task_id=? AND role_slug=? "
        "ORDER BY started_at DESC LIMIT 1", (t["id"], role or "")
    ).fetchone()
    wt = Path(run["worktree_path"]) if run and run["worktree_path"] else None
    if wt and wt.exists():
        # -uall 展开未跟踪目录为具体文件，避免 "src/" 这种目录级路径掩盖越界判断。
        for line in _git_lines(wt, ["status", "--porcelain", "-uall"]):
            path = line[3:] if len(line) > 3 else ""
            if " -> " in path:  # rename: old -> new
                path = path.split(" -> ", 1)[1]
            if path:
                changed.add(path.strip().strip('"'))

    # 已提交改动：base...agent/<slug>/<task>
    repo_name = t.get("repo")
    base = t.get("base_branch")
    if repo_name and base and role:
        repo = config.REPOS_DIR / _safe_repo(repo_name)
        branch = f"agent/{role}/{t['id']}"
        if (repo / ".git").exists():
            for path in _git_lines(repo, ["diff", "--name-only", f"{base}...{branch}"]):
                if path.strip():
                    changed.add(path.strip())
    return sorted(p for p in changed if not _is_anvil_managed(p))


def _in_scope(path: str, scope: list[str], access: list[str] | None = None) -> bool:
    # access 参数为三交集预留（M2：scope ∩ role access ∩ .anvilignore），M1 暂不启用。
    if not scope:
        return True
    return any(fnmatch.fnmatchcase(path, pat) for pat in scope)


def _with_deps(conn: sqlite3.Connection, d: dict) -> dict:
    d["depends_on"] = [x["depends_on"] for x in conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id=?", (d["id"],))]
    d["assignees"] = [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at", (d["id"],))]
    d["role_claims"] = _role_claims(conn, d["id"])
    return d


def _row(r) -> dict:
    d = dict(r)
    d["tags"] = jloads(d.pop("tags_json"), [])
    d["scope_paths"] = jloads(d.pop("scope_paths_json"), [])
    d["acceptance"] = jloads(d.pop("acceptance_json"), [])
    d["plan_seed"] = jloads(d.pop("plan_seed_json", None), None)
    return d
