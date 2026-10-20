"""Agent Inbox 端点（拉模式）：check 只读、commit 推进 cursor。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db

router = APIRouter()


@router.get("/me/inbox")
def check_inbox(task_id: str | None = None, limit: int = 30, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    if not auth.role:
        raise HTTPException(status_code=400, detail="agent session required")
    if not task_id:
        row = conn.execute(
            "SELECT task_id FROM runs WHERE role_slug=? AND task_id IS NOT NULL "
            "AND status IN ('created','projecting','running','suspended') "
            "ORDER BY started_at DESC LIMIT 1", (auth.role,)
        ).fetchone()
        task_id = row["task_id"] if row else None
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id is required")
    items = services.agent_inbox.items(conn, auth.role, task_id, limit=limit)
    return {
        "task_id": task_id,
        "cursor": services.agent_inbox._cursor(conn, auth.role, task_id),
        "unread": services.agent_inbox.unread_count(conn, auth.role, task_id),
        "items": items,
    }


@router.post("/me/inbox/commit")
def commit_inbox(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    if not auth.role:
        raise HTTPException(status_code=400, detail="agent session required")
    task_id = str(body.get("task_id") or "")
    if not task_id:
        row = conn.execute(
            "SELECT task_id FROM runs WHERE role_slug=? AND task_id IS NOT NULL "
            "AND status IN ('created','projecting','running','suspended') "
            "ORDER BY started_at DESC LIMIT 1", (auth.role,)
        ).fetchone()
        task_id = row["task_id"] if row else ""
    up_to = int(body.get("up_to") or 0)
    if not task_id or up_to <= 0:
        raise HTTPException(status_code=422, detail="task_id and up_to are required")
    cursor = services.agent_inbox.commit(conn, auth.role, task_id, up_to)
    return {"task_id": task_id, "cursor": cursor}
