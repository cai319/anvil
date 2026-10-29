"""事件账本只读端点（与 /events SSE 区分）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import Auth, get_auth, get_db
from ..services import ledger
from . import access

router = APIRouter()


@router.get("/tasks/{tid}/events")
def task_events(tid: str, after_id: int = 0, limit: int = 100,
                kinds: str | None = None, workflow_id: str | None = None,
                conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    kind_list = [k.strip() for k in (kinds or "").split(",") if k.strip()] or None
    return ledger.list_task_events(
        conn, tid, after_id=after_id, limit=limit, kinds=kind_list,
        workflow_id=workflow_id)


@router.get("/event-log")
def event_log(task_id: str | None = None, workflow_id: str | None = None,
              event_type: str | None = None, after_id: int = 0,
              limit: int = 100, conn=Depends(get_db),
              auth: Auth = Depends(get_auth)):
    if access.is_agent(auth):
        if not task_id:
            raise HTTPException(
                status_code=403, detail="agent event-log queries require task_id")
        access.assert_task_read(conn, auth, task_id)
    return ledger.list_events(
        conn, task_id=task_id, workflow_id=workflow_id,
        event_type=event_type, after_id=after_id, limit=limit)
