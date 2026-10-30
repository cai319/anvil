"""问题板（Issue Board）API：reviewer 写入、human 逐条确认、任务闸门。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import (
    FindingCompleteIn, FindingDecisionIn, FindingIn, FindingPatch, ReviewersIn,
)
from ..services import findings as findings_service
from ..services.tasks import ConflictError
from . import access

router = APIRouter()


def _conflict(exc: Exception, *, not_found: bool = False) -> HTTPException:
    if isinstance(exc, findings_service.FindingValidationError):
        return HTTPException(status_code=422, detail={
            "missing_fields": exc.missing_fields,
            "errors": exc.errors,
            "message": str(exc),
        })
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        if not_found and "not enabled" in str(exc):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _reviewer(conn, auth: Auth, tid: str) -> None:
    if not findings_service.is_reviewer(conn, tid, auth.role):
        raise HTTPException(status_code=403, detail="only task reviewers can write findings")


def _can_manage_reviewers(conn, auth: Auth, tid: str, task: dict) -> None:
    if auth.role in (None, "human"):
        return
    if services.roles.is_onboarding(conn, auth.role):
        return
    if task.get("planner") == auth.role:
        return
    raise HTTPException(
        status_code=403, detail="only the planner, onboarding assistant or human can set reviewers")


def _wake_finding_author(request: Request, conn, tid: str, author: str,
                         message: str) -> None:
    """唤醒 finding 作者；作者已被移出 reviewer 时回退到当前全部 reviewer。"""
    roles = [author] if findings_service.is_reviewer(conn, tid, author) else []
    if not roles:
        roles = findings_service.reviewer_roles(conn, tid)
    for role in roles:
        _wake_finding_reviewer(request, conn, tid, role, message)


def _wake_finding_reviewer(request: Request, conn, tid: str, role: str,
                           message: str) -> None:
    """唤醒 reviewer 处理问题板。专用 [anvil:findings] run，不落 REPLY_PROTOCOL。"""
    if not role:
        return
    orch = getattr(request.app.state, "orchestrator", None)
    active = services.runs.find_active_run(conn, tid, role)
    if active:
        if orch:
            try:
                if active["status"] == "reviewing":
                    orch.resume_review(request.app.state.db_path, active["id"], False)
                else:
                    orch.pump(request.app.state.db_path, active["id"], "resume", {})
            except Exception:  # noqa: BLE001
                pass
        return
    run = services.runs.create_run(
        conn, role, tid,
        prompt=("[anvil:findings] 问题板需要复核/更新。\n"
                f"{message}\n\n"
                "请读取现有 findings，更新或新增条目，完成后执行 "
                "`anvil finding complete --task $ANVIL_TASK_ID --summary \"...\"`。"))
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass


@router.get("/tasks/{tid}/findings/schema")
def finding_schema(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    return findings_service.schema()


@router.get("/tasks/{tid}/findings/summary")
def finding_summary(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    try:
        return findings_service.board_summary(conn, tid)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.post("/tasks/{tid}/findings/complete")
def complete_finding_board(tid: str, body: FindingCompleteIn,
                           conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    _reviewer(conn, auth, tid)
    try:
        return findings_service.complete_board(conn, tid, auth.role or "", body.summary)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.post("/tasks/{tid}/findings/reopen")
def reopen_finding_board(tid: str, body: FindingDecisionIn, request: Request,
                         conn=Depends(get_db), auth: Auth = Depends(require_human)):
    access.assert_task_read(conn, auth, tid)
    try:
        board = findings_service.reopen_board(conn, tid, auth.role or "human", body.note)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)
    for role in findings_service.reviewer_roles(conn, tid):
        _wake_finding_reviewer(request, conn, tid, role,
                               (body.note or "").strip() or "问题板已退回，请复核 findings。")
    return board


@router.get("/tasks/{tid}/findings")
def list_findings(tid: str, status: str | None = None, severity: str | None = None,
                  risk_type: str | None = None, q: str | None = None,
                  limit: int = 500, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    try:
        items = findings_service.list_findings(
            conn, tid, status=status, severity=severity,
            risk_type=risk_type, q=q, limit=limit)
        summary = findings_service.board_summary(conn, tid)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)
    return {**summary, "items": items}


@router.post("/tasks/{tid}/findings", status_code=201)
def create_finding(tid: str, body: FindingIn, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    _reviewer(conn, auth, tid)
    try:
        return findings_service.create_finding(
            conn, tid, auth.role or "", body.model_dump(),
            workflow_id=body.workflow_id, step_id=body.step_id, run_id=body.run_id)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.get("/tasks/{tid}/findings/{fid}")
def get_finding(tid: str, fid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    finding = findings_service.get_finding(conn, fid, tid)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return finding


@router.patch("/tasks/{tid}/findings/{fid}")
def update_finding(tid: str, fid: str, body: FindingPatch, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    _reviewer(conn, auth, tid)
    try:
        return findings_service.update_finding(
            conn, tid, fid, auth.role or "", body.model_dump(exclude_unset=True))
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.post("/tasks/{tid}/findings/{fid}/submit")
def submit_finding(tid: str, fid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    _reviewer(conn, auth, tid)
    try:
        return findings_service.submit_finding(conn, tid, fid, auth.role or "")
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.post("/tasks/{tid}/findings/{fid}/confirm")
def confirm_finding(tid: str, fid: str, body: FindingDecisionIn,
                    conn=Depends(get_db), auth: Auth = Depends(require_human)):
    access.assert_task_read(conn, auth, tid)
    try:
        return findings_service.confirm_finding(conn, tid, fid, auth.role or "human", body.note)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.post("/tasks/{tid}/findings/{fid}/false-positive")
def false_positive_finding(tid: str, fid: str, body: FindingDecisionIn,
                           conn=Depends(get_db), auth: Auth = Depends(require_human)):
    access.assert_task_read(conn, auth, tid)
    try:
        return findings_service.mark_false_positive(
            conn, tid, fid, auth.role or "human", body.note)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.post("/tasks/{tid}/findings/{fid}/request-changes")
def request_finding_changes(tid: str, fid: str, body: FindingDecisionIn, request: Request,
                            conn=Depends(get_db), auth: Auth = Depends(require_human)):
    access.assert_task_read(conn, auth, tid)
    try:
        finding = findings_service.request_changes(
            conn, tid, fid, auth.role or "human", body.note)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)
    _wake_finding_author(request, conn, tid, finding["author_role"],
                         (body.note or "").strip() or "请重新评估该 finding。")
    return finding


@router.post("/tasks/{tid}/findings/{fid}/reopen")
def reopen_finding(tid: str, fid: str, body: FindingDecisionIn, request: Request,
                   conn=Depends(get_db), auth: Auth = Depends(require_human)):
    access.assert_task_read(conn, auth, tid)
    try:
        finding = findings_service.request_changes(
            conn, tid, fid, auth.role or "human", body.note)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)
    _wake_finding_author(request, conn, tid, finding["author_role"],
                         (body.note or "").strip() or "请重新评估该 finding。")
    return finding


@router.delete("/tasks/{tid}/findings/{fid}", status_code=204)
def delete_finding(tid: str, fid: str, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    finding = findings_service.get_finding(conn, fid, tid)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    if auth.role not in (None, "human"):
        _reviewer(conn, auth, tid)
        if finding["status"] not in ("draft", "changes_requested"):
            raise HTTPException(
                status_code=409, detail="only draft/changes_requested findings can be deleted")
    findings_service.delete_finding(conn, tid, fid, auth.role or "human")
    return None


@router.post("/tasks/{tid}/reviewers")
def set_reviewers(tid: str, body: ReviewersIn, conn=Depends(get_db),
                  auth: Auth = Depends(get_auth)):
    access.assert_task_read(conn, auth, tid)
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _can_manage_reviewers(conn, auth, tid, task)
    source = "human" if auth.role in (None, "human") else "cindy"
    try:
        return findings_service.add_reviewers(
            conn, tid, body.roles, by=auth.role or "human", source=source)
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)


@router.delete("/tasks/{tid}/reviewers/{role}", status_code=200)
def remove_reviewer(tid: str, role: str, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    access.assert_task_read(conn, auth, tid)
    try:
        return findings_service.remove_reviewer(
            conn, tid, role, by=auth.role or "human")
    except (LookupError, ConflictError) as exc:
        raise _conflict(exc, not_found=True)
