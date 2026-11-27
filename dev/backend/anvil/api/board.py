"""目标拆解端点（编排闭环 A）：decompose / drafts / approve / reject。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..services import ledger
from ..deps import Auth, get_auth, get_db, require_human
from ..services.tasks import ConflictError
from ..util import jdumps, now_iso
from . import access, events

router = APIRouter()


def _can_access_draft(conn, auth: Auth, draft: dict) -> bool:
    if not access.is_agent(auth):
        return True
    if auth.task_id:
        return str(draft.get("source_task") or "") == auth.task_id
    source_task = draft.get("source_task")
    if source_task:
        return access.can_task_read(conn, auth, str(source_task))
    workspace_id = draft.get("workspace_id")
    if workspace_id:
        return access.can_workspace_read(conn, auth, str(workspace_id))
    return False


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
                conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    drafts = services.board.list_drafts(conn, workspace_id, source_task=source_task)
    if access.is_agent(auth):
        drafts = [d for d in drafts if _can_access_draft(conn, auth, d)]
    return drafts


@router.get("/board/drafts/{did}")
def get_draft(did: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    draft = services.board.get_draft(conn, did)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if not _can_access_draft(conn, auth, draft):
        raise HTTPException(status_code=403, detail="agent cannot read this draft")
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
    events.publish("task", {
        "id": (result or {}).get("source_task"),
        "kind": "board_draft",
        "status": (result or {}).get("status"),
        "draft_id": did,
    })
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
    if access.is_agent(auth):
        source_task = str(body.get("source_task") or "").strip()
        workspace_id = str(body.get("workspace_id") or "").strip()
        if source_task:
            access.assert_task_read(conn, auth, source_task)
        elif workspace_id:
            access.assert_workspace_write(conn, auth, workspace_id)
        else:
            raise HTTPException(
                status_code=403,
                detail="agent draft must reference a task or workspace it participates in")
    if access.is_agent(auth):
        # 真实 Agent 不能伪造 created_by；测试旁路仍按声明的角色记录，覆盖审批校验链路。
        created_by = f"agent:{auth.role}"
    else:
        created_by = body.get("created_by") or (
            f"agent:{auth.role}" if auth.role and auth.role != "human" else "human")
    try:
        draft = services.board.create_draft(
            conn, goal=goal, tree=tree, created_by=created_by,
            workspace_id=body.get("workspace_id"), source_task=body.get("source_task"),
            kind=str(body.get("kind") or "tasks"),
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {
        "id": draft.get("source_task"),
        "kind": "board_draft",
        "status": draft.get("status"),
        "draft_id": draft.get("id"),
    })
    return draft


def _audit_human_decision(conn, task_id: str | None, action: str,
                          request: Request, payload: dict) -> None:
    """HITL 决策留痕：记录发起请求的客户端特征，区分 Web UI 与本地脚本。"""
    if not task_id:
        return
    try:
        ledger.emit_audit(
            conn, role_slug="human",
            task_id=task_id,
            action=action,
            payload=jdumps({
                **payload,
                "client": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "referer": request.headers.get("referer"),
                "origin": request.headers.get("origin"),
            }),
            now=now_iso())
        conn.commit()
    except Exception:  # noqa: BLE001 —— 审计失败不阻断已完成的决策
        pass


@router.get("/board/drafts/{did}/participants")
def draft_participants(did: str, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    """审批前预览：workflow 角色与任务参与者的差异（add/missing/invalid）。"""
    from ..services import workflow as workflow_service

    draft = services.board.get_draft(conn, did)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if (draft.get("kind") or "tasks") != "workflow":
        raise HTTPException(status_code=422, detail="only workflow drafts have participants")
    source_task = draft.get("source_task")
    if not source_task:
        raise HTTPException(status_code=422, detail="draft has no source_task")
    return workflow_service.preview_participant_sync(
        conn, source_task, draft.get("tree") or {})


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
    _wake_role_approval_planners(conn, request, result)
    seen_tasks: set[str] = set()
    for did in draft_ids:
        row = conn.execute(
            "SELECT source_task, status FROM board_drafts WHERE id=?", (did,)).fetchone()
        task_id = row["source_task"] if row else None
        if task_id and task_id not in seen_tasks:
            seen_tasks.add(task_id)
            _audit_human_decision(conn, task_id, "board.approve", request, {
                "draft_ids": draft_ids,
                "landed": result.get("landed", []),
                "role_suggestions": result.get("role_suggestions") or {},
                "human_decision": True,
                "restart_running": bool(body.get("restart_running")),
                "restart_reason": body.get("restart_reason"),
            })
        events.publish("task", {
            "id": task_id,
            "kind": "board_draft",
            "status": row["status"] if row else "approved",
            "draft_id": did,
        })
    return result


def _wake_role_approval_planners(conn, request: Request, result: dict) -> None:
    """authorize 角色建议批准后唤醒 planner 去创建/绑定角色。

    批准本身只授权；planner 必须把这些角色真正创建并加入任务后，工作流审批
    门禁才会放行。唤醒失败不回滚批准，只把结果写回 outcome/warnings。
    """
    outcomes = result.get("role_suggestions") or {}
    groups: dict[tuple[str, str], dict] = {}
    for did, outcome in outcomes.items():
        if outcome.get("action") != "authorized" or outcome.get("already_satisfied"):
            continue
        task_id = str(outcome.get("task_id") or "")
        planner = str(outcome.get("planner") or "").strip()
        if not task_id or not planner:
            outcome["wake"] = "skipped"
            outcome["wake_reason"] = "no planner to wake"
            result.setdefault("warnings", []).append(
                f"{did}: role authorization approved but no planner is available")
            continue
        if planner == "human":
            outcome["wake"] = "skipped"
            outcome["wake_reason"] = "planner is human"
            continue
        task = conn.execute(
            "SELECT status, archived_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None or task["status"] in ("done", "closed") or task["archived_at"]:
            outcome["wake"] = "skipped"
            outcome["wake_reason"] = "task is terminal or archived"
            continue
        role = services.roles.get(conn, planner)
        if (planner != services.roles.ONBOARDING_SLUG
                and (role is None or not role.get("enabled", True))):
            outcome["wake"] = "skipped"
            outcome["wake_reason"] = "planner role is missing or disabled"
            continue
        clar = conn.execute(
            "SELECT clarification_status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (clar and clar["clarification_status"] in ("in_progress", "stale")):
            outcome["wake"] = "skipped"
            outcome["wake_reason"] = "clarification is active"
            continue
        key = (task_id, planner)
        group = groups.setdefault(key, {
            "task_id": task_id,
            "planner": planner,
            "draft_ids": [],
            "authorized": [],
            "needs_creation": [],
            "needs_planner": [],
            "outcomes": [],
        })
        group["draft_ids"].append(did)
        group["authorized"].extend(outcome.get("authorized") or [])
        group["needs_creation"].extend(outcome.get("needs_creation") or [])
        group["needs_planner"].extend(outcome.get("needs_planner") or [])
        group["outcomes"].append(outcome)

    for (task_id, planner), group in groups.items():
        authorized = list(dict.fromkeys(group["authorized"]))
        missing = list(dict.fromkeys(group["needs_creation"]))
        unbound = list(dict.fromkeys(group["needs_planner"]))
        assignees = [r["role_slug"] for r in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at",
            (task_id,))]
        pending_wf = conn.execute(
            "SELECT id FROM board_drafts WHERE source_task=? AND kind='workflow' "
            "AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (task_id,)).fetchone()
        running_id, running_roles = services.board.running_workflow_roles(
            conn, task_id)
        uncovered = [slug for slug in authorized if slug not in running_roles] \
            if running_id else []
        lines = [
            "human 已批准角色追加授权，但尚未完成创建/绑定：",
            "批准清单：" + "、".join(authorized),
            "已存在待绑定：" + ("、".join(unbound) or "无"),
            "不存在待创建：" + ("、".join(missing) or "无"),
            "",
            "请在本任务内完成：",
            '1. 只对批准清单中不存在的角色执行 `anvil role expand --brief "<定位>" --slug <slug>`；',
            f"2. 执行 `anvil task assign {task_id} --assignees {','.join(authorized)}` 绑定全部批准角色；",
        ]
        if pending_wf:
            lines.append(
                f"3. 当前已有 pending 工作流草案 {pending_wf['id']}；不要重复提交草案。"
                "确认该草案覆盖上述角色；如果不覆盖，comment 说明需由 human 驳回后重规划。")
        else:
            lines.append("3. 如需新工作流，按正常规划流程提交草案。")
        if running_id and uncovered:
            lines.append(
                f"4. 当前 running workflow {running_id} 未覆盖角色：{', '.join(uncovered)}。"
                "不要直接绑定这些角色；先 comment 告知 human 需要 workflow 修订。")
        lines.append("完成后用 `anvil task comment` 汇报创建/绑定结果。")
        try:
            run = services.planning.start_planner_run(
                conn, request.app.state.db_path,
                getattr(request.app.state, "orchestrator", None),
                task_id, planner, assignees=assignees,
                extra_constraint="\n".join(lines), replace_active=True)
        except Exception as exc:  # noqa: BLE001
            for outcome in group["outcomes"]:
                outcome["wake"] = "failed"
                outcome["wake_reason"] = str(exc)
            result.setdefault("warnings", []).append(
                f"{planner}: role authorization approved but planner wake failed: {exc}")
            continue
        for outcome in group["outcomes"]:
            outcome["wake"] = "started"
            outcome["wake_reason"] = None
            outcome["run_id"] = run.get("id")
        result.setdefault("replans", []).append({
            "draft_id": group["draft_ids"][0],
            "run_id": run.get("id"),
            "planner": planner,
            "authorized": authorized,
        })
        events.publish("task", {"id": task_id, "kind": "plan", "planner": planner})


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
    events.publish("task", {
        "id": draft.get("source_task"),
        "kind": "board_draft",
        "status": "rejected",
        "draft_id": did,
    })
    # 驳回后由草案作者（通常是 Cindy）重新规划；不替自然人做其它角色决策。
    replan = _wake_rejected_planner(conn, request, draft, body.get("feedback"))
    return {**draft, "replan": replan, "blocked": result.get("blocked")}
