"""目标拆解端点（编排闭环 A）：decompose / drafts / approve / reject。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..services.tasks import ConflictError
from ..util import jdumps, now_iso

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


@router.get("/board/drafts/{did}")
def get_draft(did: str, conn=Depends(get_db)):
    draft = services.board.get_draft(conn, did)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    return draft


@router.patch("/board/drafts/{did}")
def update_draft(did: str, body: dict, request: Request, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    tree = body.get("tree")
    if not isinstance(tree, dict):
        raise HTTPException(status_code=422, detail="tree is required")
    try:
        result = services.board.update_draft(
            conn, did, tree, goal=body.get("goal"),
            workspace_id=body.get("workspace_id"), by="human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit_human_decision(conn, (result or {}).get("source_task"),
                          "board.draft.update", request, {"draft_id": did})
    return result


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
    try:
        return services.board.create_draft(
            conn, goal=goal, tree=tree, created_by=created_by,
            workspace_id=body.get("workspace_id"), source_task=body.get("source_task"),
            kind=str(body.get("kind") or "tasks"),
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _audit_human_decision(conn, task_id: str | None, action: str,
                          request: Request, payload: dict) -> None:
    """HITL 决策留痕：记录发起请求的客户端特征，区分 Web UI 与本地脚本。"""
    if not task_id:
        return
    try:
        conn.execute(
            "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            ("human", task_id, action, jdumps({
                **payload,
                "client": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "referer": request.headers.get("referer"),
                "origin": request.headers.get("origin"),
            }), now_iso()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 —— 审计失败不阻断已完成的决策
        pass


@router.post("/board/drafts/approve")
def approve_drafts(body: dict, request: Request, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    draft_ids = body.get("draft_ids", [])
    try:
        result = services.board.approve(
            conn, draft_ids,
            restart_running=bool(body.get("restart_running")),
            restart_reason=body.get("restart_reason"),
            restart_note=body.get("restart_note"),
        )
    except ConflictError as exc:
        impact = getattr(exc, "impact", None)
        if impact:
            raise HTTPException(status_code=409, detail={
                "message": str(exc), "impact": impact})
        raise HTTPException(status_code=409, detail=str(exc))
    seen_tasks: set[str] = set()
    for did in draft_ids:
        row = conn.execute(
            "SELECT source_task FROM board_drafts WHERE id=?", (did,)).fetchone()
        task_id = row["source_task"] if row else None
        if task_id and task_id not in seen_tasks:
            seen_tasks.add(task_id)
            _audit_human_decision(conn, task_id, "board.approve", request, {
                "draft_ids": draft_ids,
                "landed": result.get("landed", []),
                "human_decision": True,
                "restart_running": bool(body.get("restart_running")),
                "restart_reason": body.get("restart_reason"),
            })
    return result


def _wake_rejected_planner(conn, request: Request, draft: dict,
                           feedback: str | None) -> dict | None:
    """驳回 workflow 草案后唤醒作者（通常是 Cindy）重新规划。

    `services.board.reject` 只写 mention 活动，不等于唤醒：daemon 不轮询 activities，
    真正的唤醒入口在 @mention API。这里复用 planner run 语义，避免驳回后无人接手。
    """
    if draft.get("kind") not in ("workflow", "workflow_revision"):
        return None
    task_id = draft.get("source_task")
    if not task_id:
        return None
    task = services.tasks.get(conn, task_id)
    if task is None or task.get("status") in ("done", "closed"):
        return None
    creator = str(draft.get("created_by") or "").strip()
    if creator.startswith("agent:"):
        creator = creator[6:]
    planner = creator or str(task.get("planner") or "").strip()
    if not planner or planner == "human":
        return None
    role = services.roles.get(conn, planner)
    if (planner != services.roles.ONBOARDING_SLUG
            and not (role and role.get("enabled", True))):
        return None
    assignees = [r["role_slug"] for r in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=?", (task_id,))]
    try:
        run = services.planning.start_planner_run(
            conn, request.app.state.db_path,
            getattr(request.app.state, "orchestrator", None),
            task_id, planner, assignees=assignees,
            extra_constraint=(
                f"自然人驳回了你提交的{('工作流修订' if draft.get('kind') == 'workflow_revision' else '工作流草案')}"
                f"（draft={draft.get('id')}）。\n"
                f"驳回意见：{feedback or '请重新评估工作流方案'}\n\n"
                "请读取该草案、任务最新状态和已有 workflow，按规划职责重新提交："
                "能用增量修订表达时先用 `anvil workflow revise --preview`，否则提交新的完整草案。"
                "不要原样复用被驳回的 tree；每个 loop 阶段必须显式写 "
                "subagent_hint true/false。"),
            replace_active=True,
        )
    except Exception:  # noqa: BLE001 —— 唤醒失败不改变驳回结果
        return None
    from . import events
    events.publish("task", {"id": task_id, "kind": "plan", "planner": planner})
    return {"run_id": run.get("id"), "planner": planner}


@router.post("/board/drafts/reject")
def reject_draft(body: dict, request: Request, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    did = body.get("draft_id")
    if not did:
        raise HTTPException(status_code=422, detail="draft_id is required")
    try:
        result = services.board.reject(conn, did, feedback=body.get("feedback"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    draft = result.get("draft") or {}
    _audit_human_decision(conn, draft.get("source_task"), "board.reject", request, {
        "draft_id": did,
        "feedback": body.get("feedback"),
        "human_decision": True,
    })
    # 驳回后由草案作者（通常是 Cindy）重新规划；不替自然人做其它角色决策。
    replan = _wake_rejected_planner(conn, request, draft, body.get("feedback"))
    return {**draft, "replan": replan, "blocked": result.get("blocked")}
