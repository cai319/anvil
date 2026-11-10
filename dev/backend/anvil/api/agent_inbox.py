"""Agent Inbox 端点（拉模式）：check 只读、commit 推进 cursor。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db

router = APIRouter()


def _current_run_context(conn, auth: Auth) -> tuple[str | None, str | None]:
    if not auth.run_id:
        return None, None
    row = conn.execute(
        "SELECT task_id, slice_key FROM runs WHERE id=?", (auth.run_id,)).fetchone()
    if row is None:
        return None, None
    return row["task_id"], row["slice_key"]


@router.get("/me/inbox")
def check_inbox(task_id: str | None = None, limit: int = 30,
                slice_key: str | None = None, task_wide: bool = False,
                conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    if not auth.role:
        raise HTTPException(status_code=400, detail="agent session required")
    auth_task, auth_slice = _current_run_context(conn, auth)
    if not task_id:
        task_id = auth_task
        if not task_id:
            row = conn.execute(
                "SELECT task_id FROM runs WHERE role_slug=? AND task_id IS NOT NULL "
                "AND status IN ('created','projecting','running','suspended') "
                "ORDER BY started_at DESC LIMIT 1", (auth.role,)
            ).fetchone()
            task_id = row["task_id"] if row else None
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id is required")
    if not task_wide and slice_key is None and task_id == auth_task:
        slice_key = auth_slice
    if task_wide:
        slice_key = None
    items = services.agent_inbox.items(
        conn, auth.role, task_id, limit=limit, slice_key=slice_key)
    return {
        "task_id": task_id,
        "slice_key": slice_key,
        "cursor": services.agent_inbox._cursor(conn, auth.role, task_id, slice_key),
        "unread": services.agent_inbox.unread_count(
            conn, auth.role, task_id, slice_key),
        "items": items,
    }


@router.post("/me/inbox/commit")
def commit_inbox(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    if not auth.role:
        raise HTTPException(status_code=400, detail="agent session required")
    auth_task, auth_slice = _current_run_context(conn, auth)
    task_id = str(body.get("task_id") or "")
    if not task_id:
        task_id = auth_task or ""
    if not task_id:
        row = conn.execute(
            "SELECT task_id FROM runs WHERE role_slug=? AND task_id IS NOT NULL "
            "AND status IN ('created','projecting','running','suspended') "
            "ORDER BY started_at DESC LIMIT 1", (auth.role,)
        ).fetchone()
        task_id = row["task_id"] if row else ""
    task_wide = bool(body.get("task_wide"))
    slice_key = body.get("slice_key")
    if task_wide:
        slice_key = None
    elif slice_key is None and task_id == auth_task:
        slice_key = auth_slice
    up_to = int(body.get("up_to") or 0)
    if not task_id or up_to <= 0:
        raise HTTPException(status_code=422, detail="task_id and up_to are required")
    cursor = services.agent_inbox.commit(
        conn, auth.role, task_id, up_to, slice_key=slice_key)
    return {"task_id": task_id, "slice_key": slice_key, "cursor": cursor}
