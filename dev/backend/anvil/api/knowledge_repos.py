"""派生式知识仓库目录 API（2026-10-24）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import Auth, get_auth, get_db, require_human
from ..services import knowledge_card, repo_catalog

router = APIRouter()


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
