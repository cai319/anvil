"""FastAPI dependencies: DB connection + session auth."""
from __future__ import annotations

import hmac
import os
import sqlite3
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query, Request

from . import config
from .db import open_conn
from .util import now_iso


@dataclass
class Auth:
    session: str
    role: str | None
    task_id: str | None = None  # 绑定的任务；非空时该会话只能操作这一个任务
    run_id: str | None = None    # 运行级会话绑定的 run；用于 stale-run 围栏
    test_bypass: bool = False    # ANVIL_TEST_AUTH 旁路；不代表真实 Agent 身份


# 不再接受公开默认 token；人类 token 由 config.resolve_auth_token() 生成或显式配置。
# 运行级 session token 仍然有效，但只代表其绑定角色。


def _session_row(request: Request, token: str):
    conn = open_conn(request.app.state.db_path)
    try:
        return conn.execute(
            "SELECT role_slug, task_id, run_id, expires_at FROM sessions WHERE token=?",
            (token,),
        ).fetchone()
    finally:
        conn.close()


def token_is_valid(request: Request, token: str | None) -> bool:
    """所有 API 请求的统一 token 校验：运行级 session 或人类 secret。"""
    if not token:
        return False
    row = _session_row(request, token)
    if row is not None:
        return not (row["expires_at"] and row["expires_at"] < now_iso())
    return bool(config.AUTH_TOKEN) and hmac.compare_digest(token, config.AUTH_TOKEN)


def get_db(request: Request):
    """每请求独立连接（P1#2 修复：不跨线程共享 connection）。"""
    conn = open_conn(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _resolve_auth(request: Request, session_token: str | None,
                  declared_role: str | None) -> Auth:
    """统一鉴权实现；get_auth / get_event_auth 共用，保证算法唯一。"""
    # 运行级会话（design §10 末段）：spawn 时为每个 run 发一个 token，绑定角色与任务。
    # 命中了就以会话表为准，忽略客户端自填的 X-Anvil-Role（防止冒充）。测试旁路也先查会话，
    # 这样「会话绑定」本身可被测试覆盖。
    if session_token:
        row = _session_row(request, session_token)
        if row is not None:
            if row["expires_at"] and row["expires_at"] < now_iso():
                raise HTTPException(status_code=401, detail="session expired")
            return Auth(session=session_token, role=row["role_slug"],
                        task_id=row["task_id"], run_id=row["run_id"])

    # 测试旁路（仅测试环境用；M2 上真 session 校验时须确保生产关死 ANVIL_TEST_AUTH）。
    test_auth = bool(getattr(request.app.state, "test_auth", False)) or os.environ.get("ANVIL_TEST_AUTH") == "1"
    if test_auth:
        return Auth(
            session=session_token or "test-session",
            role=declared_role or "qa-role",
            test_bypass=True,
        )
    if not session_token:
        raise HTTPException(status_code=401, detail="missing X-Anvil-Session")

    # 人/Web 的共享 token：只代表人类，不能通过 X-Anvil-Role 冒充 Agent。
    if not token_is_valid(request, session_token):
        raise HTTPException(status_code=401, detail="invalid X-Anvil-Session")
    if declared_role and declared_role != "human":
        raise HTTPException(
            status_code=403,
            detail="shared token cannot impersonate an agent role; use the agent's run token",
        )
    return Auth(session=session_token, role="human")


def get_auth(
    request: Request,
    x_anvil_session: str | None = Header(default=None, alias="X-Anvil-Session"),
    x_anvil_role: str | None = Header(default=None, alias="X-Anvil-Role"),
) -> Auth:
    return _resolve_auth(request, x_anvil_session, x_anvil_role)


def get_event_auth(
    request: Request,
    token: str | None = Query(default=None, description="SSE-only query token"),
    x_anvil_session: str | None = Header(default=None, alias="X-Anvil-Session"),
    x_anvil_role: str | None = Header(default=None, alias="X-Anvil-Role"),
) -> Auth:
    """SSE 专用鉴权：EventSource 无法设置请求头，只对 events 路由接受 ?token=。

    头优先：同时携带时以 X-Anvil-Session 为准，避免无效头被静默降级成 query 凭据。
    其他路由必须继续使用 get_auth，防止 query token 扩散。
    """
    return _resolve_auth(request, x_anvil_session or token, x_anvil_role)


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
