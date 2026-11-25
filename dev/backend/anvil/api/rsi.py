"""RSI observation/retro API (v1.50 P1)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import (APIRouter, Depends, File, Form, HTTPException, UploadFile)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..deps import Auth, get_auth, get_db, require_human
from ..services import eval_suite
from ..services import proposals as proposals_service
from ..services import role_eval
from ..services import role_export
from ..services import role_space
from ..services import rsi as rsi_service
from ..services import roles as roles_service
from . import access

router = APIRouter()


class RetroIn(BaseModel):
    task_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=8000)


@router.get("/rsi/candidates")
def list_candidates(role: str | None = None, status: str | None = None,
                    conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    if access.is_agent(auth):
        role = auth.role
    return {"items": proposals_service.list_candidates(
        conn, role=role, status=status)}


@router.post("/rsi/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    try:
        version = rsi_service.promote_candidate(
            conn, candidate_id, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"candidate_id": candidate_id, "version": version}


@router.post("/rsi/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str, note: str = "", conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    try:
        candidate = proposals_service.transition_candidate(
            conn, candidate_id, "rejected", decided_by=auth.role or "human",
            decision_note=note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # ConflictError
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return candidate


class RoleRsiIn(BaseModel):
    enabled: bool


@router.put("/roles/{slug}/rsi")
def set_role_rsi(slug: str, body: RoleRsiIn, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    result = roles_service.set_rsi_enabled(conn, slug, body.enabled)
    if not result:
        raise HTTPException(status_code=404, detail="role not found")
    return result


@router.post("/roles/{slug}/versions/baseline")
def ensure_baseline(slug: str, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    try:
        return role_space.ensure_baseline(conn, slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/roles/{slug}/versions/{version}/switch")
def switch_version(slug: str, version: int, restore_charter: bool = False,
                   conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        return role_space.switch(
            conn, slug, version, restore_charter=restore_charter)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/roles/{slug}/versions/{version}/diff")
def version_diff(slug: str, version: int, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    try:
        return role_space.preview_switch(conn, slug, version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/roles/{slug}/versions/{version}/restore-config")
def restore_config(slug: str, version: int, dry_run: bool = False,
                   conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    try:
        return role_space.restore_config(conn, slug, version, dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/roles/{slug}/versions/{version}/performance")
def version_performance(slug: str, version: int, conn=Depends(get_db),
                        auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    return role_space.actual_performance(conn, slug, version)


@router.get("/rsi/roles/{slug}/observations")
def observations(slug: str, limit: int = 50, conn=Depends(get_db),
                 auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    if roles_service.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    items = rsi_service.list_observations(conn, slug, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/rsi/roles/{slug}/trigger")
def trigger(slug: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    if roles_service.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return rsi_service.due(conn, slug)


class PurgeIn(BaseModel):
    versions: list[int] = Field(default_factory=list)
    dry_run: bool = True


@router.get("/roles/{slug}/working-set")
def get_working_set(slug: str, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    if roles_service.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return role_space.working_set(conn, slug)


@router.get("/roles/{slug}/versions/summary")
def versions_summary(slug: str, conn=Depends(get_db),
                     auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    return role_space.version_summary(conn, slug)


@router.get("/roles/{slug}/versions/duplicates")
def versions_duplicates(slug: str, kind: str | None = None,
                        conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    return role_space.version_duplicates(conn, slug, kind=kind)


@router.post("/roles/{slug}/versions/dedupe-config")
def dedupe_config_versions(slug: str, dry_run: bool = True,
                           conn=Depends(get_db),
                           auth: Auth = Depends(require_human)):
    return role_space.dedupe_config_versions(conn, slug, dry_run=dry_run)


@router.post("/roles/{slug}/versions/purge")
def versions_purge(slug: str, body: PurgeIn, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    return role_space.purge_versions(
        conn, slug, body.versions, dry_run=body.dry_run)


@router.get("/roles/{slug}/versions/{version}/export")
def export_version(slug: str, version: int, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    try:
        path = role_export.export_version(conn, slug, version)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name,
                        media_type="application/zip")


@router.post("/roles/import/preview")
async def import_preview(file: UploadFile = File(...), conn=Depends(get_db),
                         auth: Auth = Depends(require_human)):
    data = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".anvil.zip", delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        return role_export.preview_import(conn, path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)


@router.post("/roles/import")
async def import_role(file: UploadFile = File(...), mode: str = Form("new_role"),
                      slug: str | None = Form(None),
                      allow_charter_change: bool = Form(False),
                      conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    data = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".anvil.zip", delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        return role_export.import_role(
            conn, path, mode=mode, slug=slug,
            allow_charter_change=allow_charter_change)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)


@router.get("/rsi/suite/status")
def suite_status(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return {"suites": eval_suite.list_suites()}


@router.post("/rsi/suite/items")
def add_suite_item(suite_id: str, body: dict,
                   conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        item_id = eval_suite.add_item(suite_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"suite_id": suite_id, "item_id": item_id}


@router.post("/rsi/suite/freeze")
def freeze_suite(suite_id: str, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    try:
        digest = eval_suite.freeze(suite_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"suite_id": suite_id, "digest": digest}


class EvalIn(BaseModel):
    suite_id: str = Field(min_length=1)
    candidate_id: str | None = None
    candidate_version: int | None = None
    baseline_version: int | None = None
    noise_floor: float = 0.10
    minimum_effect: float = 0.05
    runtime_id: str | None = None
    model_id: str | None = None


@router.post("/rsi/roles/{slug}/evaluations")
def enqueue_evaluation(slug: str, body: EvalIn, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    if roles_service.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    if bool(body.candidate_id) == bool(body.candidate_version):
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of candidate_id or candidate_version")
    baseline = body.baseline_version
    if baseline is None:
        row = conn.execute(
            "SELECT version FROM role_versions WHERE slug=? AND kind='evolution' "
            "AND status='active' ORDER BY version DESC LIMIT 1", (slug,)).fetchone()
        if row is None:
            raise HTTPException(status_code=422,
                                detail="role has no active evolution baseline")
        baseline = int(row["version"])
    try:
        if body.candidate_id:
            candidate = proposals_service.get_candidate(conn, body.candidate_id)
            if candidate is None or candidate["role_slug"] != slug:
                raise HTTPException(status_code=404, detail="candidate not found")
            version = rsi_service.candidate_version_snapshot(
                conn, body.candidate_id)
            candidate_version = int(version["version"])
            if candidate["status"] in ("draft", "queued"):
                proposals_service.transition_candidate(
                    conn, body.candidate_id, "evaluating",
                    decided_by=auth.role or "human",
                    decision_note=f"queued suite {body.suite_id}")
        else:
            candidate_version = int(body.candidate_version or 0)
            row = conn.execute(
                "SELECT 1 FROM role_versions WHERE slug=? AND version=? "
                "AND kind='evolution' AND status != 'deleted'",
                (slug, candidate_version)).fetchone()
            if row is None:
                raise HTTPException(status_code=404,
                                    detail="candidate version not found")
        result = role_eval.enqueue(
            conn, role_slug=slug, suite_id=body.suite_id,
            candidate_version=candidate_version, baseline_version=int(baseline),
            candidate_id=body.candidate_id,
            request={
                "noise_floor": body.noise_floor,
                "minimum_effect": body.minimum_effect,
                "runtime_id": body.runtime_id,
                "model_id": body.model_id,
                "baseline_working_set_hash": role_space.working_set(
                    conn, slug).get("hash"),
                "candidate_working_set_hash": role_space.working_set(
                    conn, slug).get("hash"),
            })
        return {"run_id": result["id"], "status": result["status"],
                "candidate_version": candidate_version,
                "baseline_version": int(baseline)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/rsi/roles/{slug}/evaluations")
def list_evaluations(slug: str, limit: int = 20, conn=Depends(get_db),
                     auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    if roles_service.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return {"items": role_eval.list_runs(conn, slug, limit=limit)}


@router.get("/rsi/evaluations/{run_id}")
def get_evaluation(run_id: str, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    run = role_eval.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return run


@router.post("/rsi/roles/{slug}/retro")
def retro(slug: str, body: RetroIn, conn=Depends(get_db),
          auth: Auth = Depends(get_auth)):
    access.assert_role_self(conn, auth, slug)
    if roles_service.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    task = conn.execute(
        "SELECT id FROM tasks WHERE id=?", (body.task_id,)).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if access.is_agent(auth) and slug not in rsi_service.participants(
            conn, body.task_id):
        raise HTTPException(status_code=403, detail="role is not a task participant")
    event_id = rsi_service.record_retro(conn, slug, body.task_id, body.content)
    return {"event_id": event_id, "role": slug, "task_id": body.task_id}
