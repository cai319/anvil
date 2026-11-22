"""Agent 只读访问边界：任务参与范围 + 运行/工作区归属。

人类（共享 token）不受限；Agent 运行级/角色级 token 只能读取自己绑定或参与的
任务，以及这些任务关联的 run / workspace 内容。写入边界仍由各端点自己的
require_human / 状态机校验负责。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from ..deps import Auth


def is_agent(auth: Auth) -> bool:
    # ANVIL_TEST_AUTH 是测试环境的全量旁路身份，不代表真实 Agent 会话；
    # 对象级授权只对真实 session token 生效（安全测试用真实 sessions 覆盖）。
    return bool(auth.role and auth.role != "human" and not auth.test_bypass)


def _assert_task_bound(conn: sqlite3.Connection, auth: Auth, tid: str) -> None:
    if auth.task_id and auth.task_id != tid:
        raise HTTPException(status_code=403, detail="session is bound to another task")


def assert_task_bound(conn: sqlite3.Connection, auth: Auth, tid: str) -> None:
    """只校验运行级 token 的任务绑定；用于 claim 等“可竞争”操作。

    任务被他人认领时由 claim 返回 409（可换任务），而不是 403（越权）。
    """
    _assert_task_bound(conn, auth, tid)


def can_task_read(conn: sqlite3.Connection, auth: Auth, tid: str) -> bool:
    """不抛异常的参与范围判定，用于 list 过滤。"""
    try:
        assert_task_read(conn, auth, tid)
        return True
    except HTTPException:
        return False


def assert_task_read(conn: sqlite3.Connection, auth: Auth, tid: str) -> None:
    """Agent 读取任务数据前校验参与范围；人类直接放行。"""
    if not is_agent(auth):
        return
    _assert_task_bound(conn, auth, tid)
    row = conn.execute(
        "SELECT assignee, planner FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    if row is None:
        return  # 由调用方决定 404
    if auth.task_id:
        return  # 运行级 token 与 URL 任务一致
    bound: set[str] = set()
    if row["assignee"]:
        bound.add(str(row["assignee"]))
    if row["planner"]:
        bound.add(str(row["planner"]))
    bound.update(
        str(r["role_slug"]) for r in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (tid,)
        ).fetchall()
    )
    bound.update(
        str(r["role_slug"]) for r in conn.execute(
            "SELECT role_slug FROM task_reviewers WHERE task_id=?", (tid,)
        ).fetchall()
    )
    if not bound:
        return  # 开放任务：允许任意角色查看/认领
    if auth.role in bound:
        return
    from ..services import roles as roles_service
    if roles_service.is_onboarding(conn, auth.role or ""):
        return
    raise HTTPException(status_code=403, detail="agent is not a participant of this task")


def _is_onboarding(conn: sqlite3.Connection, role: str | None) -> bool:
    if not role:
        return False
    from ..services import roles as roles_service
    return roles_service.is_onboarding(conn, role)


def _workspace_member_write(conn: sqlite3.Connection, ws_id: str,
                            role: str | None) -> bool:
    if not role:
        return False
    row = conn.execute(
        "SELECT access FROM workspace_members WHERE workspace_id=? AND role_slug=?",
        (ws_id, role)).fetchone()
    return bool(row and row["access"] in ("write", "admin"))


def _task_in_workspace_for_role(conn: sqlite3.Connection, ws_id: str,
                                role: str | None, *, task_id: str | None = None) -> bool:
    if not role:
        return False
    sql = (
        "SELECT 1 FROM tasks t WHERE t.workspace_id=? AND ("
        " t.assignee=? OR t.planner=? OR EXISTS("
        "   SELECT 1 FROM task_assignees a WHERE a.task_id=t.id AND a.role_slug=?"
        " ))"
    )
    params: list = [ws_id, role, role, role]
    if task_id:
        sql += " AND t.id=?"
        params.append(task_id)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def assert_workspace_read(conn: sqlite3.Connection, auth: Auth, ws_id: str) -> None:
    """Agent 只能读取自己参与任务所在的频道内容。"""
    if not is_agent(auth):
        return
    if _is_onboarding(conn, auth.role):
        return
    if _task_in_workspace_for_role(conn, ws_id, auth.role):
        return
    raise HTTPException(
        status_code=403, detail="agent has no participating task in this workspace")


def assert_workspace_write(conn: sqlite3.Connection, auth: Auth, ws_id: str,
                           task_id: str | None = None) -> None:
    """Agent 只能向自己参与任务所在频道写；task-bound token 必须绑定同一任务。"""
    if not is_agent(auth):
        return
    if auth.task_id:
        if task_id and task_id != auth.task_id:
            raise HTTPException(
                status_code=403, detail="session is bound to another task")
        if not _task_in_workspace_for_role(
                conn, ws_id, auth.role, task_id=auth.task_id):
            raise HTTPException(
                status_code=403,
                detail="agent is not a participant of this workspace task")
        return
    if _is_onboarding(conn, auth.role) or _workspace_member_write(conn, ws_id, auth.role):
        return
    if _task_in_workspace_for_role(conn, ws_id, auth.role, task_id=task_id):
        return
    raise HTTPException(
        status_code=403, detail="agent cannot write this workspace's knowledge")


def can_workspace_read(conn: sqlite3.Connection, auth: Auth, ws_id: str) -> bool:
    try:
        assert_workspace_read(conn, auth, ws_id)
        return True
    except HTTPException:
        return False


def assert_role_self(conn: sqlite3.Connection, auth: Auth, slug: str) -> None:
    """角色私有配置/指标/会话只能自己或 human 读取（onboarding 也不行）。"""
    if not is_agent(auth):
        return
    if auth.role != slug:
        raise HTTPException(status_code=403, detail="agent cannot read another role")


def assert_role_read(conn: sqlite3.Connection, auth: Auth, slug: str) -> None:
    """角色元数据读取：自己、onboarding 规划者或 human。"""
    if not is_agent(auth):
        return
    if auth.role == slug or _is_onboarding(conn, auth.role):
        return
    raise HTTPException(status_code=403, detail="agent cannot read another role")


def assert_run_read(conn: sqlite3.Connection, auth: Auth, run: dict) -> None:
    """Agent 只能读取自己的 run，或自己参与任务下的 run。"""
    if not is_agent(auth):
        return
    if auth.task_id:
        # 运行级 token 严格绑定一个任务，不能借“同角色”跨任务读取。
        if str(run.get("task_id") or "") != auth.task_id:
            raise HTTPException(status_code=403, detail="session is bound to another task")
        return
    if run.get("role_slug") == auth.role:
        return
    if run.get("task_id"):
        assert_task_read(conn, auth, str(run["task_id"]))
        return
    raise HTTPException(status_code=403, detail="run belongs to another role")
