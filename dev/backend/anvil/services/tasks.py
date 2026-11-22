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
from ..util import (ConflictError, jdumps, jloads, new_id, now_iso, read_text_file,
                    validate_component_id)
from . import (governance, knowledge, knowledge_binding, ledger, repo_catalog,
               task_workspace, wake as wake_service, workspace)

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
        "SELECT role_slug, status, summary, run_id, claimed_at, lease_expires_at, submitted_at, "
        "release_requested_at FROM task_role_claims WHERE task_id=? ORDER BY role_slug", (tid,)
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
                 parent_id: str | None = None, parent_step_id: str | None = None,
                 workspace_id: str | None = None,
                 repo: str | None = None) -> None:
    """记录任务创建审计，尤其是角色运行时创建的子任务。"""
    _audit(conn, actor, tid, "create", {
        "parent_id": parent_id,
        "parent_step_id": parent_step_id,
        "workspace_id": workspace_id,
        "repo": repo,
    })
    conn.commit()


def create(conn: sqlite3.Connection, *, id: str | None = None, title: str,
           type: str | None = None, priority=None, tags: list[str] | None = None,
           repo: str | None = None, source_repo: str | None = None,
           workspace_id: str | None = None, base_branch: str | None = None,
           scope_paths: list[str] | None = None, description: str | None = None,
           acceptance: list[str] | None = None, assignee: str | None = None,
           assignees: list[str] | None = None,
           plan_first: bool = False, planner: str | None = None,
           plan_seed: dict | None = None,
           created_by: str | None = None, parent_id: str | None = None,
           parent_step_id: str | None = None, due_at: str | None = None,
           depends_on: list[str] | None = None,
           knowledge_repos: list | None = None,
           knowledge_repos_source: str = "human",
           issue_board_enabled: bool = False,
           commit: bool = True) -> dict:
    tid = id or new_id()
    try:
        tid = validate_component_id(tid, label="task id")
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    if str(tid).startswith("__") or "::slice::" in str(tid):
        # `__global__` 是角色上下文槽保留 key；`::slice::` 是 context key 分隔符。
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
        parent_ws = conn.execute(
            "SELECT source_repo FROM task_workspaces WHERE task_id=?",
            (parent_id,)).fetchone()
        parent_source = parent_ws["source_repo"] if parent_ws is not None else None
        if source_repo is not None and parent_source is not None \
                and source_repo != parent_source:
            raise ConflictError(
                f"subtask source_repo must inherit parent {parent_id}: "
                f"expected {parent_source!r}, got {source_repo!r}")
        effective_source_repo = parent_source or source_repo or repo
    else:
        # `repo` 是知识主题；`source_repo` 是可选的 Git 数据来源（不再叫执行 repo）。
        effective_source_repo = source_repo if source_repo is not None else repo
    # 委派工单绑定创建时正在运行的 workflow/execution；旧 execution 的委派
    # 不应阻塞重试后的新 step。
    parent_wf_id = None
    parent_exec_no = None
    if parent_id and parent_step_id:
        wf_row = conn.execute(
            "SELECT id, execution_no FROM workflows "
            "WHERE task_id=? AND status='running' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (parent_id,)).fetchone()
        if wf_row is not None:
            parent_wf_id = wf_row["id"]
            parent_exec_no = int(wf_row["execution_no"] or 1)
    board_enabled = bool(issue_board_enabled)
    conn.execute(
        """INSERT INTO tasks (id, title, type, status, priority, tags_json, repo, workspace_id,
                              base_branch, scope_paths_json, description, acceptance_json,
                              assignee, plan_first, planner, plan_seed_json, created_by, parent_id,
                              parent_step_id, parent_workflow_id, parent_execution_no, due_at,
                              created_at, updated_at, issue_board_enabled)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, title, type, "todo", priority, jdumps(tags), repo, workspace_id, base_branch,
         jdumps(scope_paths or []), description, jdumps(acceptance or []),
         assignee, int(plan_first or board_enabled), planner,
         jdumps(plan_seed) if plan_seed else None,
         created_by, parent_id, parent_step_id, parent_wf_id, parent_exec_no, due_at,
         now, now, int(board_enabled)),
    )
    if board_enabled:
        from . import findings as findings_service
        findings_service.ensure_board(conn, tid)
    for d in (depends_on or []):
        conn.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)", (tid, d))
    for a in (assignees or []):
        conn.execute("INSERT OR IGNORE INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)", (tid, a, now))
    _init_role_claims(conn, tid, list(assignees or []), now)
    # 任务数据存储（与知识库完全分开）：先登记 workspace，目录在首次执行时创建。
    # 派遣子任务共享来源 task 的 workspace，不创建独立目录。
    ws_task_id = tid
    if type in task_workspace.DISPATCH_TASK_TYPES and parent_id:
        ws_task_id = task_workspace.owner_task_id(conn, parent_id)
    ws = task_workspace.ensure(
        conn, ws_task_id, source_repo=effective_source_repo, base_branch=base_branch,
        create_dir=False, commit=False)
    # 显式指定是人工决策：落库并加锁，后续 auto/Cindy 不再覆盖。
    if knowledge_repos:
        knowledge_binding.set_manual(
            conn, tid, knowledge_repos, source=knowledge_repos_source or "human",
            lock=True, commit=False)
    else:
        knowledge_binding.auto_bind(conn, {
            "id": tid, "title": title, "description": description, "tags": tags,
            "repo": repo, "source_repo": effective_source_repo,
            "workspace_id": workspace_id, "parent_id": parent_id,
            "workspace_path": ws.get("path") or "",
        }, commit=False)
    if commit:
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
    d["reviewer_roles"] = [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_reviewers WHERE task_id=? ORDER BY added_at, role_slug",
        (tid,)).fetchall()]
    _attach_workspaces(conn, [d])
    ws = conn.execute(
        "SELECT source_repo, path, kind, status FROM task_workspaces WHERE task_id=?",
        (tid,)).fetchone()
    if ws is None and d.get("type") in ("dispatch", "dispatched") and d.get("parent_id"):
        owner = task_workspace.owner_task_id(conn, tid)
        ws = conn.execute(
            "SELECT source_repo, path, kind, status FROM task_workspaces WHERE task_id=?",
            (owner,)).fetchone()
    d["source_repo"] = ws["source_repo"] if ws is not None else None
    d["task_workspace"] = dict(ws) if ws is not None else None
    knowledge_binding.attach_bindings(conn, [d])
    return d


def list_delegations(conn: sqlite3.Connection, parent_id: str) -> list[dict]:
    """父任务下的委派工单（含旧子任务），带父 step 与最新 run 的展示信息。

    派遣子任务由 list_dispatches 单独展示，不能混入委派列表。
    """
    if get(conn, parent_id) is None:
        raise LookupError(parent_id)
    rows = conn.execute(
        """SELECT t.*,
                  ws.title AS step_title,
                  r.id AS run_id, r.status AS run_status,
                  r.failure_class AS run_failure_class,
                  COALESCE(
                    (SELECT c.summary FROM task_role_claims c
                      WHERE c.task_id=t.id AND c.summary IS NOT NULL
                      ORDER BY c.submitted_at DESC LIMIT 1),
                    (SELECT json_extract(a.payload_json, '$.summary') FROM audit a
                      WHERE a.task_id=t.id AND a.action='submit'
                      ORDER BY a.id DESC LIMIT 1)
                  ) AS submitted_summary
             FROM tasks t
             LEFT JOIN workflow_steps ws
               ON ws.step_id=t.parent_step_id
              AND ws.workflow_id=(
                  SELECT w.id FROM workflows w WHERE w.task_id=t.parent_id
                  ORDER BY w.created_at DESC, w.rowid DESC LIMIT 1)
             LEFT JOIN runs r
               ON r.id=(SELECT x.id FROM runs x WHERE x.task_id=t.id
                        ORDER BY x.started_at DESC, x.rowid DESC LIMIT 1)
            WHERE t.parent_id=?
              AND COALESCE(t.type,'') NOT IN ('dispatch','dispatched')
            ORDER BY t.created_at, t.id""", (parent_id,)).fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        d["tags"] = jloads(d.pop("tags_json", "[]"), [])
        d["scope_paths"] = jloads(d.pop("scope_paths_json", "[]"), [])
        d["acceptance"] = jloads(d.pop("acceptance_json", "[]"), [])
        d["assignees"] = [x["role_slug"] for x in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at",
            (d["id"],))]
        out.append(d)
    return out


def _attach_workspaces(conn: sqlite3.Connection, tasks: list[dict]) -> list[dict]:
    """给任务附上频道引用（id/name/repo），避免前端只拿到 workspace_id。"""
    ids = {str(t["workspace_id"]) for t in tasks if t.get("workspace_id")}
    refs: dict[str, dict] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT id, name, repo FROM workspaces WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall():
            refs[str(row["id"])] = dict(row)
    for t in tasks:
        t["workspace"] = refs.get(str(t.get("workspace_id") or ""))
    return tasks


def assign_roles(conn: sqlite3.Connection, tid: str, roles: list[str]) -> dict:
    """绑定任务的参与角色（团队）。全量替换 task_assignees + 重置角色工作状态。"""
    task = get(conn, tid)
    if task is None:
        raise LookupError(tid)
    executing = _executing_claimed_roles(conn, tid)
    if executing:
        raise ConflictError(
            f"task {tid} still has executing role(s) with active claims: "
            f"{', '.join(sorted(executing))}; stop their runs before reassigning roles")
    _assert_assignable(conn, roles)
    removed = set(task.get("assignees") or []) - set(roles)
    if removed:
        rows = conn.execute(
            "SELECT DISTINCT ws.role_slug FROM workflow_steps ws "
            "JOIN workflows w ON w.id=ws.workflow_id "
            "WHERE w.task_id=? AND w.status='running' "
            "AND ws.role_slug IS NOT NULL",
            (tid,)).fetchall()
        referenced = sorted(removed & {row["role_slug"] for row in rows})
        if referenced:
            raise ConflictError(
                "cannot remove roles still referenced by the running workflow: "
                + ", ".join(referenced)
                + "; revise the workflow or remove the corresponding steps first")
    if task.get("issue_board_enabled"):
        reviewers = set(task.get("reviewer_roles") or [])
        dropped = reviewers - set(roles)
        if dropped:
            raise ConflictError(
                "issue board reviewers must remain task assignees; remove them via "
                "the reviewers endpoint first: " + ", ".join(sorted(dropped)))
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


def planning_conflict(conn: sqlite3.Connection, task: dict | None) -> str | None:
    """计划期锁定：human 直接改 Roster/拉起角色 run 会与 Cindy 的规划竞态。"""
    if task is None:
        return None
    running = conn.execute(
        "SELECT 1 FROM workflows WHERE task_id=? AND status='running' LIMIT 1",
        (task["id"],)).fetchone()
    if running:
        return ("task has a running workflow; use workflow steps instead of "
                "direct assign/execution")
    draft = conn.execute(
        "SELECT 1 FROM board_drafts WHERE source_task=? AND status='pending' LIMIT 1",
        (task["id"],)).fetchone()
    if draft:
        return ("task has a pending planning draft; approve or reject it "
                "before changing assignees")
    from . import roles as roles_service

    planner = str(task.get("planner") or "")
    rows = conn.execute(
        "SELECT role_slug FROM runs WHERE task_id=? "
        "AND status NOT IN ('completed','failed','abandoned')",
        (task["id"],)).fetchall()
    for row in rows:
        role = str(row["role_slug"] or "")
        if role and (role == planner or roles_service.is_onboarding(conn, role)):
            return ("task is being planned by the planner; wait for the draft "
                    "or re-dispatch")
    return None


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
    _attach_workspaces(conn, tasks)
    return knowledge_binding.attach_bindings(conn, tasks)


def update(conn: sqlite3.Connection, tid: str, **fields) -> dict | None:
    task = get(conn, tid)
    if task is None:
        return None
    now = now_iso()
    json_cols = {"tags": "tags_json", "scope_paths": "scope_paths_json", "acceptance": "acceptance_json"}
    if "tags" in fields and fields["tags"] is not None:
        fields["tags"] = knowledge.normalize_tags(fields["tags"])
    board_enabled = None
    if "issue_board_enabled" in fields and fields["issue_board_enabled"] is not None:
        board_enabled = bool(fields["issue_board_enabled"])
        fields["issue_board_enabled"] = int(board_enabled)
        if board_enabled:
            fields["plan_first"] = 1
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
    if board_enabled:
        from . import findings as findings_service
        findings_service.ensure_board(conn, tid)
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
            # 单负责人任务同样记录 claim，保证释放/提交/评审状态与团队任务一致。
            run = _active_run_for_role(conn, tid, role)
            conn.execute(
                "INSERT INTO task_role_claims (task_id, role_slug, status, run_id, claimed_at, "
                "lease_expires_at, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id, role_slug) DO UPDATE SET status='working', "
                "run_id=excluded.run_id, claimed_at=excluded.claimed_at, "
                "lease_expires_at=excluded.lease_expires_at, submitted_at=NULL, summary=NULL, "
                "release_requested_at=NULL, updated_at=excluded.updated_at",
                (tid, role, "working", run["id"] if run else None, now, lease, now),
            )
        _audit(conn, role, tid, "claim", {"paths": paths, "team": bool(t["assignees"])})
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


_EXECUTING_RUN_STATUSES = ("created", "projecting", "running")


def _executing_roles(conn: sqlite3.Connection, tid: str) -> set[str]:
    """本任务上仍在执行的角色的 run；这些角色不能立即释放 claim。"""
    rows = conn.execute(
        "SELECT DISTINCT role_slug FROM runs WHERE task_id=? "
        f"AND status IN ({','.join('?' for _ in _EXECUTING_RUN_STATUSES)})",
        (tid, *_EXECUTING_RUN_STATUSES),
    ).fetchall()
    return {r["role_slug"] for r in rows if r["role_slug"]}


def _executing_claimed_roles(conn: sqlite3.Connection, tid: str) -> set[str]:
    """仍在执行、且持有有效 claim 的角色；重新绑定 Roster 会删除这些 claim。"""
    rows = conn.execute(
        "SELECT DISTINCT c.role_slug FROM task_role_claims c "
        "JOIN runs r ON r.task_id=c.task_id AND r.role_slug=c.role_slug "
        "WHERE c.task_id=? AND c.status IN ('working','submitted','approved') "
        f"AND r.status IN ({','.join('?' for _ in _EXECUTING_RUN_STATUSES)})",
        (tid, *_EXECUTING_RUN_STATUSES),
    ).fetchall()
    return {r["role_slug"] for r in rows if r["role_slug"]}


def _reconcile_after_release(conn: sqlite3.Connection, tid: str, now: str,
                             released_roles: set[str]) -> None:
    """释放若干 claim 后，按剩余 working claim 维护任务负责人/状态。"""
    active = conn.execute(
        "SELECT role_slug FROM task_role_claims WHERE task_id=? AND status='working' "
        "ORDER BY claimed_at, role_slug LIMIT 1", (tid,)
    ).fetchone()
    task_row = conn.execute(
        "SELECT assignee, status FROM tasks WHERE id=?", (tid,)).fetchone()
    if task_row is None or task_row["status"] != "in_progress":
        return
    if active:
        assignee = task_row["assignee"]
        if not assignee or assignee in released_roles:
            assignee = active["role_slug"]
        conn.execute(
            "UPDATE tasks SET assignee=?, lease_expires_at=?, updated_at=? WHERE id=?",
            (assignee, now, now, tid),
        )
        return
    conn.execute(
        "UPDATE tasks SET status='todo', assignee=NULL, claimed_at=NULL, "
        "lease_expires_at=NULL, updated_at=? WHERE id=?", (now, tid),
    )


def _release_or_defer_claims(conn: sqlite3.Connection, tid: str, role: str | None,
                             team: bool, now: str) -> tuple[list[str], list[str]]:
    """释放 claim；仍有执行中 run 的角色只登记延迟释放请求。"""
    role_specific = bool(team) and bool(role) and role != "human"
    if role_specific:
        rows = conn.execute(
            "SELECT role_slug, status FROM task_role_claims "
            "WHERE task_id=? AND role_slug=? AND status IN ('pending','working')",
            (tid, role),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role_slug, status FROM task_role_claims "
            "WHERE task_id=? AND status IN ('pending','working')", (tid,)
        ).fetchall()

    executing = _executing_roles(conn, tid)
    released: list[str] = []
    deferred: list[str] = []
    for row in rows:
        slug = row["role_slug"]
        if slug in executing:
            conn.execute(
                "UPDATE task_role_claims SET release_requested_at=COALESCE(release_requested_at, ?), "
                "updated_at=? WHERE task_id=? AND role_slug=? AND status IN ('pending','working')",
                (now, now, tid, slug),
            )
            deferred.append(slug)
            continue
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, "
            "submitted_at=NULL, release_requested_at=NULL, updated_at=? "
            "WHERE task_id=? AND role_slug=? AND status IN ('pending','working')",
            (now, tid, slug),
        )
        released.append(slug)
    return released, deferred


def unclaim(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    """放弃任务认领。

    角色仍在执行本任务（run 处于 created/projecting/running）时不能立即释放
    claim，否则会出现“claim=released、角色仍在该 slice 执行”的矛盾状态。
    此时只登记 release_requested_at，等 run 结束后由调度器释放。
    """
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_not_terminal(t, "unclaim")
    now = now_iso()

    # 团队任务由具体角色释放时只影响该角色；human/单负责人任务释放整体认领。
    released, deferred = _release_or_defer_claims(
        conn, tid, role, bool(t["assignees"]), now)

    _reconcile_after_release(conn, tid, now, set(released))
    _audit(conn, role, tid, "unclaim", {
        "released": sorted(released),
        "deferred": sorted(deferred),
    })
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def process_release_requests(conn: sqlite3.Connection) -> list[dict]:
    """执行已登记的延迟释放：对应角色没有活跃执行 run 后把 claim 置 released。"""
    rows = conn.execute(
        "SELECT task_id, role_slug FROM task_role_claims "
        "WHERE release_requested_at IS NOT NULL ORDER BY task_id, role_slug"
    ).fetchall()
    done: list[dict] = []
    by_task: dict[str, set[str]] = {}
    now = now_iso()
    for row in rows:
        tid, slug = row["task_id"], row["role_slug"]
        if slug in _executing_roles(conn, tid):
            continue
        cur = conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, "
            "submitted_at=NULL, release_requested_at=NULL, updated_at=? "
            "WHERE task_id=? AND role_slug=? AND status='working'",
            (now, tid, slug),
        )
        if cur.rowcount == 0:
            # 已 submitted/approved 或已 released：请求失效，不再延迟。
            conn.execute(
                "UPDATE task_role_claims SET release_requested_at=NULL, updated_at=? "
                "WHERE task_id=? AND role_slug=?", (now, tid, slug))
            continue
        by_task.setdefault(tid, set()).add(slug)
        done.append({"task_id": tid, "role_slug": slug})
        _audit(conn, slug, tid, "unclaim", {"released": [slug], "deferred": True})
    for tid, roles in by_task.items():
        _reconcile_after_release(conn, tid, now, roles)
    conn.commit()
    return done


def heartbeat(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    conn.execute("UPDATE tasks SET lease_expires_at=?, updated_at=? WHERE id=?", (lease, now_iso(), tid))
    if role:
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
           role: str | None = None,
           force_terminate_workflow: bool = False) -> dict:
    from . import workflow as workflow_service
    # 与 create_workflow/finalize 共用 task 生命周期锁：人工强制收口后，新的
    # workflow 不能在“已终止旧 workflow、尚未写 in_review”的窗口里启动。
    with workflow_service.workflow_lifecycle_lock(
            conn, tid, required=True, wait_seconds=2.0):
        return _submit_locked(
            conn, tid, summary=summary, branch=branch,
            changed_paths=changed_paths, role=role,
            force_terminate_workflow=force_terminate_workflow)


def _submit_locked(conn: sqlite3.Connection, tid: str, *, summary: str | None = None,
                   branch: str | None = None, changed_paths: list[str] | None = None,
                   role: str | None = None,
                   force_terminate_workflow: bool = False) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_submittable(conn, t)
    running_workflow = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? AND status='running' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,)).fetchone()
    if running_workflow is not None:
        if not force_terminate_workflow or role not in (None, "human"):
            raise ConflictError(
                f"task {tid} has a running workflow {running_workflow['id']}; "
                "use workflow step submit, or terminate the workflow first")
        from . import workflow as workflow_service
        workflow_service.terminate_running_workflows(
            conn, tid, "人工强制收口并提交任务评审")
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
    # 派遣子任务：角色提交即完成，不进入人工评审。
    if t.get("type") in ("dispatch", "dispatched"):
        if role and t.get("assignee") and role != t["assignee"]:
            raise ConflictError(
                f"task {tid} is assigned to {t['assignee']}, not {role}")
        if role:
            claim = _role_claim(conn, tid, role)
            if claim is not None:
                conn.execute(
                    "UPDATE task_role_claims SET status='approved', summary=?, "
                    "submitted_at=COALESCE(submitted_at, ?), lease_expires_at=NULL, "
                    "updated_at=? WHERE task_id=? AND role_slug=?",
                    (summary, now, now, tid, role))
        cur = conn.execute(
            "UPDATE tasks SET status='done', lease_expires_at=NULL, "
            "review_rejected_at=NULL, updated_at=? "
            "WHERE id=? AND status='in_progress'", (now, tid))
        if cur.rowcount != 1:
            raise ConflictError(
                f"task {tid} state changed; refresh before submitting")
        _complete_runs(conn, tid, role)
        _audit(conn, role, tid, "submit", {
            "summary": summary, "branch": branch, "changed_paths": changed,
            "reported_paths": changed_paths, "final": True, "dispatch": True,
        })
        conn.commit()
        try:
            from . import dispatch as dispatch_service
            dispatch_service.on_task_done(conn, tid)
        except Exception:  # noqa: BLE001 —— 回切/回流失败不阻断任务完成
            pass
        return get(conn, tid)  # type: ignore[return-value]
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
        cur = conn.execute(
            "UPDATE tasks SET status='in_review', lease_expires_at=NULL, "
            "review_rejected_at=NULL, updated_at=? "
            "WHERE id=? AND status='in_progress'", (now, tid))
        if cur.rowcount != 1:
            raise ConflictError(
                f"task {tid} state changed; refresh before submitting")
        _complete_runs(conn, tid)
        if not _notify_delegator(conn, t, role):
            mention(conn, tid, role or "system", "human",
                    f"团队任务已全部提交，请评审（{', '.join(t['assignees'])}）")
        _audit(conn, role, tid, "submit", {"summary": summary, "branch": branch,
                                           "changed_paths": changed,
                                           "reported_paths": changed_paths,
                                           "final": True})
        conn.commit()
        return get(conn, tid)  # type: ignore[return-value]

    cur = conn.execute(
        "UPDATE tasks SET status='in_review', review_rejected_at=NULL, updated_at=? "
        "WHERE id=? AND status='in_progress'", (now, tid))
    if cur.rowcount != 1:
        raise ConflictError(
            f"task {tid} state changed; refresh before submitting")
    conn.execute(
        "UPDATE task_role_claims SET status='submitted', summary=?, submitted_at=?, "
        "lease_expires_at=NULL, updated_at=? WHERE task_id=? AND status IN ('pending','working')",
        (summary, now, now, tid),
    )
    _notify_delegator(conn, t, role)
    _audit(conn, role, tid, "submit", {"summary": summary, "branch": branch,
                                       "changed_paths": changed, "reported_paths": changed_paths})
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def review(conn: sqlite3.Connection, tid: str, approve: bool,
           role: str | None = None, *, feedback: str | None = None,
           notify_rework: bool = True, notify_parent: bool = True) -> dict:
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    if t["status"] != "in_review":
        raise ConflictError(f"task {tid} is {t['status']}, expected in_review")
    if approve:
        with_workflow = conn.execute(
            "SELECT id FROM workflows WHERE task_id=? AND status='running' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,)).fetchone()
        if with_workflow is not None:
            raise ConflictError(
                f"task {tid} still has a running workflow {with_workflow['id']}; "
                "terminate it before final review")
        latest = _current_workflow_id(conn, tid)
        child_sql = (
            "SELECT id FROM tasks WHERE parent_id=? "
            "AND COALESCE(type,'') NOT IN ('dispatch','dispatched') "
            "AND status NOT IN ('done','closed')")
        child_params: list = [tid]
        if latest:
            child_sql += " AND (parent_workflow_id IS NULL OR parent_workflow_id=?)"
            child_params.append(latest)
        pending_child = conn.execute(
            child_sql + " LIMIT 1", child_params).fetchone()
        if pending_child is not None:
            raise ConflictError(
                f"task {tid} still has open child task {pending_child['id']}; "
                "finish or close it before final review")
    if approve and t.get("issue_board_enabled"):
        from . import findings as findings_service
        findings_service.assert_ready_for_task_review(conn, tid)
    new_status = "done" if approve else "in_progress"
    now = now_iso()
    # 驳回后重新起租：角色若始终不返工，治理 tick 仍能回收任务；返工 run 存活时
    # `_run_liveness` 会续租，不会误回收。
    lease = (None if approve else
             (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat())
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE tasks SET status=?, lease_expires_at=?, review_rejected_at=?, "
            "updated_at=? WHERE id=? AND status='in_review'",
            (new_status, lease, None if approve else now, now, tid))
        if cur.rowcount != 1:
            raise ConflictError(
                f"task {tid} state changed; refresh before reviewing")
        if approve:
            conn.execute(
                "UPDATE task_role_claims SET status='approved', updated_at=? "
                "WHERE task_id=?", (now, tid))
        else:
            # 驳回：已提交的角色回到 working，可重新派发/继续修改
            conn.execute(
                "UPDATE task_role_claims SET status='working', submitted_at=NULL, "
                "lease_expires_at=?, updated_at=? "
                "WHERE task_id=? AND status='submitted'", (lease, now, tid))
        _audit(conn, role, tid, "review", {
            "approve": approve,
            **({"feedback": feedback[:2000]} if feedback else {}),
        })
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if approve:
        # 最终评审通过后，任务内不再有活跃执行；避免 suspended run/session 永久残留。
        _complete_runs(conn, tid)
        conn.commit()
        try:
            from . import dispatch as dispatch_service
            dispatch_service.on_task_done(conn, tid)
        except Exception:  # noqa: BLE001 —— 回切状态失败不阻断评审
            pass
        if notify_parent and t.get("parent_step_id"):
            _wake_parent_after_delegation(conn, t, role)
    elif notify_rework:
        _notify_task_rework(conn, t, role, feedback)
    try:
        from . import experience_capture
        if approve:
            experience_capture.capture_task_completion(conn, tid, actor=role)
        else:
            experience_capture.capture_review_rejection(conn, tid, reviewer=role, actor=role)
    except Exception:  # noqa: BLE001 —— 捕获失败不影响评审
        pass
    if approve:
        notify_downstream(conn, tid)  # 完成后 @提及下游角色，触发交接
    return get(conn, tid)  # type: ignore[return-value]


def _notify_task_rework(conn: sqlite3.Connection, task: dict, reviewer: str | None,
                        feedback: str | None) -> None:
    """最终评审驳回：唤醒 assignees/负责人返工；review_rejected_at 阻止调度器回弹。"""
    message = (feedback or "").strip() or "人工最终评审驳回，请按评审意见返工后重新提交。"
    targets = list(dict.fromkeys(
        [r for r in (task.get("assignees") or []) if r]
        + ([task["assignee"]] if task.get("assignee") else [])))
    for target in targets:
        mention(conn, task["id"], reviewer or "human", target,
                f"最终评审驳回：{message[:1000]}")
        wake_service.enqueue(
            conn, task_id=task["id"], role_slug=target,
            reason="task.review_rejected",
            note=(f"[最终评审驳回] 任务 {task['id']} 已回到 in_progress。"
                  f"评审意见：{message[:1000]}。请按意见返工后重新 "
                  "`anvil task submit`；不要重复 claim。"))
    if not targets:
        mention(conn, task["id"], reviewer or "human", "human",
                f"最终评审驳回，但任务没有 assignee；请指定返工角色。{message[:800]}")
    conn.commit()


def _wake_parent_after_delegation(conn: sqlite3.Connection, task: dict,
                                  actor: str | None) -> None:
    parent = _parent_step_target(conn, task)
    if parent is None:
        return
    wake_service.enqueue(
        conn, task_id=parent["task_id"], role_slug=parent["role_slug"],
        workflow_id=parent["workflow_id"], step_id=parent["step_id"],
        reason="delegation.accepted",
        note=(f"[委派已解除] 子任务 {task['id']} 已验收通过；父步骤 "
              f"{parent['step_id']} 的委派门已释放，继续推进并提交。"))
    conn.commit()


def _require_delegation(t: dict) -> None:
    if not t.get("parent_step_id"):
        raise ConflictError(f"task {t['id']} is not a delegation task")
    if t["status"] != "in_review":
        raise ConflictError(f"task {t['id']} is {t['status']}, expected in_review")


def accept_delegation(conn: sqlite3.Connection, tid: str, actor: str | None) -> dict:
    """派发者验收委派工单：子任务 done，父 step 的委派门释放。"""
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _require_delegation(t)
    review(conn, tid, True, actor, notify_parent=False)
    created = str(t.get("created_by") or "")
    delegator = created[len("agent:"):] if created.startswith("agent:") else ""
    if delegator and actor != delegator:
        mention(conn, tid, actor or "system", delegator,
                "委派已验收，请继续父 step")
    parent = _parent_step_target(conn, t)
    if parent is not None:
        wake_service.enqueue(
            conn, task_id=parent["task_id"], role_slug=parent["role_slug"],
            workflow_id=parent["workflow_id"], step_id=parent["step_id"],
            reason="delegation.accepted",
            note=(f"[委派已解除] 子任务 {tid} 已验收通过；父步骤 "
                  f"{parent['step_id']} 的委派门已释放，继续推进并提交。"))
    _audit(conn, actor, tid, "delegation.accept", {
        "delegator": t.get("created_by"), "owner": t.get("assignee"),
    })
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def reject_delegation(conn: sqlite3.Connection, tid: str, actor: str | None,
                      feedback: str) -> dict:
    """派发者打回委派工单：子任务回到 in_progress，并 @ 负责人返工。"""
    feedback = (feedback or "").strip()
    if not feedback:
        raise ConflictError("feedback is required when rejecting a delegation")
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _require_delegation(t)
    review(conn, tid, False, actor, feedback=feedback, notify_rework=False)
    if t.get("assignee"):
        mention(conn, tid, actor or "system", t["assignee"],
                f"委派打回：{feedback}")
        wake_service.enqueue(
            conn, task_id=tid, role_slug=t["assignee"],
            reason="delegation.rejected",
            note=(f"[委派打回] 子任务 {tid} 被派发者打回：{feedback[:500]}。"
                  "该任务仍由你认领（不要重复 claim）：按反馈返工后重新 "
                  "`anvil task submit`；父步骤会继续等待。"))
    _audit(conn, actor, tid, "delegation.reject", {
        "delegator": t.get("created_by"), "owner": t.get("assignee"),
        "feedback": feedback,
    })
    conn.commit()
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
    now = now_iso()
    cur = conn.execute(
        "UPDATE tasks SET status='closed', lease_expires_at=NULL, updated_at=? "
        "WHERE id=? AND status NOT IN ('done','closed')", (now, tid))
    if cur.rowcount != 1:
        raise ConflictError(f"task {tid} state changed; refresh before cancelling")
    _release_or_defer_claims(conn, tid, role, bool(t["assignees"]), now)
    # 委派子任务被人工终止 = 视为已解除：唤醒父 step 继续，避免门禁永久卡死。
    if t.get("parent_step_id") and t.get("parent_id"):
        parent = _parent_step_target(conn, t)
        if parent is not None:
            mention(conn, t["parent_id"], role or "system", parent["role_slug"],
                    f"委派 {tid} 已取消，视为解除；父步骤可继续")
            wake_service.enqueue(
                conn, task_id=parent["task_id"], role_slug=parent["role_slug"],
                workflow_id=parent["workflow_id"], step_id=parent["step_id"],
                reason="delegation.cancelled",
                note=(f"[委派已取消] 子任务 {tid} 被人工终止，视为已解除；"
                      f"父步骤 {parent['step_id']} 不再被阻塞，继续推进并提交。"))
    # 父任务终止：派遣子任务随父任务终止；先关闭子任务，再由下面按 source 标记 cancelled。
    child_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE parent_id=? "
        "AND COALESCE(type,'') IN ('dispatch','dispatched') "
        "AND status NOT IN ('done','closed')", (tid,)).fetchall()]
    for cid in child_ids:
        conn.execute(
            "UPDATE tasks SET status='closed', lease_expires_at=NULL, updated_at=? WHERE id=?",
            (now_iso(), cid))
        _release_or_defer_claims(conn, cid, None, False, now_iso())
        for row in conn.execute(
                "SELECT id, source_task_id FROM task_dispatches WHERE target_task_id=? "
                "AND status IN ('open','return_ready','returning')", (cid,)).fetchall():
            if row["source_task_id"] != tid:
                conn.execute(
                    "UPDATE task_dispatches SET status='cancelled', updated_at=? WHERE id=?",
                    (now_iso(), row["id"]))
    # 父任务终止：delegation 子任务同样关闭，避免子任务继续等待/阻塞后续流程。
    delegation_children = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE parent_id=? "
        "AND COALESCE(type,'') NOT IN ('dispatch','dispatched') "
        "AND parent_step_id IS NOT NULL "
        "AND status NOT IN ('done','closed')", (tid,)).fetchall()]
    for cid in delegation_children:
        conn.execute(
            "UPDATE tasks SET status='closed', lease_expires_at=NULL, updated_at=? "
            "WHERE id=? AND status NOT IN ('done','closed')", (now_iso(), cid))
        conn.execute(
            "UPDATE task_role_claims SET status='released', lease_expires_at=NULL, "
            "updated_at=? WHERE task_id=? AND status IN ('pending','working')",
            (now_iso(), cid))
        conn.execute(
            "UPDATE runs SET status='abandoned', ended_at=? WHERE task_id=? "
            "AND status IN ('created','projecting','running','suspended',"
            "'submitted','reviewing')", (now_iso(), cid))
    # closed 是失败/终止，不是成功完成：派遣任务不再自动回切。
    now = now_iso()
    target_dispatch = conn.execute(
        "SELECT id FROM task_dispatches WHERE target_task_id=? "
        "AND status IN ('open','return_ready','returning')", (tid,)).fetchall()
    for row in target_dispatch:
        conn.execute(
            "UPDATE task_dispatches SET status='no_return', updated_at=? WHERE id=?",
            (now, row["id"]))
    if target_dispatch:
        conn.execute(
            "UPDATE tasks SET dispatch_status='no_return', updated_at=? WHERE id=?",
            (now, tid))
    for row in conn.execute(
            "SELECT id, target_task_id FROM task_dispatches WHERE source_task_id=? "
            "AND status IN ('open','return_ready','returning')", (tid,)).fetchall():
        conn.execute(
            "UPDATE task_dispatches SET status='cancelled', updated_at=? WHERE id=?",
            (now, row["id"]))
        conn.execute(
            "UPDATE tasks SET dispatch_status='cancelled' WHERE id=?",
            (row["target_task_id"],))
    from . import knowledge_harvest  # 惰性导入，避免 services 循环
    knowledge_harvest.remove_task_reminder(conn, tid)
    _audit(conn, role, tid, "cancel")
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def reopen(conn: sqlite3.Connection, tid: str, role: str | None = None) -> dict:
    """重新打开已人工终止的任务，并接回已有执行。

    - 最新工作流 running → 任务回到 in_progress，由调度器继续原执行，
      不重新规划、不重新生成工作流。
    - 最新工作流 done → 任务进入 in_review，等待最终人工评审。
    - 无工作流 / 已 terminated/failed → todo，由人类决定重新派发或规划。
    """
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    _assert_can_reopen(t)
    latest = conn.execute(
        "SELECT status FROM workflows WHERE task_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,)).fetchone()
    status = "todo"
    if latest and latest["status"] == "running":
        status = "in_progress"
    elif latest and latest["status"] == "done":
        status = "in_review"
    cur = conn.execute(
        "UPDATE tasks SET status=?, assignee=NULL, claimed_at=NULL, "
        "lease_expires_at=NULL, archived_at=NULL, review_rejected_at=NULL, updated_at=? "
        "WHERE id=? AND status='closed'", (status, now_iso(), tid))
    if cur.rowcount != 1:
        raise ConflictError(f"task {tid} state changed; refresh before reopening")
    conn.execute(
        "UPDATE task_role_claims SET status='pending', summary=NULL, run_id=NULL, "
        "claimed_at=NULL, lease_expires_at=NULL, submitted_at=NULL, updated_at=? WHERE task_id=?",
        (now_iso(), tid),
    )
    # 取消时移除了未触发的收割提醒；只有已落板（有 workflow）的任务才补回。
    if conn.execute(
            "SELECT 1 FROM workflows WHERE task_id=? LIMIT 1", (tid,)
    ).fetchone() is not None:
        from . import knowledge_harvest  # 惰性导入，避免 services 循环
        knowledge_harvest.ensure_task_reminder_by_id(conn, tid)
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
    cur = conn.execute(
        "UPDATE tasks SET archived_at=?, updated_at=? "
        "WHERE id=? AND status IN ('done','closed') AND archived_at IS NULL",
        (now, now, tid))
    if cur.rowcount != 1:
        raise ConflictError(f"task {tid} state changed; refresh before archiving")
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

    children = conn.execute(
        "SELECT id FROM tasks WHERE parent_id=? "
        "AND COALESCE(type,'') NOT IN ('dispatch','dispatched')", (tid,)).fetchall()
    if children and not force:
        raise ConflictError(f"task {tid} has child tasks; delete them first or use force")

    # 派遣子任务随父任务级联删除，不作为阻塞子任务；先清理它们，避免孤儿顶层任务。
    dispatch_children = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE parent_id=? "
        "AND COALESCE(type,'') IN ('dispatch','dispatched')", (tid,)).fetchall()]
    for cid in dispatch_children:
        try:
            delete(conn, cid, force=True, role=role)
        except Exception:  # noqa: BLE001 —— 级联清理失败不应阻断父任务删除
            pass

    # 派遣记录不随来源删除而保留回切能力：来源删除后只标记软关联失效/不再回切。
    now = now_iso()
    for row in conn.execute(
            "SELECT id, target_task_id FROM task_dispatches WHERE source_task_id=? "
            "AND status IN ('open','return_ready','returning')", (tid,)).fetchall():
        conn.execute(
            "UPDATE task_dispatches SET status='cancelled', updated_at=? WHERE id=?",
            (now, row["id"]))
        conn.execute(
            "UPDATE tasks SET dispatch_status='cancelled' WHERE id=?",
            (row["target_task_id"],))
    for row in conn.execute(
            "SELECT id FROM task_dispatches WHERE target_task_id=? "
            "AND status IN ('open','return_ready','returning')", (tid,)).fetchall():
        conn.execute(
            "UPDATE task_dispatches SET status='no_return', updated_at=? WHERE id=?",
            (now, row["id"]))

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
        "SELECT role_slug FROM role_task_sessions "
        "WHERE task_id=? OR task_key=?", (tid, tid)).fetchall()}
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
                    task_key_value=daemon_service.task_key(tid),
                    all_slices=True)
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
        # 强制删除只解除父子关系；委派来源字段也必须同时清空，否则子任务会永远
        # 挂在一个不存在的 parent_step/workflow 上（委派门永远无法释放）。
        conn.execute(
            "UPDATE tasks SET parent_id=NULL, parent_step_id=NULL, "
            "parent_workflow_id=NULL, parent_execution_no=NULL "
            "WHERE parent_id=?", (tid,))

    # 任务删除时一并清掉其 workflow，避免孤儿步骤继续被调度器推进。
    workflow_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM workflows WHERE task_id=?", (tid,)).fetchall()]
    step_ids: list[str] = []
    for wf_id in workflow_ids:
        step_ids.extend(r["step_id"] for r in conn.execute(
            "SELECT step_id FROM workflow_steps WHERE workflow_id=?",
            (wf_id,)).fetchall())
    conn.execute("DELETE FROM gate_packets WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM decisions WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM workflow_dossiers WHERE task_id=?", (tid,))
    for wf_id in workflow_ids:
        conn.execute("DELETE FROM workflow_steps WHERE workflow_id=?", (wf_id,))
        conn.execute("DELETE FROM workflows WHERE id=?", (wf_id,))
    for step_id in step_ids:
        conn.execute(
            "DELETE FROM leases WHERE resource_type='step' AND resource_key=?",
            (step_id,))
    conn.execute(
        "DELETE FROM leases WHERE run_id IN (SELECT id FROM runs WHERE task_id=?)",
        (tid,))
    conn.execute("DELETE FROM context_manifests WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM wake_requests WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM action_cards WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM events WHERE task_id=?", (tid,))
    # 观测记录随任务清理（含 task_id 为空、仅 run 归属的历史行）。
    conn.execute(
        "DELETE FROM run_subagents WHERE task_id=? OR run_id IN "
        "(SELECT id FROM runs WHERE task_id=?)", (tid, tid))
    conn.execute("DELETE FROM runs WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_deps WHERE task_id=? OR depends_on=?", (tid, tid))
    conn.execute("DELETE FROM task_assignees WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_knowledge_repos WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_role_claims WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_reviewers WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_finding_boards WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_findings WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM task_finding_submissions WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM activities WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM sessions WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM role_inbox_cursors WHERE task_id=?", (tid,))
    # 任务上下文槽（daemon 未启用/角色已停机时也要清，避免孤儿槽被下次同名任务复用）
    conn.execute(
        "DELETE FROM role_task_sessions WHERE task_id=? OR task_key=?", (tid, tid))
    # 任务数据目录保留到保留期结束（可审计/可恢复）；标记 deleted，由定期清理删除。
    task_workspace.mark_deleted(conn, tid, commit=False)
    from . import knowledge_harvest  # 惰性导入，避免 services 循环
    knowledge_harvest.remove_task_reminder(conn, tid)
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


def comment(conn: sqlite3.Connection, tid: str, role: str, body: str,
            slice_key: str | None = None) -> dict:
    ledger.emit(
        conn, "message.comment", task_id=tid, slice_key=slice_key,
        actor_type="human" if role == "human" else "role", actor=role,
        payload={"body": body}, mirror_activity=True, activity_kind="comment",
        activity_role=role,
    )
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def context_note(conn: sqlite3.Connection, tid: str, role: str, body: str,
                 for_step: str | None = None) -> dict:
    """写入 task-wide、system 作者的备注，供未来 slice/step 的上下文使用。

    普通 comment 会被当前 run 的 slice 作用域限制，且 agent_inbox 会过滤自己发出的
    活动；这里用 role_slug=NULL、slice_key=NULL 的 comment 活动，保证任意未来切片可见，
    同时不产生 mention、不唤醒任何角色。
    """
    t = get(conn, tid)
    if t is None:
        raise LookupError(tid)
    body = (body or "").strip()
    if not body:
        raise ConflictError("note body is required")
    for_step = (for_step or "").strip() or None
    prefix = "系统备注" + (f"（给 {for_step}）" if for_step else "") + "："
    ledger.emit(
        conn, "message.context_note", task_id=tid, slice_key=None,
        actor_type="human" if role == "human" else "system",
        actor=role,
        payload={"body": prefix + body, "author": role, "for_step": for_step},
        mirror_activity=True, activity_kind="comment",
        activity_role=ledger.ACTIVITY_ROLE_NULL,
    )
    conn.commit()
    return get(conn, tid)  # type: ignore[return-value]


def mention(conn: sqlite3.Connection, tid: str, role: str, target: str,
            message: str, slice_key: str | None = None) -> dict:
    """向目标（如 `human`）发起 @提及，用于人工确认点（HITL）的通知。"""
    ledger.emit(
        conn, "message.mention", task_id=tid, slice_key=slice_key,
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


def waiting_for_human(conn: sqlite3.Connection, task_id: str | None,
                      role: str, slice_key: str | None = None) -> bool:
    """该角色最后一条 @ 是 @human 且之后没有人类回复 → 合法 HITL 等待。"""
    if not task_id:
        return False
    timeline = []
    for event in timeline_events(conn, task_id):
        value = event.get("slice_key")
        if slice_key:
            if value in (None, slice_key):
                timeline.append(event)
        elif value is None:
            timeline.append(event)
    own = [i for i, event in enumerate(timeline)
           if event["kind"] == "mention" and event["role"] == role]
    if not own:
        return False
    last = own[-1]
    target = (timeline[last].get("payload") or {}).get("target")
    if target != "human":
        return False
    # 人类评论/mention 都视为已回应；保持与旧看门狗行为一致。
    return not any(e["role"] == "human" for e in timeline[last + 1:])


def timeline_events(conn: sqlite3.Connection, tid: str) -> list[dict]:
    """timeline 的别名；避免调用方为了一个判断反复拼 audit/activities。"""
    return timeline(conn, tid)


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


def _current_workflow_id(conn: sqlite3.Connection, tid: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,)).fetchone()
    return row["id"] if row else None


def _assert_submittable(conn: sqlite3.Connection, t: dict) -> None:
    """§5.1 提交守卫：当前 execution 的委派未 done/closed 前父任务不可 submit。

    派遣子任务是空闲角色临时抽离的小切片，明确不阻塞父任务提交。
    closed（人工取消）视为已解除；旧 execution 的残留委派不阻塞新执行。
    """
    _assert_claimable(conn, t)
    latest = _current_workflow_id(conn, t["id"])
    sql = ("SELECT 1 FROM tasks WHERE parent_id=? "
           "AND COALESCE(type,'') NOT IN ('dispatch','dispatched') "
           "AND status NOT IN ('done','closed')")
    params: list = [t["id"]]
    if latest:
        sql += " AND (parent_workflow_id IS NULL OR parent_workflow_id=?)"
        params.append(latest)
    if conn.execute(sql + " LIMIT 1", params).fetchone() is not None:
        raise ConflictError("child tasks not all done")


def _parent_step_target(conn: sqlite3.Connection, t: dict) -> dict | None:
    """委派子任务对应的父 step 目标（task/workflow/step/role）。

    parent_workflow_id 优先；历史数据没有该字段时回退到父任务当前 running workflow。
    """
    parent_id = t.get("parent_id")
    step_id = t.get("parent_step_id")
    if not parent_id or not step_id:
        return None
    wf_id = t.get("parent_workflow_id")
    if wf_id:
        row = conn.execute(
            "SELECT role_slug FROM workflow_steps WHERE workflow_id=? AND step_id=?",
            (wf_id, step_id)).fetchone()
    else:
        row = conn.execute(
            "SELECT ws.role_slug, ws.workflow_id FROM workflow_steps ws "
            "JOIN workflows w ON w.id=ws.workflow_id "
            "WHERE w.task_id=? AND ws.step_id=? AND w.status='running' "
            "ORDER BY w.created_at DESC, w.rowid DESC LIMIT 1",
            (parent_id, step_id)).fetchone()
        if row is not None:
            wf_id = row["workflow_id"]
    if row is None or not row["role_slug"]:
        return None
    return {"task_id": parent_id, "workflow_id": wf_id, "step_id": step_id,
            "role_slug": row["role_slug"]}


def _notify_delegator(conn: sqlite3.Connection, t: dict, actor: str | None) -> bool:
    """委派提交后通知派发者验收；同角色自委派时通知 human。"""
    if not t.get("parent_step_id"):
        return False
    created = str(t.get("created_by") or "")
    if not created.startswith("agent:"):
        return False
    delegator = created[len("agent:"):]
    target = "human" if delegator == (actor or "") else delegator
    mention(conn, t["id"], actor or "system", target, "委派已提交，请验收")
    if target != "human":
        # 父任务时间线也留一条，派发者切回父任务时能在会话里看到。
        mention(conn, t["parent_id"], actor or "system", target,
                f"委派 {t['id']}《{t.get('title') or ''}》已提交，请验收")
        parent = _parent_step_target(conn, t)
        if parent is not None:
            wake_service.enqueue(
                conn, task_id=parent["task_id"], role_slug=delegator,
                workflow_id=parent["workflow_id"], step_id=parent["step_id"],
                reason="delegation.submitted",
                note=(f"[委派待验收] 子任务 {t['id']}《{t.get('title') or ''}》"
                      f"已提交，父步骤 {parent['step_id']} 暂停等待验收。"
                      "先 `anvil task show` 查看产出，再执行 delegation-accept 或 "
                      "delegation-reject；处理完继续父步骤。"))
    return True


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
            lines = read_text_file(p).splitlines()
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
    if "issue_board_enabled" in d:
        d["issue_board_enabled"] = bool(d["issue_board_enabled"])
    return d
