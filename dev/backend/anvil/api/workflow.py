"""Workflow 端点：任务工作流查询、角色步骤提交、人工 gate 审核/重试。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db
from ..services.tasks import ConflictError
from . import events

router = APIRouter()


@router.get("/tasks/{tid}/workflow")
def get_task_workflow(tid: str, conn=Depends(get_db)):
    wf = services.workflow.get_for_task(conn, tid)
    if wf is None:
        raise HTTPException(status_code=404, detail="task has no workflow")
    return wf


@router.post("/workflows/{wid}/steps/{sid}/submit")
def submit_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    """角色提交当前步骤（anvil step submit）。"""
    try:
        wf = services.workflow.submit_step(conn, wid, sid, auth.role or "", body.get("summary"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_submitted", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/review")
def review_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    """人工审核 gate 步骤（通过/驳回）。"""
    try:
        wf = services.workflow.review_step(
            conn, wid, sid, bool(body.get("approve")), body.get("feedback"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_review", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/retry")
def retry_step(wid: str, sid: str, conn=Depends(get_db)):
    """人工重试失败/驳回的步骤。"""
    wf = services.workflow.retry_step(conn, wid, sid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    events.publish("task", {"id": wf["task_id"], "kind": "step_retry", "step": sid})
    return wf
