"""目标拆解端点（编排闭环 A）：decompose / drafts / approve / reject。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db

router = APIRouter()


@router.get("/board/templates")
def list_templates():
    return services.board.list_templates()


@router.post("/board/decompose")
def decompose(body: dict, conn=Depends(get_db)):
    goal = body.get("goal")
    if not goal:
        raise HTTPException(status_code=422, detail="goal is required")
    return services.board.decompose(
        conn, goal, body.get("workspace_id"), body.get("created_by", "orchestrator"),
        template=body.get("template", "design-dev-test"),
    )


@router.get("/board/drafts")
def list_drafts(workspace_id: str | None = None, source_task: str | None = None,
                conn=Depends(get_db)):
    return services.board.list_drafts(conn, workspace_id, source_task=source_task)


@router.post("/board/drafts", status_code=201)
def create_draft(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """agent（planner）或人提交工作流草案（P1-0 计划先行）。

    source_task 关联发起任务；approve 后子任务落其下并按依赖自动派发。
    agent 会话只能为自身绑定任务创建草案（防跨任务写入）。
    """
    goal = body.get("goal")
    tree = body.get("tree")
    if not goal or not isinstance(tree, dict):
        raise HTTPException(status_code=422, detail="goal and tree are required")
    if auth.role and body.get("source_task") and auth.task_id and body["source_task"] != auth.task_id:
        raise HTTPException(status_code=403, detail="session not bound to this task")
    created_by = body.get("created_by") or (f"agent:{auth.role}" if auth.role else "human")
    return services.board.create_draft(
        conn, goal=goal, tree=tree, created_by=created_by,
        workspace_id=body.get("workspace_id"), source_task=body.get("source_task"),
        kind=str(body.get("kind") or "tasks"),
    )


@router.post("/board/drafts/approve")
def approve_drafts(body: dict, conn=Depends(get_db)):
    return services.board.approve(conn, body.get("draft_ids", []))


@router.post("/board/drafts/reject")
def reject_draft(body: dict, conn=Depends(get_db)):
    did = body.get("draft_id")
    if not did:
        raise HTTPException(status_code=422, detail="draft_id is required")
    return services.board.reject(conn, did)
