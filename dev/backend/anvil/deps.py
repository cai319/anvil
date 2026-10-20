"""FastAPI dependencies: DB connection + session auth."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from . import config
from .db import open_conn
from .util import now_iso


@dataclass
class Auth:
    session: str
    role: str | None
    task_id: str | None = None  # 绑定的任务；非空时该会话只能操作这一个任务


def get_db(request: Request):
    """每请求独立连接（P1#2 修复：不跨线程共享 connection）。"""
    conn = open_conn(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_auth(
    request: Request,
    x_anvil_session: str | None = Header(default=None, alias="X-Anvil-Session"),
    x_anvil_role: str | None = Header(default=None, alias="X-Anvil-Role"),
) -> Auth:
    # 运行级会话（design §10 末段）：spawn 时为每个 run 发一个 token，绑定角色与任务。
    # 命中了就以会话表为准，忽略客户端自填的 X-Anvil-Role（防止冒充）。测试旁路也先查会话，
    # 这样「会话绑定」本身可被测试覆盖。
    if x_anvil_session:
        conn = open_conn(request.app.state.db_path)
        try:
            row = conn.execute(
                "SELECT role_slug, task_id, expires_at FROM sessions WHERE token=?",
                (x_anvil_session,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            if row["expires_at"] and row["expires_at"] < now_iso():
                raise HTTPException(status_code=401, detail="session expired")
            return Auth(session=x_anvil_session, role=row["role_slug"], task_id=row["task_id"])

    # 测试旁路（仅测试环境用；M2 上真 session 校验时须确保生产关死 ANVIL_TEST_AUTH）。
    test_auth = bool(getattr(request.app.state, "test_auth", False)) or os.environ.get("ANVIL_TEST_AUTH") == "1"
    if test_auth:
        return Auth(session=x_anvil_session or "test-session", role=x_anvil_role or "qa-role")
    if not x_anvil_session:
        raise HTTPException(status_code=401, detail="missing X-Anvil-Session")

    # 人/Web 的共享 token（轻校验，P3#13）；配置了 AUTH_TOKEN 则要求值匹配。
    if config.AUTH_TOKEN and x_anvil_session != config.AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid X-Anvil-Session")
    return Auth(session=x_anvil_session, role=x_anvil_role)


def require_human(auth: Auth = Depends(get_auth)) -> Auth:
    """人工确认点硬边界：Agent 角色令牌不得代替自然人执行审批/删除/重置（HITL）。"""
    if auth.role and auth.role != "human":
        raise HTTPException(status_code=403, detail="this operation requires a human")
    return auth
