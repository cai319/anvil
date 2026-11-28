"""受控 PoC/动态验证作业 API（M2a：人类触发）。

agent 创建 safe 作业与审批流在 M2b 开放；本版本所有端点均要求 human。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..deps import Auth, get_db, require_human
from ..sandbox.errors import SandboxUnavailable
from ..sandbox.jobs import JobSpecError
from ..services import sandbox as sandbox_service

router = APIRouter()


@router.post("/poc-jobs", status_code=201)
def create_poc_job(body: dict, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    try:
        return sandbox_service.create_job(
            conn, spec_raw=body, actor_role=auth.role, actor_is_human=True)
    except JobSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SandboxUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "strict_sandbox_unavailable",
                    "message": str(exc),
                    "hint": "安装 Docker/Podman 并启动后重试；Windows 严格档在 M3 接入 WSL2。"},
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/poc-jobs")
def list_poc_jobs(task_id: str | None = None, finding_id: str | None = None,
                  status: str | None = None, limit: int = 100,
                  conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return sandbox_service.list_jobs(
        conn, task_id=task_id, finding_id=finding_id,
        status=status, limit=limit)


@router.get("/poc-jobs/{job_id}")
def get_poc_job(job_id: str, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    job = sandbox_service.job_view(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/poc-jobs/{job_id}/log")
def get_poc_job_log(job_id: str, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    try:
        return sandbox_service.job_log(conn, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/poc-jobs/{job_id}/evidence")
def get_poc_job_evidence(job_id: str, conn=Depends(get_db),
                         auth: Auth = Depends(require_human)):
    job = sandbox_service.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.get("evidence") or {}


@router.get("/poc-jobs/{job_id}/artifacts")
def list_poc_job_artifacts(job_id: str, conn=Depends(get_db),
                           auth: Auth = Depends(require_human)):
    return sandbox_service.job_artifacts(conn, job_id)


@router.get("/poc-jobs/{job_id}/artifacts/{rel_path:path}")
def download_poc_job_artifact(job_id: str, rel_path: str,
                              conn=Depends(get_db),
                              auth: Auth = Depends(require_human)):
    try:
        path = sandbox_service.artifact_file(conn, job_id, rel_path)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path, media_type="application/octet-stream",
        filename=Path(rel_path).name)


@router.post("/poc-jobs/{job_id}/cancel")
def cancel_poc_job(job_id: str, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    try:
        return sandbox_service.cancel_job(conn, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
