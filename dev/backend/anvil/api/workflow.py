"""Workflow 端点：任务工作流查询、角色步骤提交、人工 gate 审核/重试。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..services.tasks import ConflictError
from ..services import reviews as review_service
from . import events

router = APIRouter()


@router.get("/tasks/{tid}/workflow")
def get_task_workflow(tid: str, conn=Depends(get_db)):
    wf = services.workflow.get_for_task(conn, tid)
    if wf is None:
        raise HTTPException(status_code=404, detail="task has no workflow")
    return wf


@router.get("/tasks/{tid}/workflows")
def list_task_workflows(tid: str, conn=Depends(get_db)):
    """任务的执行历史；旧执行保留为 terminated，不覆盖。"""
    return services.workflow.list_for_task(conn, tid)


@router.get("/workflows/{wid}")
def get_workflow(wid: str, conn=Depends(get_db)):
    wf = services.workflow.get_workflow(conn, wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return wf


def _assert_workflow_task(conn, wid: str, auth: Auth) -> dict:
    wf = services.workflow.get_workflow(conn, wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if auth.task_id and auth.task_id != wf["task_id"]:
        raise HTTPException(status_code=403, detail="session is bound to another task")
    if auth.role and auth.role != "human" and auth.task_id is None:
        t = services.tasks.get(conn, wf["task_id"]) or {}
        bound = set(t.get("assignees") or [])
        if t.get("assignee"):
            bound.add(t["assignee"])
        if t.get("planner"):
            bound.add(t["planner"])
        if bound and auth.role not in bound and not services.roles.is_onboarding(conn, auth.role):
            raise HTTPException(status_code=403, detail="role is not a participant of this task")
    return wf


@router.get("/workflows/{wid}/steps/{sid}/packet")
def get_packet(wid: str, sid: str, conn=Depends(get_db)):
    """Gate/decision review packet + immutable decisions."""
    wf = services.workflow.get_workflow(conn, wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    step = next((s for s in wf["steps"] if s["step_id"] == sid), None)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    authority = review_service.authority_of(step)
    active = (
        step["status"] in ("running", "submitted")
        and (step.get("gate") or step.get("kind") == "decision")
    )
    packet = review_service.ensure_packet(conn, wf, step, created_by="api") if active else None
    return {
        "step": step,
        "packet": packet,
        "decisions": review_service.decisions_for_step(conn, wid, sid),
        "can_decide": {
            "human": step["status"] == "submitted"
            or (step.get("kind") == "decision" and step["status"] == "running"),
            "role": step.get("kind") == "decision" and step["status"] == "running"
            and authority in ("ai", "joint"),
            "authority": authority,
            "stale_evidence": review_service.packet_has_stale_evidence(packet),
        },
    }


@router.post("/workflows/{wid}/steps/{sid}/packet/refresh")
def refresh_packet(wid: str, sid: str, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    """Rebuild the packet from the latest step evidence (human action)."""
    wf = services.workflow.get_workflow(conn, wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    step = next((s for s in wf["steps"] if s["step_id"] == sid), None)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    if step["status"] != "submitted":
        raise HTTPException(status_code=409, detail="step is not awaiting review")
    packet = review_service.ensure_packet(
        conn, wf, step, created_by="human", force=True)
    return {"packet": packet,
            "decisions": review_service.decisions_for_step(conn, wid, sid)}


@router.post("/workflows/{wid}/steps/{sid}/submit")
def submit_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    """角色提交当前步骤（anvil step submit）。"""
    _assert_workflow_task(conn, wid, auth)
    try:
        wf = services.workflow.submit_step(
            conn, wid, sid, auth.role or "", body.get("summary"), auth.run_id,
            body.get("evidence"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except review_service.EvidenceError as exc:
        raise HTTPException(status_code=422, detail=f"evidence_invalid: {exc}")
    events.publish("task", {"id": wf["task_id"], "kind": "step_submitted", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/decide")
def decide_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    """decision 步骤由决策角色 approve/reject（anvil step decide）。"""
    _assert_workflow_task(conn, wid, auth)
    try:
        wf = services.workflow.decide_step(
            conn, wid, sid, auth.role, bool(body.get("approve")), body.get("feedback"),
            auth.run_id, body.get("packet_id"), body.get("rework_targets"),
            body.get("reasons"), body.get("override_reason"),
            bool(body.get("acknowledge_stale")))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_decision", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/escalate")
def escalate_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
                  auth: Auth = Depends(get_auth)):
    """AI decider escalates a decision to a human with a reason/triggers."""
    _assert_workflow_task(conn, wid, auth)
    try:
        wf = services.workflow.escalate_step(
            conn, wid, sid, auth.role, str(body.get("reason") or ""),
            body.get("triggers"), body.get("proposed_decision"),
            body.get("proposed_feedback"), auth.run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_escalated", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/plan")
def plan_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
              auth: Auth = Depends(get_auth)):
    """plan_slices 步骤由执行角色提交切片计划（anvil step plan）。"""
    _assert_workflow_task(conn, wid, auth)
    try:
        wf = services.workflow.apply_slice_plan(
            conn, wid, sid, auth.role, body, run_id=auth.run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_planned", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/review")
def review_step(wid: str, sid: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    """人工审核 gate 步骤（通过/驳回）。"""
    try:
        wf = services.workflow.review_step(
            conn, wid, sid, bool(body.get("approve")), body.get("feedback"),
            body.get("reasons"), body.get("rework_targets"), body.get("packet_id"),
            body.get("override_reason"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_review", "step": sid})
    return wf


@router.post("/workflows/{wid}/steps/{sid}/retry")
def retry_step(wid: str, sid: str, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    """人工重试失败/驳回的步骤。"""
    try:
        wf = services.workflow.retry_step(conn, wid, sid)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    events.publish("task", {"id": wf["task_id"], "kind": "step_retry", "step": sid})
    return wf
