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
    return bool(auth.role and auth.role != "human")


def _assert_task_bound(conn: sqlite3.Connection, auth: Auth, tid: str) -> None:
    if auth.task_id and auth.task_id != tid:
        raise HTTPException(status_code=403, detail="session is bound to another task")


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
    if not bound:
        return  # 开放任务：允许任意角色查看/认领
    if auth.role in bound:
        return
    from ..services import roles as roles_service
    if roles_service.is_onboarding(conn, auth.role or ""):
        return
    raise HTTPException(status_code=403, detail="agent is not a participant of this task")


def assert_workspace_read(conn: sqlite3.Connection, auth: Auth, ws_id: str) -> None:
    """Agent 只能读取自己参与任务所在的频道内容。"""
    if not is_agent(auth):
        return
    row = conn.execute(
        "SELECT 1 FROM tasks t WHERE t.workspace_id=? AND ("
        "  t.assignee=? OR t.planner=? OR EXISTS("
        "    SELECT 1 FROM task_assignees a WHERE a.task_id=t.id AND a.role_slug=?"
        "  )) LIMIT 1",
        (ws_id, auth.role, auth.role, auth.role),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=403, detail="agent has no participating task in this workspace")


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
