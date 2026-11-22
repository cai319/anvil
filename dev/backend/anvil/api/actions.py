"""Action Cards 端点：prepare（Agent） / approve|reject（自然人）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..services.tasks import ConflictError
from . import access

router = APIRouter()


@router.post("/action-cards", status_code=201)
def create_action(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    if not auth.role or auth.role == "human":
        raise HTTPException(status_code=400, detail="agent session required")
    task_id = body.get("task_id")
    if task_id:
        if auth.task_id and task_id != auth.task_id:
            raise HTTPException(status_code=403, detail="session is bound to another task")
        access.assert_task_read(conn, auth, str(task_id))
    try:
        return services.actions.create(
            conn, auth.role, str(body.get("kind") or ""), task_id,
            body.get("payload") or {}, body.get("reason"))
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/action-cards")
def list_actions(status: str | None = None, conn=Depends(get_db),
                 auth: Auth = Depends(get_auth)):
    cards = services.actions.list_cards(conn, status)
    if access.is_agent(auth):
        cards = [card for card in cards if card.get("role_slug") == auth.role]
    return cards


@router.post("/action-cards/{cid}/approve")
def approve_action(cid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        return services.actions.decide(conn, cid, True)
    except LookupError:
        raise HTTPException(status_code=404, detail="action not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/action-cards/{cid}/reject")
def reject_action(cid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        return services.actions.decide(conn, cid, False)
    except LookupError:
        raise HTTPException(status_code=404, detail="action not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
