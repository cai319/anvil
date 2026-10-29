"""Reminder endpoints（M4 / design §6.4 / §7.2 / §10）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..deps import Auth, get_auth, get_db
from ..util import ConflictError

router = APIRouter()


def _is_agent(auth: Auth) -> bool:
    return bool(auth.role and auth.role != "human")


def _owned_reminder(conn, rid: str, auth: Auth):
    """Agent 只能操作自己的 reminder；人类可操作全部。"""
    reminder = services.reminders.get_reminder(conn, rid)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if _is_agent(auth) and reminder["role_slug"] != auth.role:
        raise HTTPException(status_code=403, detail="reminder belongs to another role")
    return reminder


@router.get("/reminders")
def list_reminders(role_slug: str | None = None, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    # Agent 只能看到自己的提醒；人类可按 role_slug 过滤或查看全部。
    if _is_agent(auth):
        role_slug = auth.role
    return services.reminders.list_reminders(conn, role_slug)


@router.post("/reminders", status_code=201)
def add_reminder(body: dict, conn=Depends(get_db),
                 auth: Auth = Depends(get_auth)):
    requested = str(body.get("role_slug") or "").strip() or None
    if _is_agent(auth):
        if requested and requested != auth.role:
            raise HTTPException(status_code=403, detail="cannot create a reminder for another role")
        requested = auth.role
    try:
        return services.reminders.create_reminder(
            conn,
            role_slug=requested,
            kind=body.get("kind", "cron"),
            cron_expr=body.get("cron_expr"),
            fire_at=body.get("fire_at"),
            when_condition=body.get("when_condition"),
            prompt=body.get("prompt"),
        )
    except (ConflictError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.put("/reminders/{rid}")
def update_reminder(rid: str, body: dict, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    _owned_reminder(conn, rid, auth)
    if _is_agent(auth) and "role_slug" in body and body.get("role_slug") != auth.role:
        raise HTTPException(status_code=403, detail="cannot reassign a reminder to another role")
    try:
        reminder = services.reminders.update_reminder(conn, rid, **body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    return reminder


@router.delete("/reminders/{rid}", status_code=204)
def delete_reminder(rid: str, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    _owned_reminder(conn, rid, auth)
    if not services.reminders.delete_reminder(conn, rid):
        raise HTTPException(status_code=404, detail="reminder not found")
    return None


@router.post("/reminders/{rid}/fire-now")
def fire_now(rid: str, request: Request, conn=Depends(get_db),
             auth: Auth = Depends(get_auth)):
    """手动触发（调试用）：创建对应 run 并标记 reminder 已触发。"""
    reminder = _owned_reminder(conn, rid, auth)
    if not reminder["role_slug"]:
        raise HTTPException(status_code=422, detail="reminder has no role_slug")
    run = services.reminders.create_reminder_run(
        conn, reminder["role_slug"], reminder["prompt"] or "定时提醒")
    if reminder["kind"] == "cron":
        services.reminders.update_reminder(conn, rid, status="pending")
    else:
        services.reminders.update_reminder(conn, rid, status="fired")
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    return run
