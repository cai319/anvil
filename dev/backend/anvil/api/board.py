"""目标拆解端点（编排闭环 A）：decompose / drafts / approve / reject。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..services.tasks import ConflictError
from . import events

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
    try:
        return services.board.create_draft(
            conn, goal=goal, tree=tree, created_by=created_by,
            workspace_id=body.get("workspace_id"), source_task=body.get("source_task"),
            kind=str(body.get("kind") or "tasks"),
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _start_replans(request: Request, conn, result: dict) -> dict:
    """执行 board 决策返回的重规划描述；失败只记 warning，不回滚已批准的角色。"""
    runs = []
    for replan in result.get("replans", []):
        try:
            run = services.planning.start_planner_run(
                conn, request.app.state.db_path,
                getattr(request.app.state, "orchestrator", None),
                replan["task_id"], replan["planner"],
                assignees=replan.get("assignees"),
                extra_constraint=replan.get("extra_constraint", ""),
                replace_active=bool(replan.get("replace_active")),
            )
            runs.append(run)
            events.publish("task", {"id": replan["task_id"], "kind": "plan",
                                    "planner": replan["planner"]})
        except Exception as exc:  # noqa: BLE001
            result.setdefault("warnings", []).append(f"replan failed: {exc}")
    if runs:
        result["runs"] = runs
    return result


@router.post("/board/drafts/approve")
def approve_drafts(body: dict, request: Request, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    try:
        result = services.board.approve(conn, body.get("draft_ids", []))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _start_replans(request, conn, result)


@router.post("/board/drafts/reject")
def reject_draft(body: dict, request: Request, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    did = body.get("draft_id")
    if not did:
        raise HTTPException(status_code=422, detail="draft_id is required")
    try:
        result = services.board.reject(
            conn, did, feedback=body.get("feedback"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    replan = result.get("replan")
    if replan:
        result["replans"] = [replan]
        _start_replans(request, conn, result)
    # 兼容旧客户端：驳回响应仍以 draft 字段为主体（status/tree/...）。
    draft = result.get("draft") or {}
    payload = {**draft, "replan": replan, "blocked": result.get("blocked")}
    if result.get("runs"):
        payload["runs"] = result["runs"]
    if result.get("warnings"):
        payload["warnings"] = result["warnings"]
    return payload
