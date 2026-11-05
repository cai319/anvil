"""派生式知识仓库目录 API（2026-10-24）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import Auth, get_auth, get_db, require_human
from ..services import (knowledge_card, knowledge_clean, knowledge_quality,
                        knowledge_repos, knowledge_stats, repo_catalog,
                        repo_knowledge, task_knowledge, utility,
                        workspace_knowledge)
from ..util import ConflictError

router = APIRouter()

_KNOWLEDGE_STATUSES = {"draft", "proposed", "approved", "rejected", "deprecated", "expired"}
_TASK_KNOWLEDGE_STATUSES = {"active", "proposed", "archived"}


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


@router.get("/quality/summary")
def knowledge_quality_summary(repo: str | None = None, conn=Depends(get_db),
                              auth: Auth = Depends(get_auth)):
    return knowledge_quality.summary(conn, repo)


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
def card_validate(body: dict, conn=Depends(get_db),
                  auth: Auth = Depends(get_auth)):
    kind = str(body.get("kind") or "")
    fields = body.get("fields") or {}
    title = str(body.get("title") or "")
    tldr = str(body.get("tldr") or "")
    body_md = str(body.get("body_md") or "")
    if not body_md:
        body_md = knowledge_card.render_markdown(title, tldr, kind, fields)
    check = knowledge_card.validate_card(
        kind, fields, for_approval=bool(body.get("for_approval")))
    quality = knowledge_quality.evaluate_row({
        "kind": kind, "title": title, "tldr": tldr, "body_md": body_md,
        "fields": fields, "authority": body.get("authority") or "reference",
        "review_at": body.get("review_at"), "expires_at": body.get("expires_at"),
        "status": "proposed",
    }, "repo_knowledge")
    return {**check, "quality": quality}


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


@router.get("/topic-repos")
def topic_repo_list(status: str = "active", conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    """主题知识库注册表 + 卡片/待审/绑定任务计数（管理页）。"""
    return {"repos": knowledge_repos.list_with_stats(conn, status=status)}


@router.get("/repo-cards")
def repo_card_queue(status: str | None = "proposed", q: str | None = None,
                    limit: int = 200, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    """跨主题知识库的卡片队列（默认待审批）。"""
    if status and status != "all" and status not in _KNOWLEDGE_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")
    return {"cards": repo_knowledge.list_cards(
        conn, status=None if status == "all" else status, q=q, limit=limit)}


@router.post("/repos", status_code=201)
def repo_create(body: dict, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    """人工创建/登记一个主题知识库。"""
    name = str(body.get("repo") or body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="repo name is required")
    try:
        return knowledge_repos.ensure(
            conn, name, description=str(body.get("description") or ""),
            created_by="human")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/repos/{repo:path}/rename")
def repo_rename(repo: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    try:
        return knowledge_repos.rename(
            conn, repo, str(body.get("new_name") or ""), actor="human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="repo not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/repos/{repo:path}/merge")
def repo_merge(repo: str, body: dict, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    try:
        return knowledge_repos.merge(
            conn, repo, str(body.get("target") or ""), actor="human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="repo not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/repos/{repo:path}/lock")
def repo_lock(repo: str, body: dict, conn=Depends(get_db),
              auth: Auth = Depends(require_human)):
    try:
        return knowledge_repos.lock(conn, repo, bool(body.get("locked", True)))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="repo not found") from exc


@router.get("/repos/{repo:path}/cards")
def repo_cards(repo: str, status: str | None = None, q: str | None = None,
               limit: int = 200, conn=Depends(get_db),
               auth: Auth = Depends(get_auth)):
    return repo_knowledge.list_cards(
        conn, repo=repo, status=status, q=q, limit=limit)


@router.post("/repos/{repo:path}/cards", status_code=201)
def repo_card_create(repo: str, body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(get_auth)):
    """Agent/human propose 一张主题知识卡；默认 proposed，人工 approve 后生效。"""
    is_agent = bool(auth.role and auth.role not in (None, "human")
                    and not auth.test_bypass)
    status = "proposed" if is_agent else str(body.get("status") or "proposed")
    try:
        return repo_knowledge.create(
            conn, repo,
            kind=str(body.get("kind") or "insight"),
            title=str(body.get("title") or ""),
            tldr=str(body.get("tldr") or ""),
            body_md=str(body.get("body_md") or body.get("body") or ""),
            fields=body.get("fields") or {},
            tags=body.get("tags") or [],
            authority=str(body.get("authority") or "reference"),
            status=status if status in ("proposed", "draft") else "proposed",
            source_task_id=body.get("source_task_id") or auth.task_id,
            source_step_id=body.get("source_step_id"),
            source_role=body.get("source_role") or auth.role,
            source_event=body.get("source_event"),
            confidence=float(body.get("confidence") or 0.5),
            created_by=auth.role or "human",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/repo-cards/{card_id}/approve")
def repo_card_approve(card_id: str, body: dict | None = None,
                      conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    try:
        return repo_knowledge.approve(
            conn, card_id, actor=auth.role or "human",
            expected_version=(body or {}).get("expected_version"))
    except knowledge_quality.QualityGateError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "quality": exc.quality,
        }) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/repo-cards/{card_id}")
def repo_card_update(card_id: str, body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    """人工补全/编辑 proposed 主题卡；body 由 fields 重新渲染。"""
    try:
        return repo_knowledge.update(
            conn, card_id,
            title=body.get("title"), tldr=body.get("tldr"),
            fields=body.get("fields"), tags=body.get("tags"),
            authority=body.get("authority"),
            review_at=body.get("review_at"),
            expires_at=body.get("expires_at"),
            expected_version=body.get("expected_version"),
            actor=auth.role or "human")
    except knowledge_quality.QualityGateError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "quality": exc.quality,
        }) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/repo-cards/{card_id}/reject")
def repo_card_reject(card_id: str, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    try:
        return repo_knowledge.reject(conn, card_id, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc


@router.post("/repo-cards/{card_id}/feedback")
def repo_card_feedback(card_id: str, body: dict, conn=Depends(get_db),
                       auth: Auth = Depends(get_auth)):
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="card not found")
    try:
        return knowledge_quality.add_feedback(
            conn, "repo_knowledge", card_id,
            str(body.get("signal") or ""),
            task_id=body.get("task_id") or auth.task_id,
            run_id=body.get("run_id") or auth.run_id,
            role_slug=body.get("role_slug") or auth.role,
            note=str(body.get("note") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/review-candidates")
def knowledge_review_candidates(repo: str | None = None, limit: int = 50,
                                conn=Depends(get_db),
                                auth: Auth = Depends(require_human)):
    return {"cards": knowledge_quality.review_candidates(
        conn, repo=repo, limit=limit)}


@router.post("/repo-cards/{card_id}/review")
def repo_card_review(card_id: str, body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    try:
        return knowledge_quality.review_action(
            conn, card_id, str(body.get("action") or ""),
            actor=auth.role or "human", days=body.get("days"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/repo-cards/{card_id}/clean")
def repo_card_clean(card_id: str, body: dict | None = None,
                    conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    """人工按需 LLM 清洗：默认 dry-run；显式传 cleaned+false 才写回 proposed。"""
    body = body or {}
    try:
        if body.get("dry_run", True):
            return knowledge_clean.build_preview(
                conn, card_id, runtime_id=body.get("runtime_id"))
        cleaned = body.get("cleaned")
        if not isinstance(cleaned, dict):
            raise HTTPException(status_code=422, detail="cleaned payload is required")
        return knowledge_clean.apply_preview(
            conn, card_id, cleaned, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except knowledge_clean.CleaningError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except utility.UtilityError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/repo-cards/{card_id}/to-channel")
def repo_card_to_channel(card_id: str, body: dict, conn=Depends(get_db),
                         auth: Auth = Depends(require_human)):
    from ..services import harvest
    workspace_id = str(body.get("workspace_id") or "")
    if not workspace_id:
        raise HTTPException(status_code=422, detail="workspace_id is required")
    try:
        return harvest.repo_card_to_channel(
            conn, card_id, workspace_id, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/repo-cards/{card_id}/to-shared")
def repo_card_to_shared(card_id: str, conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    from ..services import harvest
    try:
        return harvest.repo_card_to_shared(
            conn, card_id, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="card not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/repos/{repo:path}")
def repo_profile(repo: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    profile = repo_catalog.get_profile(conn, repo)
    if profile is None:
        raise HTTPException(status_code=404, detail="repo not found")
    return profile
