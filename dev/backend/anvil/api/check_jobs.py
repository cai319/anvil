"""引擎执行的验收检查作业 API（v1.62 #2）。

检查作业绑定任务工作区、不要求 finding；与 PoC 作业区分 kind，但复用同一套
执行与取证管线。agent 只能创建/读取任务范围内的检查；审批/驳回/取消 human-only。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..deps import Auth, get_auth, get_db, require_human
from ..sandbox.errors import QuotaExceeded, SandboxUnavailable
from ..sandbox.jobs import JobSpecError
from ..services import sandbox as sandbox_service
from . import access

router = APIRouter()

KIND = "check"


def _is_human(auth: Auth) -> bool:
    return not access.is_agent(auth)


def _checked_job(conn, job_id: str, auth: Auth) -> dict:
    job = sandbox_service.get_job(conn, job_id)
    if job is None or job.get("kind") != KIND:
        raise HTTPException(status_code=404, detail="check job not found")
    if access.is_agent(auth):
        if not job.get("task_id"):
            raise HTTPException(
                status_code=403, detail="agent cannot read an unbound check job")
        access.assert_task_read(conn, auth, str(job["task_id"]))
    return job


@router.post("/check-jobs", status_code=201)
def create_check_job(body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(get_auth)):
    human = _is_human(auth)
    if not human:
        task_id = body.get("task_id")
        if not task_id:
            raise HTTPException(
                status_code=403,
                detail="agent check jobs must be bound to a task workspace")
        access.assert_task_read(conn, auth, str(task_id))
    try:
        return sandbox_service.create_job(
            conn, spec_raw=body, actor_role=auth.role, actor_is_human=human,
            kind=KIND)
    except JobSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SandboxUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/check-jobs")
def list_check_jobs(task_id: str | None = None, status: str | None = None,
                    limit: int = 100, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    if access.is_agent(auth):
        if auth.task_id:
            task_id = auth.task_id
        elif task_id:
            access.assert_task_read(conn, auth, task_id)
        else:
            raise HTTPException(
                status_code=403, detail="agent must pass task_id")
    return sandbox_service.list_jobs(
        conn, task_id=task_id, status=status, kind=KIND, limit=limit)


@router.get("/check-jobs/{job_id}")
def get_check_job(job_id: str, conn=Depends(get_db),
                  auth: Auth = Depends(get_auth)):
    job = _checked_job(conn, job_id, auth)
    return sandbox_service.job_view(conn, job_id) or job


@router.get("/check-jobs/{job_id}/log")
def get_check_job_log(job_id: str, conn=Depends(get_db),
                      auth: Auth = Depends(get_auth)):
    _checked_job(conn, job_id, auth)
    try:
        return sandbox_service.job_log(conn, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/check-jobs/{job_id}/evidence")
def get_check_job_evidence(job_id: str, conn=Depends(get_db),
                           auth: Auth = Depends(get_auth)):
    job = _checked_job(conn, job_id, auth)
    return job.get("evidence") or {}


@router.get("/check-jobs/{job_id}/result")
def get_check_job_result(job_id: str, conn=Depends(get_db),
                         auth: Auth = Depends(get_auth)):
    job = _checked_job(conn, job_id, auth)
    return job.get("check_result") or {}


@router.get("/check-jobs/{job_id}/artifacts")
def list_check_job_artifacts(job_id: str, conn=Depends(get_db),
                             auth: Auth = Depends(get_auth)):
    _checked_job(conn, job_id, auth)
    return sandbox_service.job_artifacts(conn, job_id)


@router.get("/check-jobs/{job_id}/artifacts/{rel_path:path}")
def download_check_job_artifact(job_id: str, rel_path: str,
                                conn=Depends(get_db),
                                auth: Auth = Depends(get_auth)):
    _checked_job(conn, job_id, auth)
    try:
        path = sandbox_service.artifact_file(conn, job_id, rel_path)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path, media_type="application/octet-stream",
        filename=Path(rel_path).name)


@router.post("/check-jobs/{job_id}/cancel")
def cancel_check_job(job_id: str, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    try:
        return sandbox_service.cancel_job(conn, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/check-jobs/{job_id}/approve")
def approve_check_job(job_id: str, body: dict | None = None,
                      conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    note = (body or {}).get("note")
    try:
        return sandbox_service.approve_job(
            conn, job_id, actor_role=auth.role or "human", note=note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/check-jobs/{job_id}/reject")
def reject_check_job(job_id: str, body: dict | None = None,
                     conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    note = (body or {}).get("note")
    try:
        return sandbox_service.reject_job(
            conn, job_id, actor_role=auth.role or "human", note=note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
