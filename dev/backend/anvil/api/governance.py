"""治理端点（M2）：租约回收 tick / 全场冻结 / Token 用量与预算 / 审计。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .. import services
from ..services import tokens as tokens_service
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import TaskOut
from . import events

router = APIRouter()


@router.post("/control/tick")
def control_tick(request: Request, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    """编排器 tick：回收 lease 过期的认领。测试旁路（test_auth）下 force 回收。"""
    force = bool(getattr(request.app.state, "test_auth", False))
    return services.governance.tick(conn, force=force)


@router.get("/control/freeze")
def get_freeze(conn=Depends(get_db)):
    return {"frozen": services.governance.is_frozen(conn)}


@router.post("/control/freeze")
def set_freeze(body: dict, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    return services.governance.set_freeze(conn, bool(body.get("frozen", False)))


@router.post("/control/freeze-all")
def freeze_all(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return services.governance.set_freeze(conn, True)


@router.post("/control/resume-all")
def resume_all(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return services.governance.set_freeze(conn, False)


@router.get("/budget")
def get_budget(conn=Depends(get_db)):
    """今日 Token 用量 + 四层闸门上限（前端只读，与 get_freeze/list_audit 一致无 token）。"""
    return services.governance.budget_status(conn)


@router.put("/budget")
def update_budget(body: dict, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """设置系统级/运行时级并发上限与每日 Token 预算（design §5.6 / §6.5）。

    只更新请求里出现的字段；`daily_token_budget: ""` 表示清除预算（不限）。
    """
    kwargs = {k: body[k] for k in (
        "max_parallel_system", "max_parallel_runtime", "daily_token_budget"
    ) if k in body}
    return services.governance.update_limits(conn, **kwargs)


@router.get("/tokens")
def token_summary(scope: str | None = None,
                  date_from: str | None = Query(default=None, alias="from"),
                  date_to: str | None = Query(default=None, alias="to"),
                  conn=Depends(get_db)):
    return tokens_service.summary(conn, scope=scope, date_from=date_from, date_to=date_to)


@router.get("/audit")
def list_audit(task_id: str | None = None, role: str | None = None,
               action: str | None = None,
               date_from: str | None = Query(default=None, alias="from"),
               date_to: str | None = Query(default=None, alias="to"),
               limit: int = 100, conn=Depends(get_db)):
    return services.governance.list_audit(
        conn, task_id=task_id, limit=limit, role=role, action=action,
        date_from=date_from, date_to=date_to,
    )
