"""受控 PoC/动态验证作业 API（M2b）。

agent 可创建 safe 作业并读取自己任务范围内的作业；relaxed 进入人工审批。
审批/驳回/取消仍为 human-only。
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


def _is_human(auth: Auth) -> bool:
    return not access.is_agent(auth)


def _finding_task(conn, finding_id: str | None) -> str | None:
    if not finding_id:
        return None
    try:
        row = conn.execute(
            "SELECT task_id FROM task_findings WHERE id=?", (finding_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return str(row["task_id"]) if row else None


def _checked_job(conn, job_id: str, auth: Auth) -> dict:
    job = sandbox_service.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if access.is_agent(auth):
        if not job.get("task_id"):
            raise HTTPException(
                status_code=403, detail="agent cannot read an unbound PoC job")
        access.assert_task_read(conn, auth, str(job["task_id"]))
    return job


@router.post("/poc-jobs", status_code=201)
def create_poc_job(body: dict, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    human = _is_human(auth)
    if not human:
        task_id = body.get("task_id")
        if not task_id and body.get("finding_id"):
            task_id = _finding_task(conn, str(body["finding_id"]))
        if not task_id:
            raise HTTPException(
                status_code=403,
                detail="agent PoC jobs must be bound to a task or finding")
        access.assert_task_read(conn, auth, str(task_id))
    try:
        return sandbox_service.create_job(
            conn, spec_raw=body, actor_role=auth.role, actor_is_human=human)
    except JobSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SandboxUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "strict_sandbox_unavailable",
                    "message": str(exc),
                    "hint": "安装 Docker/Podman 并启动后重试；Windows 严格档在 M3 接入 WSL2。"},
        ) from exc
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/poc-jobs")
def list_poc_jobs(task_id: str | None = None, finding_id: str | None = None,
                  status: str | None = None, limit: int = 100,
                  conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    if access.is_agent(auth):
        if auth.task_id:
            task_id = auth.task_id
        elif task_id:
            access.assert_task_read(conn, auth, task_id)
        elif finding_id:
            bound = _finding_task(conn, finding_id)
            if not bound:
                raise HTTPException(status_code=403, detail="finding not found")
            access.assert_task_read(conn, auth, bound)
            task_id = bound
        else:
            raise HTTPException(
                status_code=403, detail="agent must pass task_id or finding_id")
    return sandbox_service.list_jobs(
        conn, task_id=task_id, finding_id=finding_id,
        status=status, limit=limit)


@router.get("/poc-jobs/{job_id}")
def get_poc_job(job_id: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    job = _checked_job(conn, job_id, auth)
    return sandbox_service.job_view(conn, job_id) or job


@router.get("/poc-jobs/{job_id}/log")
def get_poc_job_log(job_id: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _checked_job(conn, job_id, auth)
    try:
        return sandbox_service.job_log(conn, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/poc-jobs/{job_id}/evidence")
def get_poc_job_evidence(job_id: str, conn=Depends(get_db),
                         auth: Auth = Depends(get_auth)):
    job = _checked_job(conn, job_id, auth)
    return job.get("evidence") or {}


@router.get("/poc-jobs/{job_id}/artifacts")
def list_poc_job_artifacts(job_id: str, conn=Depends(get_db),
                           auth: Auth = Depends(get_auth)):
    _checked_job(conn, job_id, auth)
    return sandbox_service.job_artifacts(conn, job_id)


@router.get("/poc-jobs/{job_id}/artifacts/{rel_path:path}")
def download_poc_job_artifact(job_id: str, rel_path: str,
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


@router.post("/poc-jobs/{job_id}/cancel")
def cancel_poc_job(job_id: str, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    try:
        return sandbox_service.cancel_job(conn, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/poc-jobs/{job_id}/approve")
def approve_poc_job(job_id: str, body: dict | None = None,
                    conn=Depends(get_db), auth: Auth = Depends(require_human)):
    note = (body or {}).get("note")
    try:
        return sandbox_service.approve_job(
            conn, job_id, actor_role=auth.role or "human", note=note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/poc-jobs/{job_id}/reject")
def reject_poc_job(job_id: str, body: dict | None = None,
                   conn=Depends(get_db), auth: Auth = Depends(require_human)):
    note = (body or {}).get("note")
    try:
        return sandbox_service.reject_job(
            conn, job_id, actor_role=auth.role or "human", note=note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
