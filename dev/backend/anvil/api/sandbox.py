"""执行沙箱状态/自检端点（人类）。

Agent 不需要读这些路径/能力信息；状态与自检仅 human token 可用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import Auth, get_db, require_human
from ..sandbox.errors import SandboxUnavailable
from ..services import ledger as ledger_service
from ..services import sandbox as sandbox_service

router = APIRouter()


@router.get("/sandbox/status")
def sandbox_status(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return sandbox_service.status(conn)


@router.post("/sandbox/self-check")
def sandbox_self_check(body: dict | None = None, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    """真实金丝雀；``{"force": true}`` 忽略缓存重跑（修复环境后使用）。"""
    payload = body or {}
    result = sandbox_service.self_check(
        conn, force=bool(payload.get("force")),
        runtime_id=payload.get("runtime_id") or None)
    ledger_service.emit_audit(
        conn, role_slug="human", task_id=None,
        action="sandbox.self_check", payload=result)
    conn.commit()
    return result


@router.post("/sandbox/provision")
def sandbox_provision(body: dict | None = None, conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    """Windows WSL 一次性准备；必须显式 consent=true。"""
    consent = bool((body or {}).get("consent"))
    try:
        return sandbox_service.provision_strict(conn, consent=consent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SandboxUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "sandbox_provision_unavailable",
                    "message": str(exc)},
        ) from exc
