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


# 本地单用户模式下的共享 token。显式设置 ANVIL_AUTH_TOKEN 后只接受该值。
_SHARED_DEFAULT_TOKENS = {"anvil", "anvil-web"}


def _session_row(request: Request, token: str):
    conn = open_conn(request.app.state.db_path)
    try:
        return conn.execute(
            "SELECT role_slug, task_id, expires_at FROM sessions WHERE token=?",
            (token,),
        ).fetchone()
    finally:
        conn.close()


def token_is_valid(request: Request, token: str | None) -> bool:
    """所有 API 请求的统一 token 校验：运行级 session 或人类共享 token。"""
    if not token:
        return False
    row = _session_row(request, token)
    if row is not None:
        return not (row["expires_at"] and row["expires_at"] < now_iso())
    if config.AUTH_TOKEN:
        return token == config.AUTH_TOKEN
    return token in _SHARED_DEFAULT_TOKENS


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
        row = _session_row(request, x_anvil_session)
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

    # 人/Web 的共享 token：只代表人类，不能通过 X-Anvil-Role 冒充 Agent。
    if not token_is_valid(request, x_anvil_session):
        raise HTTPException(status_code=401, detail="invalid X-Anvil-Session")
    if x_anvil_role and x_anvil_role != "human":
        raise HTTPException(
            status_code=403,
            detail="shared token cannot impersonate an agent role; use the agent's run token",
        )
    return Auth(session=x_anvil_session, role="human")


def require_human(request: Request, auth: Auth = Depends(get_auth)) -> Auth:
    """人工确认点硬边界：只有显式 human 身份才能审批/删除/重置。

    `ANVIL_TEST_AUTH=1` 是测试专用全量旁路（默认 qa-role）；生产绝不能开启。
    """
    test_auth = bool(getattr(request.app.state, "test_auth", False)) or os.environ.get("ANVIL_TEST_AUTH") == "1"
    if test_auth and auth.role == "qa-role":
        return auth
    if auth.role != "human":
        raise HTTPException(status_code=403, detail="this operation requires a human")
    return auth
