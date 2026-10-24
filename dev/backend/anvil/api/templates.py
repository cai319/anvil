"""Workflow template studio endpoints."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..util import ConflictError
from ..services import templates as template_service

router = APIRouter()


def _audit(conn: sqlite3.Connection, request: Request, action: str, payload: dict) -> None:
    from ..util import jdumps, now_iso
    try:
        conn.execute(
            "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            ("human", None, action, jdumps({
                **payload,
                "client": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            }), now_iso()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


@router.get("/workflow-templates")
def list_templates(q: str | None = None, status: str | None = None,
                   conn=Depends(get_db)):
    return template_service.list_templates(conn, q=q, status=status)


@router.get("/workflow-templates/import-sources")
def import_sources(conn=Depends(get_db)):
    workflows = [
        dict(r) for r in conn.execute(
            "SELECT w.id, w.task_id, w.status, t.title AS task_title "
            "FROM workflows w JOIN tasks t ON t.id=w.task_id "
            "ORDER BY w.created_at DESC LIMIT 100")
    ]
    drafts = [
        dict(r) for r in conn.execute(
            "SELECT d.id, d.source_task, d.goal, d.created_at, t.title AS task_title "
            "FROM board_drafts d LEFT JOIN tasks t ON t.id=d.source_task "
            "WHERE COALESCE(d.kind,'tasks')='workflow' AND d.status='pending' "
            "ORDER BY d.created_at DESC LIMIT 100")
    ]
    return {"workflows": workflows, "drafts": drafts,
            "sops": services.board.list_templates()}


@router.post("/workflow-templates/import", status_code=201)
def import_template(body: dict, request: Request, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    source_kind = str(body.get("source_kind") or "")
    source_id = str(body.get("source_id") or "")
    name = body.get("name")
    try:
        if source_kind == "workflow":
            result = template_service.import_workflow(conn, source_id, name=name)
        elif source_kind == "draft":
            result = template_service.import_draft(conn, source_id, name=name)
        else:
            raise HTTPException(status_code=422, detail="unsupported source_kind")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "template.import", {
        "source_kind": source_kind, "source_id": source_id,
        "template_id": result.get("id"),
    })
    return result


@router.post("/workflow-templates", status_code=201)
def create_template(body: dict, request: Request, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    try:
        result = template_service.create_template(
            conn, name=str(body.get("name") or ""),
            description=str(body.get("description") or ""),
            definition=body.get("definition"),
            layout=body.get("layout") or {},
            tags=body.get("tags") or [],
            use_mode=str(body.get("use_mode") or "both"),
            created_by="human",
            source_kind="manual",
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "template.create", {"template_id": result.get("id")})
    return result


@router.get("/workflow-templates/{tid}")
def get_template(tid: str, conn=Depends(get_db)):
    result = template_service.get_template(conn, tid)
    if result is None:
        raise HTTPException(status_code=404, detail="template not found")
    return result


@router.put("/workflow-templates/{tid}/draft")
def save_draft(tid: str, body: dict, request: Request, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    try:
        result = template_service.save_draft(
            conn, tid,
            definition=body.get("definition") or {},
            layout=body.get("layout") or {},
            name=body.get("name"),
            description=body.get("description"),
            tags=body.get("tags"),
            use_mode=body.get("use_mode"),
            base_revision=body.get("base_revision"),
            created_by="human",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "template.save_draft", {
        "template_id": tid,
        "revision": (result.get("draft") or {}).get("revision"),
    })
    return result


@router.post("/workflow-templates/{tid}/validate")
def validate_template(tid: str, body: dict, conn=Depends(get_db)):
    if body.get("definition"):
        return {"issues": template_service.validate_template(
            conn, body["definition"], body.get("layout") or {})}
    template = template_service.get_template(conn, tid)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    draft = template.get("draft") or template.get("published")
    if not draft:
        raise HTTPException(status_code=409, detail="template has no definition")
    return {"issues": template_service.validate_template(
        conn, draft["definition"], draft.get("layout") or {})}


@router.post("/workflow-templates/{tid}/publish")
def publish(tid: str, body: dict, request: Request, conn=Depends(get_db),
            auth: Auth = Depends(require_human)):
    try:
        result = template_service.publish(
            conn, tid, change_note=str(body.get("change_note") or ""), created_by="human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "template.publish", {
        "template_id": tid, "version": result.get("published_version"),
    })
    return result


@router.post("/workflow-templates/{tid}/archive")
def archive(tid: str, request: Request, conn=Depends(get_db),
            auth: Auth = Depends(require_human)):
    try:
        result = template_service.archive_template(conn, tid)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _audit(conn, request, "template.archive", {"template_id": tid})
    return result


@router.post("/workflow-templates/{tid}/duplicate", status_code=201)
def duplicate(tid: str, body: dict, request: Request, conn=Depends(get_db),
              auth: Auth = Depends(require_human)):
    try:
        result = template_service.duplicate_template(
            conn, tid, name=body.get("name"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "template.duplicate", {
        "template_id": tid, "new_id": result.get("id"),
    })
    return result


@router.get("/workflow-templates/{tid}/versions")
def versions(tid: str, conn=Depends(get_db)):
    template = template_service.get_template(conn, tid)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return template.get("versions") or []


@router.get("/workflow-templates/{tid}/versions/{version}")
def get_version(tid: str, version: int, conn=Depends(get_db)):
    result = template_service.get_version(conn, tid, version)
    if result is None:
        raise HTTPException(status_code=404, detail="template version not found")
    return result


@router.post("/workflow-templates/{tid}/rollback")
def rollback(tid: str, body: dict, request: Request, conn=Depends(get_db),
             auth: Auth = Depends(require_human)):
    try:
        result = template_service.rollback_template(
            conn, tid, int(body.get("version") or 0), created_by="human")
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "template.rollback", {
        "template_id": tid, "from_version": body.get("version"),
    })
    return result


@router.post("/workflow-templates/{tid}/preview-apply")
def preview_apply(tid: str, body: dict, conn=Depends(get_db)):
    try:
        return template_service.preview_apply(
            conn, str(body.get("task_id") or ""), tid,
            body.get("version"), body.get("role_mapping") or {})
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/tasks/{task_id}/workflow/from-template")
def apply_template(task_id: str, body: dict, request: Request,
                   conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        result = template_service.apply_to_task(
            conn, task_id, str(body.get("template_id") or ""),
            body.get("version"), body.get("role_mapping") or {},
            restart_running=bool(body.get("restart_running")),
            restart_reason=body.get("restart_reason"),
            restart_note=body.get("restart_note"),
            reject_pending_drafts=bool(body.get("reject_pending_drafts")),
            acknowledge_uncovered=bool(body.get("acknowledge_uncovered")),
            mode=str(body.get("mode") or "direct"),
            actor="human",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "workflow.apply_template", {
        "task_id": task_id, "template_id": body.get("template_id"),
        "version": body.get("version"), "role_mapping": body.get("role_mapping"),
        "restart_running": bool(body.get("restart_running")),
        "restart_reason": body.get("restart_reason"),
    })
    return result


@router.post("/tasks/{task_id}/plan-seed")
def set_plan_seed(task_id: str, body: dict, request: Request,
                  conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        result = template_service.set_plan_seed(
            conn, task_id, str(body.get("template_id") or ""),
            body.get("version"), created_by="human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(conn, request, "task.set_plan_seed", {"task_id": task_id, **result})
    return result


@router.delete("/tasks/{task_id}/plan-seed", status_code=204)
def clear_plan_seed(task_id: str, request: Request, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    try:
        template_service.clear_plan_seed(conn, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _audit(conn, request, "task.clear_plan_seed", {"task_id": task_id})
    return None
