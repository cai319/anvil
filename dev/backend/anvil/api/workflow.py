"""Workflow 端点：任务工作流查询、角色步骤提交、人工 gate 审核/重试。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..services.tasks import ConflictError
from ..services import dossier as dossier_service
from ..services import leases as lease_service
from ..services import metrics as metrics_service
from ..services import replay as replay_service
from ..services import reviews as review_service
from ..services import weights as weight_service
from . import events

router = APIRouter()


def _attach_pending_revision(conn, wf: dict) -> dict:
    drafts = services.board.list_drafts(conn, source_task=wf["task_id"])
    rev = next((d for d in drafts if d.get("kind") == "workflow_revision"
                and d.get("status") == "pending"
                and (d.get("tree") or {}).get("base_workflow_id") == wf["id"]), None)
    if rev:
        wf["pending_revision"] = {
            "draft_id": rev["id"],
            "impact": (rev.get("tree") or {}).get("impact") or {},
            "created_by": rev.get("created_by"),
        }
    return wf


@router.get("/tasks/{tid}/workflow")
def get_task_workflow(tid: str, conn=Depends(get_db)):
    wf = services.workflow.get_for_task(conn, tid)
    if wf is None:
        raise HTTPException(status_code=404, detail="task has no workflow")
    return _attach_pending_revision(conn, wf)


@router.get("/tasks/{tid}/workflows")
def list_task_workflows(tid: str, conn=Depends(get_db)):
    """任务的执行历史；旧执行保留为 terminated，不覆盖。"""
    return services.workflow.list_for_task(conn, tid)


@router.get("/workflows/{wid}")
def get_workflow(wid: str, conn=Depends(get_db)):
    wf = services.workflow.get_workflow(conn, wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    wf["dispatch"] = services.workflow.explain_dispatch(conn, wf)
    return _attach_pending_revision(conn, wf)


@router.get("/workflows/{wid}/metrics")
def get_metrics(wid: str, conn=Depends(get_db)):
    try:
        return metrics_service.compute(conn, wid)
    except LookupError:
        raise HTTPException(status_code=404, detail="workflow not found")


@router.get("/workflows/{wid}/graph")
def get_graph(wid: str, mode: str = "actual", conn=Depends(get_db)):
    try:
        if mode == "planned":
            return metrics_service.planned_graph(conn, wid)
        if mode == "diff":
            return metrics_service.graph_diff(conn, wid)
        return metrics_service.actual_graph(conn, wid)
    except LookupError:
        raise HTTPException(status_code=404, detail="workflow not found")


@router.get("/workflows/{wid}/dossier")
def get_dossier(wid: str, format: str = "json", conn=Depends(get_db)):
    payload = dossier_service.get(conn, wid)
    if payload is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if format == "md":
        body = dossier_service.render_md(payload)
        return Response(
            content=body, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="dossier-{wid[:8]}.md"'})
    return payload


@router.post("/workflows/{wid}/dossier/rebuild")
def rebuild_dossier(wid: str, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    payload = dossier_service.persist(conn, wid)
    if payload is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return payload


@router.get("/tasks/{tid}/dossier")
def get_task_dossier(tid: str, workflow_id: str | None = None,
                     format: str = "json", conn=Depends(get_db)):
    wid = workflow_id
    if wid is None:
        row = conn.execute(
            "SELECT id FROM workflows WHERE task_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,)).fetchone()
        wid = row["id"] if row else None
    if wid is None:
        raise HTTPException(status_code=404, detail="task has no workflow")
    return get_dossier(wid, format=format, conn=conn)


@router.get("/tasks/{tid}/replay/log")
def download_replay_log(tid: str, conn=Depends(get_db)):
    """Export the complete task replay (metadata + workflow + event stream) as .log."""
    try:
        content = replay_service.render_log(conn, tid)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    filename = replay_service.log_filename(tid)
    return Response(
        content=content, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/tasks/{tid}/leases")
def list_leases(tid: str, conn=Depends(get_db)):
    steps = [dict(r) for r in conn.execute(
        "SELECT s.workflow_id, s.step_id, s.status, s.lease_id, s.fence_token, "
        "l.holder, l.expires_at, l.status AS lease_status "
        "FROM workflow_steps s JOIN workflows w ON w.id=s.workflow_id "
        "LEFT JOIN leases l ON l.id=s.lease_id "
        "WHERE w.task_id=? AND s.lease_id IS NOT NULL "
        "ORDER BY s.updated_at DESC", (tid,)).fetchall()]
    return {"paths": lease_service.for_task(conn, tid), "steps": steps}


@router.post("/tasks/{tid}/reservations")
def reserve_paths(tid: str, body: dict, conn=Depends(get_db),
                  auth: Auth = Depends(get_auth)):
    paths = body.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise HTTPException(status_code=422, detail="paths is required")
    holder = auth.role or str(body.get("holder") or "human")
    if holder == "human" and body.get("holder"):
        holder = str(body["holder"])
    try:
        lease_service.reserve_paths(
            conn, tid, [str(p) for p in paths], holder=holder,
            ttl_seconds=body.get("ttl_seconds"))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"paths": lease_service.for_task(conn, tid)}


@router.delete("/tasks/{tid}/reservations")
def release_reservations(tid: str, body: dict, conn=Depends(get_db),
                         auth: Auth = Depends(get_auth)):
    paths = body.get("paths") or []
    holder = auth.role or "human"
    if holder == "human" and body.get("holder"):
        holder = str(body["holder"])
    count = lease_service.release_paths(conn, tid, [str(p) for p in paths], holder)
    return {"released": count}


@router.post("/tasks/{tid}/workflow/restart")
def restart_workflow(tid: str, body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    """Terminate the current execution and start a new one with the same definition."""
    row = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? AND status='running' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task has no running workflow")
    wf = services.workflow.get_workflow(conn, row["id"])
    definition = (wf or {}).get("definition") or {}
    if not definition:
        raise HTTPException(status_code=409, detail="workflow has no definition")
    try:
        new_wf = services.workflow.create_workflow(
            conn, tid, definition, created_by="human",
            enforce_role_coverage=False, restart_running=True,
            restart_reason=body.get("reason") or "人工调整流程强度后重新执行",
            restart_note=body.get("note"))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "kind": "workflow_restarted"})
    return new_wf


@router.post("/tasks/{tid}/workflow-weight")
def set_workflow_weight(tid: str, body: dict, conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    try:
        return weight_service.set_task_weight(
            conn, tid, body.get("weight"), body.get("reason"))
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/tasks/{tid}/workflow-weight")
def clear_workflow_weight(tid: str, conn=Depends(get_db),
                          auth: Auth = Depends(require_human)):
    try:
        return weight_service.set_task_weight(conn, tid, None)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


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


def _assert_revision_actor(conn, wf: dict, auth: Auth) -> None:
    """只有任务 planner / onboarding 助理（Cindy）可提交修订；human 可预览。"""
    if auth.role in (None, "human"):
        return
    task = services.tasks.get(conn, wf["task_id"]) or {}
    planner = str(task.get("planner") or "")
    if auth.role != planner and not services.roles.is_onboarding(conn, auth.role):
        raise HTTPException(
            status_code=403,
            detail="only the task planner or onboarding assistant can submit revisions")


@router.post("/workflows/{wid}/revisions/preview")
def preview_workflow_revision(wid: str, body: dict, conn=Depends(get_db),
                              auth: Auth = Depends(get_auth)):
    wf = _assert_workflow_task(conn, wid, auth)
    _assert_revision_actor(conn, wf, auth)
    intent = body.get("intent") if isinstance(body.get("intent"), dict) else body
    try:
        return services.workflow.preview_revision(conn, wid, intent)
    except LookupError:
        raise HTTPException(status_code=404, detail="workflow not found")


@router.post("/workflows/{wid}/revisions", status_code=201)
def create_workflow_revision(wid: str, body: dict, conn=Depends(get_db),
                             auth: Auth = Depends(get_auth)):
    wf = _assert_workflow_task(conn, wid, auth)
    _assert_revision_actor(conn, wf, auth)
    intent = body.get("intent") if isinstance(body.get("intent"), dict) else body
    rationale = str(body.get("rationale") or body.get("goal") or "").strip()
    try:
        draft = services.board.create_revision_draft(
            conn, wid, intent, rationale, auth.role or "human")
    except services.workflow.RevisionConflict as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "impact": exc.impact})
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "workflow_revision",
                            "workflow_id": wid, "draft_id": draft.get("id")})
    return draft


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
            body.get("evidence"), body.get("changed_paths"))
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
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": wf["task_id"], "kind": "step_retry", "step": sid})
    return wf
