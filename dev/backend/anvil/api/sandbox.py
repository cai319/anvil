"""执行沙箱状态/自检端点（人类）。

Agent 不需要读这些路径/能力信息；状态与自检仅 human token 可用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Auth, get_db, require_human
from ..services import ledger as ledger_service
from ..services import sandbox as sandbox_service

router = APIRouter()


@router.get("/sandbox/status")
def sandbox_status(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return sandbox_service.status(conn)


@router.post("/sandbox/self-check")
def sandbox_self_check(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    result = sandbox_service.self_check()
    ledger_service.emit_audit(
        conn, role_slug="human", task_id=None,
        action="sandbox.self_check", payload=result)
    conn.commit()
    return result
