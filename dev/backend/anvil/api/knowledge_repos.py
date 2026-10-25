"""派生式知识仓库目录 API（2026-10-24）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import Auth, get_auth, get_db, require_human
from ..services import knowledge_card, knowledge_stats, repo_catalog, task_knowledge, workspace_knowledge

router = APIRouter()

_KNOWLEDGE_STATUSES = {"draft", "proposed", "approved", "rejected", "deprecated", "expired"}
_TASK_KNOWLEDGE_STATUSES = {"active", "archived"}


@router.get("/cards")
def list_knowledge_cards(
    status: str | None = None, kind: str | None = None,
    workspace_id: str | None = None, q: str | None = None,
    limit: int = 200, offset: int = 0,
    conn=Depends(get_db), auth: Auth = Depends(require_human),
):
    if status and status != "all" and status not in _KNOWLEDGE_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")
    if kind and kind not in knowledge_card.KINDS:
        raise HTTPException(status_code=422, detail=f"invalid kind: {kind}")
    return workspace_knowledge.list_all(
        conn, status=status, kind=kind, workspace_id=workspace_id,
        q=q, limit=limit, offset=offset,
    )


@router.get("/stats")
def knowledge_library_stats(
    conn=Depends(get_db), auth: Auth = Depends(require_human),
):
    return knowledge_stats.library_stats(conn)


@router.get("/task-cards")
def list_task_knowledge_cards(
    status: str | None = None, workspace_id: str | None = None,
    q: str | None = None, limit: int = 200, offset: int = 0,
    conn=Depends(get_db), auth: Auth = Depends(require_human),
):
    if status and status != "all" and status not in _TASK_KNOWLEDGE_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")
    return task_knowledge.list_all(
        conn, status=status, workspace_id=workspace_id,
        q=q, limit=limit, offset=offset,
    )


@router.get("/card/template")
def card_template(kind: str | None = None, auth: Auth = Depends(get_auth)):
    return knowledge_card.template(kind)


@router.post("/card/validate")
def card_validate(body: dict, auth: Auth = Depends(get_auth)):
    return knowledge_card.validate_card(
        str(body.get("kind") or ""),
        body.get("fields") or {},
        for_approval=bool(body.get("for_approval")),
    )


class SuggestIn(BaseModel):
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    limit: int = 5


@router.get("/repos/stats")
def repo_stats(conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return repo_catalog.binding_stats(conn)


@router.post("/repos/refresh")
def repo_refresh(auth: Auth = Depends(require_human)):
    repo_catalog.invalidate_catalog()
    return {"ok": True}


@router.post("/repos/suggest")
def repo_suggest(body: SuggestIn, conn=Depends(get_db),
                 auth: Auth = Depends(get_auth)):
    return repo_catalog.suggest(
        conn,
        title=body.title,
        description=body.description,
        tags=body.tags,
        exec_repo=body.repo,
        workspace_id=body.workspace_id,
        exclude_task_id=body.task_id,
        limit=body.limit,
    )


@router.get("/repos")
def repo_list(conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return repo_catalog.build_catalog(conn)


@router.get("/repos/{repo:path}")
def repo_profile(repo: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    profile = repo_catalog.get_profile(conn, repo)
    if profile is None:
        raise HTTPException(status_code=404, detail="repo not found")
    return profile
