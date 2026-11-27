"""Task endpoints: CRUD + manual assign + agent-side transitions (§3.1/§10)."""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import config, services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import (
    ClaimIn, CommentIn, DelegationOut, DepIn, ReviewIn, SubmitIn, TaskChildOut,
    TaskChildrenOut, TaskDispatchOut, TaskIn, TaskKnowledgeOut, TaskOut,
    TaskPatch, TaskRoleStatesBatchOut, TaskRoleStatesOut, SubagentRecordOut,
    TimelineEventOut,
)
from ..services import knowledge_quality, ledger, repo_catalog
from ..services.tasks import ConflictError
from ..util import jdumps, now_iso
from . import access, events

router = APIRouter()


def _assert_session_task(conn, auth: Auth, tid: str) -> None:
    """运行级会话只能操作自己绑定/参与的任务。"""
    access.assert_task_read(conn, auth, tid)


def _pump_after_submit(request: Request, conn, tid: str) -> None:
    """任务 submit 成功后 pump 运行图（run running→submitted→reviewing→interrupt）。"""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return
    active = services.runs.find_active_run(conn, tid, slice_key=None)
    if active and active["status"] == "running":
        try:
            orch.pump(request.app.state.db_path, active["id"], "submit", {})
        except Exception:  # noqa: BLE001 —— 图失败不影响已成功的 task 变更，靠 reconcile 自愈
            pass


def _resume_after_review(request: Request, conn, tid: str, approve: bool) -> None:
    """任务 review 成功后恢复运行图的人审 interrupt。"""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return
    active = services.runs.find_active_run(conn, tid, slice_key=None)
    if active and active["status"] == "reviewing":
        try:
            orch.resume_review(request.app.state.db_path, active["id"], approve)
        except Exception:  # noqa: BLE001
            pass


@router.post("", status_code=201, response_model=TaskOut)
def create_task(body: TaskIn, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if auth.task_id and body.parent_id and body.parent_id != auth.task_id:
        raise HTTPException(
            status_code=403, detail="session is bound to another task")
    if is_real_agent:
        if body.issue_board_enabled:
            raise HTTPException(
                status_code=403,
                detail="issue board must be enabled by a human",
            )
        # 主任务保持 human 创建；角色运行时只能在“自己参与的任务”下创建子任务。
        if not body.parent_id:
            raise HTTPException(
                status_code=403,
                detail="agents may only create subtasks under a parent task",
            )
        parent = services.tasks.get(conn, body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="parent task not found")
        if not services.tasks.is_task_participant(conn, body.parent_id, auth.role):
            raise HTTPException(
                status_code=403,
                detail="agent is not a participant of the parent task",
            )
    if body.parent_step_id:
        if not body.parent_id:
            raise HTTPException(
                status_code=422, detail="parent_step_id requires parent_id")
        step = conn.execute(
            "SELECT ws.workflow_id, ws.step_id, ws.role_slug, ws.status "
            "FROM workflow_steps ws JOIN workflows w ON w.id=ws.workflow_id "
            "WHERE w.task_id=? AND ws.step_id=? "
            "ORDER BY w.created_at DESC, w.rowid DESC LIMIT 1",
            (body.parent_id, body.parent_step_id)).fetchone()
        if step is None:
            raise HTTPException(status_code=404, detail="parent step not found")
        if step["status"] != "running":
            raise HTTPException(
                status_code=409,
                detail=f"parent step {body.parent_step_id} is {step['status']}, expected running")
        if is_real_agent and auth.role != step["role_slug"]:
            raise HTTPException(
                status_code=403,
                detail="only the parent step's role can create a delegation",
            )
        target_roles = ([body.assignee] if body.assignee else []) + list(
            body.assignees or [])
        if is_real_agent and step["role_slug"] in target_roles:
            raise HTTPException(
                status_code=409,
                detail=("cannot delegate to the current step's own role: "
                        "the parent step would wait on itself; do the work in "
                        "the current step or ask cindy to add a step"),
            )
        for slug in ([body.assignee] if body.assignee else []) + list(body.assignees or []):
            role_row = services.roles.get(conn, slug)
            if role_row is None or not role_row.get("enabled", True):
                raise HTTPException(
                    status_code=404, detail=f"delegate role not found or disabled: {slug}")
        if not body.type:
            body.type = "delegation"
    if auth.role:
        body.created_by = ("human" if auth.role == "human" else f"agent:{auth.role}")
    repos = [r.model_dump() if hasattr(r, "model_dump") else r
             for r in (body.knowledge_repos or [])]
    try:
        task = services.tasks.create(
        conn, id=body.id, title=body.title, type=body.type, priority=body.priority,
        tags=body.tags, repo=body.repo, source_repo=body.source_repo,
        workspace_id=body.workspace_id, base_branch=body.base_branch,
        scope_paths=body.scope_paths, description=body.description,
        acceptance=body.acceptance, assignee=body.assignee, assignees=body.assignees,
        plan_first=body.plan_first, planner=body.planner, plan_seed=body.plan_seed,
        created_by=body.created_by,
        parent_id=body.parent_id, parent_step_id=body.parent_step_id,
        due_at=body.due_at,
        depends_on=body.depends_on,
        knowledge_repos=repos or None,
        knowledge_repos_source="human" if auth.role in (None, "human") else "agent",
        issue_board_enabled=body.issue_board_enabled,
        )
        if is_real_agent:
            services.tasks.audit_create(
                conn, task["id"], auth.role,
                parent_id=body.parent_id,
                parent_step_id=body.parent_step_id,
                workspace_id=task.get("workspace_id"),
                repo=task.get("repo"),
            )
        return task
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{tid}/assign", response_model=TaskOut)
def assign_roles(tid: str, body: dict, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    """绑定任务参与角色（团队协作，多角色）。body: {roles: [slug, ...]}"""
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    conflict = services.tasks.planning_conflict(conn, task)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    roles = [str(r) for r in (body.get("roles") or [])]
    try:
        result = services.tasks.assign_roles(conn, tid, roles)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "kind": "assign"})
    return result


@router.post("/{tid}/assign-existing")
def assign_existing_roles(tid: str, body: dict, request: Request,
                          conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """规划者追加**现有角色**；已有 Roster 时先提交角色追加提案给 human。

    - 空 Roster：planner/onboarding 可直接绑定，不需要 HITL。
    - 已有 Roster：只追加新角色（不替换、不移除）；新增项必须来自 human 已批准的
      `role_suggestion`（apply_mode=authorize）提案。没有批准时自动创建/更新提案并
      @human，返回 202；human 反馈/批准后 planner 重试本接口即可。
    """
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    agent = auth.role
    if not agent or agent == "human":
        raise HTTPException(
            status_code=403, detail="agent planner session required; human uses /assign")
    if auth.task_id and auth.task_id != tid:
        raise HTTPException(status_code=403, detail="session is bound to another task")
    planner = task.get("planner") or ""
    if agent != planner and not services.roles.is_onboarding(conn, agent):
        raise HTTPException(
            status_code=403,
            detail="only the task planner or onboarding assistant can bind a roster")
    roles = [str(r).strip() for r in (body.get("roles") or []) if str(r).strip()]
    if not roles:
        raise HTTPException(status_code=422, detail="roles is required")
    for slug in roles:
        role = services.roles.get(conn, slug)
        if role is None:
            raise HTTPException(status_code=409, detail=f"role {slug} does not exist")
        if not role.get("enabled", True):
            raise HTTPException(status_code=409, detail=f"role {slug} is disabled")
        if services.roles.is_onboarding(conn, slug):
            raise HTTPException(
                status_code=409, detail=f"onboarding assistant {slug} cannot join tasks")

    existing = list(task.get("assignees") or [])
    existing_set = set(existing)
    added = [r for r in roles if r not in existing_set]
    if existing and added:
        authorized = services.board.authorized_assignee_additions(conn, tid)
        missing = [r for r in added if r not in authorized]
        if missing:
            draft = services.board.propose_assignee_additions(
                conn, tid, agent, missing,
                reason=str(body.get("reason") or "").strip() or None)
            events.publish("task", {"id": tid, "kind": "assign_proposed", "by": agent})
            return JSONResponse(status_code=202, content={
                "status": "pending_review",
                "task_id": tid,
                "draft_id": draft.get("id"),
                "requested_assignees": missing,
                "message": "任务已有 assignees；角色追加提案已提交给 human。"
                           "human 反馈/批准后，重新执行 anvil task assign。",
            })

    try:
        result = (services.tasks.add_assignees(conn, tid, roles) if existing
                  else services.tasks.assign_roles(conn, tid, roles))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    summary = "、".join(roles)
    from ..util import jdumps, now_iso
    ledger.emit_activity(
        conn, task_id=tid,
        role_slug=agent,
        kind="comment",
        payload=jdumps({"body": (f"在现有 Roster 上追加角色：{summary}。" if existing
                          else f"基于现有角色库绑定任务 Roster：{summary}。")}),
        now=now_iso())
    ledger.emit_audit(
        conn, role_slug=agent,
        task_id=tid,
        action="task.assign_existing",
        payload=jdumps({"roles": roles, "added": added, "existing": existing,
                 "planner": planner}),
        now=now_iso())
    conn.commit()
    events.publish("task", {"id": tid, "kind": "assign", "by": agent})
    return result


@router.get("", response_model=list[TaskOut])
def list_tasks(status: str | None = Query(default=None), assignee: str | None = Query(default=None),
               workspace_id: str | None = Query(default=None), type: str | None = Query(default=None),
               tag: str | None = Query(default=None),
               parent_id: str | None = Query(default=None),
               include_archived: bool = Query(default=False),
               archived_only: bool = Query(default=False),
               conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    tasks = services.tasks.list_all(conn, status=status, assignee=assignee,
                                    workspace_id=workspace_id, type=type, tag=tag,
                                    parent_id=parent_id,
                                    include_archived=include_archived,
                                    archived_only=archived_only)
    if access.is_agent(auth):
        if auth.task_id:
            tasks = [t for t in tasks if t["id"] == auth.task_id]
        else:
            tasks = [t for t in tasks
                     if access.can_task_read(conn, auth, t["id"])]
    return tasks


@router.get("/status-summary")
def task_status_summary(task_ids: str = Query(default=""), conn=Depends(get_db),
                        auth: Auth = Depends(get_auth)):
    """大厅/任务卡用的批量角色状态源：当前 workflow + 每角色最近一次 run。

    只返回展示所需字段；前端 taskRoleState 需要 workflow（失败/返工/等待人工）和
    最近 run（包括 completed/failed 等终态），不能只依赖 active runs。
    """
    ids = [x.strip() for x in task_ids.split(",") if x.strip()][:200]
    if access.is_agent(auth):
        ids = [tid for tid in ids if access.can_task_read(conn, auth, tid)]
    if not ids:
        return []
    out = []
    for tid in ids:
        wf = services.workflow.get_for_task(conn, tid)
        wf_summary = None
        if wf is not None:
            wf_summary = {
                "id": wf.get("id"),
                "status": wf.get("status"),
                "steps": [
                    {key: step.get(key) for key in (
                        "step_id", "role_slug", "status", "slice_key",
                        "recovery_count", "next_retry_at")}
                    for step in (wf.get("steps") or [])
                ],
            }
        rows = conn.execute(
            "SELECT role_slug, status, started_at FROM runs "
            "WHERE task_id=? AND role_slug IS NOT NULL "
            "ORDER BY COALESCE(started_at,'') DESC, rowid DESC", (tid,),
        ).fetchall()
        statuses: dict[str, dict] = {}
        for row in rows:
            role = row["role_slug"]
            if role not in statuses:
                statuses[role] = {
                    "status": row["status"],
                    "started_at": row["started_at"],
                }
        out.append({"task_id": tid, "workflow": wf_summary, "run_statuses": statuses})
    return out


@router.get("/role-states", response_model=TaskRoleStatesBatchOut)
def task_role_states_batch(task_ids: str = Query(...), conn=Depends(get_db),
                           auth: Auth = Depends(get_auth)):
    """批量角色状态（Lobby/列表页）；按任务读取权限过滤。"""
    ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]
    if len(ids) > 200:
        raise HTTPException(status_code=422, detail="too many task_ids (max 200)")
    from ..services import daemon as daemon_service
    from ..services import role_state as role_state_service

    mgr = daemon_service.get_manager()
    tasks: dict[str, list[dict]] = {}
    for tid in ids:
        if not access.can_task_read(conn, auth, tid):
            continue
        tasks[tid] = role_state_service.task_role_states(conn, tid, mgr)
    return {"tasks": tasks}


@router.get("/{tid}/role-states", response_model=TaskRoleStatesOut)
def task_role_states(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """任务内角色状态（服务端单一推导）；前端只做标签/颜色映射。"""
    access.assert_task_read(conn, auth, tid)
    from ..services import daemon as daemon_service
    from ..services import role_state as role_state_service

    mgr = daemon_service.get_manager()
    return {
        "task_id": tid,
        "roles": role_state_service.task_role_states(conn, tid, mgr),
    }


@router.get("/{tid}", response_model=TaskOut)
def get_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    return t


@router.get("/{tid}/knowledge/explain")
def explain_task_knowledge(tid: str, role: str | None = None,
                           conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """解释该任务起始上下文会带哪些知识（检索 scope/绑定/主题卡片）。"""
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if is_real_agent and role and role != auth.role:
        raise HTTPException(
            status_code=403, detail="agent can only explain its own role context")
    try:
        return services.context_explain.explain_task(
            conn, tid, role=role or (auth.role if is_real_agent else None))
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.get("/{tid}/knowledge", response_model=list[TaskKnowledgeOut])
def list_task_knowledge(tid: str, include_subtasks: bool = False, deep: bool = False,
                        conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    try:
        return services.task_knowledge.list_for_task(
            conn, tid, include_subtasks=include_subtasks, deep=deep)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.post("/{tid}/knowledge", status_code=201, response_model=TaskKnowledgeOut)
def create_task_knowledge(tid: str, body: dict, conn=Depends(get_db),
                          auth: Auth = Depends(get_auth)):
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if is_real_agent:
        access.assert_task_read(conn, auth, tid)
    participant = (not is_real_agent
                   or services.tasks.is_task_participant(conn, tid, auth.role))
    # Cindy 作为知识收割者可为非参与任务写 proposed 任务知识；不能直接写 active。
    harvester = (is_real_agent and auth.role is not None
                 and services.roles.is_onboarding(conn, auth.role))
    if not participant and not harvester:
        raise HTTPException(status_code=403, detail="agent is not a participant of this task")
    requested_status = body.get("status")
    if not participant:
        if requested_status not in (None, "proposed"):
            raise HTTPException(
                status_code=403,
                detail="knowledge harvester can only create proposed task knowledge")
        status = "proposed"
    else:
        # 显式 API 写入默认 active（任务参与角色共享，无需预审批）；
        # 自动捕获走 experience_capture，显式传 status='proposed'。
        status = str(requested_status or "active")
    try:
        return services.task_knowledge.create(
            conn, tid,
            title=str(body.get("title") or "").strip(),
            tldr=str(body.get("tldr") or "").strip(),
            kind=str(body.get("kind") or ""),
            fields=body.get("fields") or {},
            tags=body.get("tags") or [],
            source_role=auth.role,
            status=status,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except services.knowledge_card.IncompleteKnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{tid}/knowledge/{kid}/approve")
def approve_task_knowledge(tid: str, kid: str, conn=Depends(get_db),
                           auth: Auth = Depends(require_human)):
    """人工批准任务级知识草稿（proposed → active）。"""
    task = services.tasks.get(conn, tid)
    card = services.task_knowledge.get(conn, kid)
    if task is None or card is None or card.get("task_id") != tid:
        raise HTTPException(status_code=404, detail="knowledge not found")
    try:
        return services.task_knowledge.approve(
            conn, kid, actor=auth.role or "human")
    except knowledge_quality.QualityGateError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "quality": exc.quality,
        }) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{tid}/knowledge/{kid}", response_model=TaskKnowledgeOut)
def update_task_knowledge(tid: str, kid: str, body: dict,
                          conn=Depends(get_db),
                          auth: Auth = Depends(require_human)):
    """人工补全 proposed 任务知识，用于批准或晋升前修订。"""
    task = services.tasks.get(conn, tid)
    card = services.task_knowledge.get(conn, kid)
    if task is None or card is None or card.get("task_id") != tid:
        raise HTTPException(status_code=404, detail="knowledge not found")
    try:
        return services.task_knowledge.update(
            conn, kid, title=body.get("title"), tldr=body.get("tldr"),
            fields=body.get("fields"), tags=body.get("tags"),
            review_at=body.get("review_at"),
            expected_version=body.get("expected_version"),
            actor=auth.role or "human")
    except knowledge_quality.QualityGateError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "quality": exc.quality,
        }) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="knowledge not found") from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{tid}/knowledge/{kid}/promote")
def promote_task_knowledge(tid: str, kid: str, body: dict,
                           conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """任务级知识卡 → 主题知识库草稿（人工/角色都可 propose，人工批准生效）。"""
    from ..services import harvest
    card = services.task_knowledge.get(conn, kid)
    if card is None or card.get("task_id") != tid:
        raise HTTPException(status_code=404, detail="knowledge not found")
    repo = str(body.get("repo") or "").strip()
    if not repo:
        raise HTTPException(status_code=422, detail="repo is required")
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if is_real_agent:
        access.assert_task_read(conn, auth, tid)
    if is_real_agent and auth.role != card.get("source_role"):
        raise HTTPException(status_code=403, detail="agent did not author this card")
    try:
        return harvest.task_card_to_repo(
            conn, kid, repo, actor=auth.role or "human", status="proposed")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="knowledge not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{tid}/knowledge/{kid}", status_code=204)
def delete_task_knowledge(tid: str, kid: str, conn=Depends(get_db),
                          auth: Auth = Depends(require_human)):
    if not services.task_knowledge.delete(conn, kid, task_id=tid):
        raise HTTPException(status_code=404, detail="knowledge not found")
    return None


@router.get("/{tid}/timeline", response_model=list[TimelineEventOut])
def task_timeline(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """任务会话时间线：多角色交互流（认领/提交/评审/评论/运行生命周期）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    return services.tasks.timeline(conn, tid)


@router.get("/{tid}/activities")
def task_activities(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """任务活动事件（评论 / @提及 / run 迁移）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    return services.tasks.list_activities(conn, tid)


@router.get("/{tid}/diff")
def task_diff(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """任务当前改动 + 越界文件（前端 diff 折叠区）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    try:
        return services.tasks.task_diff(conn, tid)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.get("/{tid}/config-snapshot")
def task_config_snapshot(tid: str, conn=Depends(get_db),
                         auth: Auth = Depends(get_auth)):
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    payload = services.tasks.task_config_snapshot(conn, tid)
    if access.is_agent(auth):
        # 角色 system_prompt 属于角色私有配置；任务快照只保留执行所需字段。
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            for key in ("projection", "effective"):
                block = snapshot.get(key)
                if isinstance(block, dict):
                    block.pop("system_prompt", None)
    return payload


@router.get("/{tid}/bids")
def task_bids(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """认领出价记录（score 策略为 v2；先到先得模式下为空）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    access.assert_task_read(conn, auth, tid)
    return []


@router.delete("/{tid}", status_code=204)
def delete_task(tid: str, force: bool = False, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    """物理删除任务（含 runs/评论/依赖）。有活跃 run 或子任务时需 force=true。"""
    try:
        deleted = services.tasks.delete(conn, tid, force=force, role=auth.role)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "deleted": True})
    return None


@router.post("/{tid}/cancel", response_model=TaskOut)
def cancel_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        result = services.tasks.cancel(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/reopen", response_model=TaskOut)
def reopen_task(tid: str, request: Request, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    try:
        result = services.tasks.reopen(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result["status"] == "in_progress":
        # 重开即接回原执行：立即唤醒挂起/空闲的步骤 run，不等 10 分钟 watchdog。
        orch = getattr(request.app.state, "orchestrator", None)
        if orch:
            from ..services import daemon as daemon_service
            mgr = daemon_service.get_manager()
            for item in services.workflow.resume_runs_for_task(conn, tid):
                if item.get("run_status") == "running":
                    # 常驻进程仍在忙（或排队中）时不要打断；空闲/失联才 resume。
                    if mgr is None:
                        continue
                    try:
                        st = mgr.status(item["role_slug"])
                    except Exception:  # noqa: BLE001
                        st = {}
                    if st.get("switching"):
                        continue
                    if st.get("alive") and (
                            st.get("status") == "busy"
                            or int(st.get("queue_len") or 0) > 0):
                        continue
                try:
                    orch.pump(request.app.state.db_path, item["run_id"], "resume", {})
                except Exception:  # noqa: BLE001 —— 唤醒失败由 scheduler 兜底
                    pass
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/archive", response_model=TaskOut)
def archive_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        result = services.tasks.archive(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "archived": True})
    return result


@router.post("/{tid}/unarchive", response_model=TaskOut)
def unarchive_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        result = services.tasks.unarchive(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "archived": False})
    return result


@router.post("/archive-completed")
def archive_completed_tasks(workspace_id: str | None = Query(default=None),
                            conn=Depends(get_db), auth: Auth = Depends(require_human)):
    return services.tasks.archive_completed(conn, workspace_id=workspace_id, role=auth.role)


@router.patch("/{tid}", response_model=TaskOut)
def patch_task(tid: str, body: TaskPatch, force: bool = False,
               conn=Depends(get_db), auth: Auth = Depends(require_human)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    fields = body.model_dump(exclude_none=True)
    if "issue_board_enabled" in fields:
        if fields["issue_board_enabled"]:
            running = conn.execute(
                "SELECT 1 FROM workflows WHERE task_id=? AND status='running' LIMIT 1",
                (tid,)).fetchone()
            if task["status"] not in ("todo", "in_progress") or running is not None:
                raise HTTPException(
                    status_code=409,
                    detail="issue board can only be enabled before the workflow starts")
        else:
            findings_count = conn.execute(
                "SELECT COUNT(*) AS n FROM task_findings WHERE task_id=?", (tid,)
            ).fetchone()["n"]
            if findings_count and not force:
                raise HTTPException(
                    status_code=409,
                    detail="task has findings; pass force=true to hide the issue board")
    repos = fields.pop("knowledge_repos", None)
    if repos is not None:
        repos = [r if isinstance(r, dict) else {"repo": str(r)} for r in repos]
        services.knowledge_binding.set_manual(
            conn, tid, repos, source="human", lock=True)
    try:
        t = services.tasks.update(conn, tid, **fields)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "kind": "knowledge-repos"})
    return t


def _knowledge_repo_payload(conn, task: dict) -> dict:
    return {
        "task_id": task["id"],
        "locked": bool(task.get("knowledge_repos_locked")),
        "state": task.get("knowledge_binding_state"),
        "repos": task.get("knowledge_repos") or [],
        "scope": services.knowledge_binding.retrieval_scope(conn, task),
    }


@router.get("/{tid}/knowledge-repos")
def get_task_knowledge_repos(tid: str, conn=Depends(get_db)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _knowledge_repo_payload(conn, task)


@router.post("/{tid}/knowledge-repos/auto")
def reauto_task_knowledge_repos(tid: str, conn=Depends(get_db),
                                auth: Auth = Depends(require_human)):
    """人工重新识别：解除锁定并重跑 deterministic 匹配；保留 human/cindy 行。"""
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    conn.execute("UPDATE tasks SET knowledge_repos_locked=0 WHERE id=?", (tid,))
    conn.commit()
    repo_catalog.invalidate_catalog()
    services.knowledge_binding.auto_bind(conn, services.tasks.get(conn, tid) or task)
    task = services.tasks.get(conn, tid)
    events.publish("task", {"id": tid, "kind": "knowledge-repos"})
    return _knowledge_repo_payload(conn, task)


@router.post("/{tid}/knowledge-repos/{repo}/accept")
def accept_suggested_repo(tid: str, repo: str, conn=Depends(get_db),
                          auth: Auth = Depends(require_human)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    cur = conn.execute(
        "UPDATE task_knowledge_repos SET status='applied', source='human', updated_at=? "
        "WHERE task_id=? AND repo=? AND status='suggested'",
        (now_iso(), tid, repo))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="suggestion not found")
    ledger.emit_audit(
        conn, role_slug="human",
        task_id=tid,
        action="task.knowledge_suggestion_accept",
        payload="{}",
        now=now_iso())
    conn.commit()
    return _knowledge_repo_payload(conn, services.tasks.get(conn, tid))


@router.post("/{tid}/knowledge-repos/{repo}/dismiss")
def dismiss_suggested_repo(tid: str, repo: str, conn=Depends(get_db),
                           auth: Auth = Depends(require_human)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    cur = conn.execute(
        "UPDATE task_knowledge_repos SET status='dismissed', updated_at=? "
        "WHERE task_id=? AND repo=? AND status='suggested'",
        (now_iso(), tid, repo))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="suggestion not found")
    ledger.emit_audit(
        conn, role_slug="human",
        task_id=tid,
        action="task.knowledge_suggestion_dismiss",
        payload="{}",
        now=now_iso())
    conn.commit()
    return _knowledge_repo_payload(conn, services.tasks.get(conn, tid))


@router.post("/{tid}/claim", response_model=TaskOut)
def claim_task(tid: str, body: ClaimIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    # claim 是可竞争操作：只校验运行级 token 绑定；已被他人认领时由 claim 返回 409。
    access.assert_task_bound(conn, auth, tid)
    try:
        result = services.tasks.claim(conn, tid, auth.role or "qa-role", body.paths)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/unclaim", response_model=TaskOut)
def unclaim_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    try:
        result = services.tasks.unclaim(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/heartbeat", response_model=TaskOut)
def heartbeat_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    try:
        result = services.tasks.heartbeat(conn, tid, auth.role)
        if auth.run_id:
            services.workflow.renew_step_lease_for_run(conn, auth.run_id)
        return result
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.post("/{tid}/submit", response_model=TaskOut)
def submit_task(tid: str, body: SubmitIn, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    # 工作流任务的完成必须走 step 状态机；允许 task submit 会让任务在步骤未完成时
    # 提前进入 in_review，甚至被评审为 done 后中断剩余步骤。人类仍可强制收口。
    if auth.role and auth.role != "human":
        running = conn.execute(
            "SELECT 1 FROM workflows WHERE task_id=? AND status='running' LIMIT 1",
            (tid,),
        ).fetchone()
        if running is not None:
            raise HTTPException(
                status_code=409,
                detail=("task has a running workflow; use "
                        "`anvil step submit <workflow_id> <step_id>`"),
            )
    team = services.tasks.is_team_task(conn, tid)
    try:
        result = services.tasks.submit(
            conn, tid, summary=body.summary, branch=body.branch,
            changed_paths=body.changed_paths, role=auth.role,
            force_terminate_workflow=body.force_terminate_workflow)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # 团队任务由角色级 submit 直接推进任务状态，不走单 run 的 LangGraph 评审 interrupt。
    if not team:
        _pump_after_submit(request, conn, tid)
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/review", response_model=TaskOut)
def review_task(tid: str, body: ReviewIn, request: Request, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    try:
        result = services.tasks.review(
            conn, tid, body.approve, auth.role, feedback=body.feedback)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _resume_after_review(request, conn, tid, body.approve)
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.get("/{tid}/subagents", response_model=list[SubagentRecordOut])
def list_task_subagents(tid: str, status: str | None = None, limit: int = 100,
                        conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """该任务相关 run 的运行时子代理观测记录（只读，不参与调度）。"""
    _assert_session_task(conn, auth, tid)
    return services.runs.list_subagents(
        conn, task_id=tid, status=status, limit=limit)


@router.get("/{tid}/children", response_model=TaskChildrenOut)
def list_children(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """委派/派遣子任务的统一投影（relation_type 区分行为，字段归一）。"""
    _assert_session_task(conn, auth, tid)
    try:
        return services.child_tasks.list_children(conn, tid)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.get("/{tid}/delegations", response_model=list[DelegationOut])
def list_delegations(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """父任务下的委派工单（含旧子任务），供「委派」页签/step 详情使用。"""
    _assert_session_task(conn, auth, tid)
    try:
        return services.tasks.list_delegations(conn, tid)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


def _delegation_actor(task: dict, auth: Auth) -> str:
    """只有派发者本人或 human 能验收委派；负责人不能验收自己的产出。"""
    if auth.role == "human" or auth.test_bypass:
        return "human"
    created = str(task.get("created_by") or "")
    delegator = created[len("agent:"):] if created.startswith("agent:") else ""
    owner = str(task.get("assignee") or "")
    if auth.role and auth.role == delegator and auth.role != owner:
        return auth.role
    raise HTTPException(
        status_code=403,
        detail="only the delegator or a human can accept/reject this delegation",
    )


@router.post("/{tid}/delegation/accept", response_model=TaskOut)
def accept_delegation(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    actor = _delegation_actor(task, auth)
    try:
        result = services.tasks.accept_delegation(conn, tid, actor)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/delegation/reject", response_model=TaskOut)
def reject_delegation(tid: str, body: dict, conn=Depends(get_db),
                      auth: Auth = Depends(get_auth)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    actor = _delegation_actor(task, auth)
    try:
        result = services.tasks.reject_delegation(
            conn, tid, actor, str(body.get("feedback") or ""))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


def _auth_run_context(conn, auth: Auth, tid: str) -> dict:
    """当前运行级会话的 run/slice/step；人类/测试旁路/跨任务会话返回空。"""
    empty = {"run_id": None, "slice_key": None, "step_id": None}
    if auth.test_bypass or not auth.run_id:
        return empty
    row = conn.execute(
        "SELECT task_id, slice_key, step_id FROM runs WHERE id=?",
        (auth.run_id,)).fetchone()
    if row is None or row["task_id"] != tid:
        return empty
    return {"run_id": auth.run_id, "slice_key": row["slice_key"],
            "step_id": row["step_id"]}


def _auth_slice_key(conn, auth: Auth, tid: str) -> str | None:
    return _auth_run_context(conn, auth, tid)["slice_key"]


@router.post("/{tid}/note", response_model=TaskOut)
def context_note_task(tid: str, body: dict, conn=Depends(get_db),
                      auth: Auth = Depends(get_auth)):
    """写 task-wide 系统备注，供未来 slice/step 带入上下文；不唤醒任何角色。"""
    _assert_session_task(conn, auth, tid)
    role = auth.role or "human"
    if role != "human":
        allowed = (
            services.tasks.is_task_participant(conn, tid, role)
            or services.context_router.is_participant(conn, tid, role)
            or services.roles.is_onboarding(conn, role))
        if not allowed:
            raise HTTPException(
                status_code=403, detail="role is not a participant of this task")
    try:
        result = services.tasks.context_note(
            conn, tid, role, str(body.get("body") or body.get("message") or ""),
            str(body.get("for_step") or "").strip() or None)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "kind": "note"})
    return result


@router.post("/{tid}/comment", response_model=TaskOut)
def comment_task(tid: str, body: CommentIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    held = _held_response(conn, tid, auth, body.model_dump())
    if held is not None:
        return held
    try:
        result = services.tasks.comment(
            conn, tid, auth.role or "human", body.body,
            slice_key=_auth_slice_key(conn, auth, tid))
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "kind": "comment"})
    return result


def _wake_mentioned_role(conn, request: Request, task: dict, target: str,
                         message: str, sender: str, wf: dict | None,
                         *, source_slice_key: str | None = None,
                         source_run_id: str | None = None,
                         source_step_id: str | None = None,
                         explicit_mode: str | None = None) -> None:
    """按上下文路由唤醒被 @ 的角色：复用当前 / 切换切片 / 新建派遣子任务。

    - 同 task 同 slice：复用现有上下文；
    - 同 task 已知 slice：切回该 slice 槽；
    - 无关/无归属：创建派遣子任务（人类/显式模式始终允许；Agent 自动路由受开关控制），否则退化为本任务新回复 run。
    """
    orch = getattr(request.app.state, "orchestrator", None)
    findings_mode = (
        bool(task.get("issue_board_enabled"))
        and target in services.findings.reviewer_roles(conn, task["id"])
        and services.findings.board_status(conn, task["id"]) == "in_progress")
    if findings_mode:
        from ..services import daemon as daemon_service
        decision = {
            "mode": "switch",
            "target_task_id": task["id"],
            "target_slice_key": None,
            "context_key": daemon_service.context_key(task["id"]),
            "current_context_key": None,
            "current_run_id": None,
            "reasons": ["findings_task_level"],
            "score": None,
            "confidence": "hard",
        }
    else:
        decision = services.context_router.route(
            conn, target_role=target, source_task_id=task["id"],
            source_slice_key=source_slice_key,
            source_step_id=source_step_id, source_run_id=source_run_id,
            action_kind="mention", message=message,
            explicit_mode=explicit_mode)
    try:
        ledger.emit_audit(
            conn, role_slug=sender,
            task_id=task["id"],
            action="context.routing",
            payload=jdumps({
                 "target": target, "message": message[:200],
                 "source_slice_key": source_slice_key,
                 "source_run_id": source_run_id,
                 **decision,
             }),
            now=now_iso())
        conn.commit()
    except Exception:  # noqa: BLE001 —— 审计失败不阻断唤醒
        pass

    mode = decision.get("mode")
    # 人类/显式 dispatch 是明确意图，不受 Agent 自动路由开关限制；
    # 开关只控制 Agent 之间的自动升级。
    allow_dispatch = (
        config.DISPATCH_AUTO_FROM_MENTION
        or sender in (None, "human")
        or explicit_mode == "dispatch"
    )
    downgrade_reason = None
    if mode == "dispatch" and allow_dispatch:
        try:
            result = services.dispatch.create_dispatch(
                conn, source_task_id=task["id"], target_role=target,
                message=message, requester=sender,
                source_slice_key=source_slice_key,
                source_step_id=source_step_id,
                source_run_id=source_run_id,
                reason_code="auto_route",
                reason_json={"score": decision.get("score"),
                             "reasons": decision.get("reasons")},
            )
            run = result.get("run")
            if orch and run and not result.get("reused"):
                try:
                    orch.pump(request.app.state.db_path, run["id"], "wake", {})
                except Exception:  # noqa: BLE001
                    pass
            return
        except services.dispatch.DispatchError as exc:
            if str(exc).startswith("dispatch blocked by governance"):
                # frozen/预算/并发达限：不能再退化成 reply run 绕过治理闸门。
                try:
                    ledger.emit_audit(
                        conn, role_slug=sender,
                        task_id=task["id"],
                        action="dispatch.suppressed",
                        payload=jdumps({"target": target, "message": message[:200],
                                 "reason": str(exc)}),
                        now=now_iso())
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass
                return
            # 其他派遣失败（深度/权限/去重冲突）退化为本任务内的回复 run。
            mode = "new_slice"
            downgrade_reason = f"dispatch_failed: {exc}"
    if mode == "dispatch":
        mode = "new_slice"
        downgrade_reason = downgrade_reason or "dispatch_disabled_by_config"
    if downgrade_reason:
        # 首条 context.routing 审计记录的是路由决策；补一条最终生效模式，避免审计
        # 一直停留在未执行的 "dispatch"。
        try:
            ledger.emit_audit(
                conn, role_slug=sender,
                task_id=task["id"],
                action="context.routing.downgraded",
                payload=jdumps({"target": target, "original_mode": "dispatch",
                         "final_mode": mode, "reason": downgrade_reason,
                         "source_slice_key": source_slice_key,
                         "source_run_id": source_run_id}),
                now=now_iso())
            conn.commit()
        except Exception:  # noqa: BLE001 —— 审计失败不阻断唤醒
            pass

    target_task_id = decision.get("target_task_id") or task["id"]
    target_slice = decision.get("target_slice_key")
    # 同 task 的 mention 优先落在当前 running step 的 run 上：否则一旦创建过
    # reply run，find_active_run 会持续选中最新的 reply run，步骤 run 永久 suspended。
    step_run = None
    if target_task_id == task["id"]:
        step_run = services.runs.find_running_step_run(
            conn, target_task_id, target, slice_key=target_slice)
    active = step_run or services.runs.find_active_run(
        conn, target_task_id, target, slice_key=target_slice)
    using_step_run = active is step_run and step_run is not None
    if active:
        status = str(active.get("status") or "")
        if status == "suspended":
            # 挂起等待下一条消息：resume 现有 run，复用该上下文。
            hidden = (
                target_task_id != task["id"]
                or (source_slice_key is not None
                    and source_slice_key != target_slice))
            if hidden:
                # 跨 task/跨切片的 mention 不在目标 run 的 transcript 里；
                # 用一次性 wake_note 把请求本身带过去。
                try:
                    services.dispatch.append_wake_note(
                        conn, active["id"],
                        f"来源任务 {task['id']} 的 {sender} @你：\n{message}\n\n"
                        "请在本上下文处理该请求；处理完按系统回切提示返回原上下文。")
                except Exception:  # noqa: BLE001
                    pass
            if mode == "switch" and not using_step_run:
                try:
                    services.dispatch.record_context_return(
                        conn, switched_run_id=active["id"],
                        return_context_key=decision.get("current_context_key"),
                        return_run_id=decision.get("current_run_id"))
                except Exception:  # noqa: BLE001
                    pass
            if orch:
                try:
                    orch.pump(request.app.state.db_path, active["id"], "resume", {})
                except Exception:  # noqa: BLE001
                    pass
            return
        if status in ("submitted", "reviewing"):
            # HITL 评审中的 run 不能被 mention 拉回执行。
            return
        if using_step_run and status in ("created", "projecting"):
            # 步骤 run 还没真正启动：把 mention 作为一次性 wake_note 带过去，
            # 由它自己的 spawn 渲染进 STEP_PROTOCOL 提示词，不另建 reply run。
            try:
                services.dispatch.append_wake_note(
                    conn, active["id"],
                    f"来源任务 {task['id']} 的 {sender} @你：\n{message}")
                conn.commit()
            except Exception:  # noqa: BLE001
                pass
            if orch:
                try:
                    orch.pump(request.app.state.db_path, active["id"], "wake", {})
                except Exception:  # noqa: BLE001
                    pass
            return
        # running：不能 resume（会重复投递）；新建排队 run。

    source_note = (
        f"来源任务：{task['id']}"
        + (f" / 切片 {source_slice_key}" if source_slice_key else "")
        + f"\n路由：{mode}（{', '.join(decision.get('reasons') or [])}）\n\n")
    if findings_mode:
        run = services.runs.create_run(
            conn, target, target_task_id,
            prompt=("[anvil:findings]你在任务会话中被 "
                    f"{sender} @：\n{message}\n\n"
                    "请复核/更新问题板 findings；完成后执行 "
                    "`anvil finding complete --task $ANVIL_TASK_ID --summary \"...\"`。"))
    elif wf is not None or target == services.roles.ONBOARDING_SLUG:
        run = services.runs.create_run(
            conn, target, target_task_id,
            prompt=("[anvil:reply]你在任务会话中被 "
                    f"{sender} @：\n{source_note}{message}\n\n"
                    "请评估该反馈并回复你的判断与依据。"),
            slice_key=target_slice, origin="mention")
    else:
        run = services.runs.create_run(
            conn, target, target_task_id, slice_key=target_slice, origin="mention")
    # 回复 run 结束后应回到步骤 run：即使 mode=reuse（同 task+slice），只要
    # 已找到 running step 的 run 也要 arm 回切，而不是只写一条 activity。
    return_run_id = step_run["id"] if step_run else decision.get("current_run_id")
    if return_run_id and (step_run or mode == "switch"):
        return_key = decision.get("current_context_key")
        if step_run:
            from ..services import daemon as daemon_service
            return_key = daemon_service.context_key(target_task_id, target_slice)
        try:
            services.dispatch.record_context_return(
                conn, switched_run_id=run["id"],
                return_context_key=return_key, return_run_id=return_run_id)
        except Exception:  # noqa: BLE001
            pass
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass


@router.post("/{tid}/mention", response_model=TaskOut)
def mention_task(tid: str, body: dict, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """@提及某角色，注入注意事项/上下文；@的是角色时唤醒该角色（raft inbox：@ 即拉入）。"""
    _assert_session_task(conn, auth, tid)
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    held = _held_response(conn, tid, auth, body)
    if held is not None:
        return held
    target = str(body.get("target") or "human")
    message = _strip_leading_mention(conn, str(body.get("message") or ""), target)
    source_ctx = _auth_run_context(conn, auth, tid)
    explicit_slice = str(body.get("slice_key") or "").strip() or None
    if explicit_slice:
        source_ctx = {**source_ctx, "slice_key": explicit_slice}
    result = services.tasks.mention(
        conn, tid, auth.role or "human", target, message,
        slice_key=source_ctx["slice_key"])
    # workflow/system 是系统伪发送者，不是真实角色；即使历史数据里有同名 role 行，
    # 也不允许通过 @ 唤醒它。
    pseudo_targets = {"workflow", "system"}
    if target != "human":
        target_role = services.roles.get(conn, target)
        wf = services.workflow.get_for_task(conn, tid)
        # 唤醒范围不只 task_assignees：单负责人 assignee、workflow 步骤角色、
        # 以及被明确 @ 的现有角色都算“可拉入”，否则子任务/工作流角色会漏唤醒。
        bound = {x["role_slug"] for x in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (tid,))}
        if task.get("assignee"):
            bound.add(task["assignee"])
        if task.get("planner"):
            bound.add(task["planner"])
        if wf is not None:
            bound.update(str(s.get("role_slug")) for s in (wf.get("steps") or [])
                         if s.get("role_slug"))
        is_planner = (
            target == services.roles.ONBOARDING_SLUG
            or (bool(task.get("planner")) and target == task.get("planner")))
        # Cindy（内置 onboarding 助理）即使没有 roles 行也应可被 @ 触发重规划；
        # workflow/system 永远不作为可唤醒目标。
        target_exists = (
            target not in pseudo_targets
            and (target == services.roles.ONBOARDING_SLUG
                 or (target_role is not None
                     and bool(target_role.get("enabled", True))))
        )
        sender = auth.role or "human"
        if not target_exists:
            pass  # 角色不存在/已禁用：只记录 mention，不唤醒
        elif is_planner and task.get("status") not in ("done", "closed"):
            # 自然人或其它角色 @planner/Cindy：首次规划阶段先澄清；已有 workflow 的
            # 任务仍走原来的规划/修订判断。
            clarified = _clarify_if_needed(
                conn, request, tid, target, _assignee_slugs(conn, tid),
                human_message=f"{sender} 通过 @{target} 指示：\n{message}",
                sender=sender)
            if clarified is None:
                # 由 planner 自己判断如何处理，不是系统替人决策。
                try:
                    services.planning.start_planner_run(
                        conn,
                        request.app.state.db_path,
                        getattr(request.app.state, "orchestrator", None),
                        tid,
                        target,
                        assignees=_assignee_slugs(conn, tid),
                        extra_constraint=(
                            f"{sender} 通过 @{target} 指示：\n{message}\n\n"
                            "请按你的规划职责判断下一步：能用增量修订表达时先用 "
                            "`anvil workflow revise --preview` 判断并提交修订草案；"
                            "否则提交/修订完整 workflow 草案；不需要时直接回复并说明理由。"
                            "角色分配只能由 planner 在 workflow/loop 模板中定义。"),
                        replace_active=True,
                    )
                    events.publish(
                        "task", {"id": tid, "kind": "plan", "planner": target})
                except Exception:  # noqa: BLE001 —— 规划失败不阻断已记录的 mention
                    pass
        else:
            _wake_mentioned_role(
                conn, request, task, target, message,
                auth.role or "human", wf,
                source_slice_key=source_ctx["slice_key"],
                source_run_id=source_ctx["run_id"],
                source_step_id=source_ctx["step_id"],
                explicit_mode=body.get("context_mode"))
    events.publish("task", {"id": tid, "kind": "mention", "target": target})
    return result


def _held_response(conn, tid: str, auth: Auth, body: dict):
    """Held Draft（简化版）：Agent 发送时若基于旧版本，返回 409 + 期间新内容。

    人类消息不参与；`force=true` 表示知情越过。
    """
    if not auth.role or auth.role == "human" or body.get("force"):
        return None
    expect = body.get("expect_activity_id")
    if expect is None:
        return None
    try:
        expect = int(expect)
    except (TypeError, ValueError):
        return None
    latest = conn.execute(
        "SELECT COALESCE(MAX(id),0) AS m FROM activities WHERE task_id=?", (tid,)
    ).fetchone()["m"]
    if latest <= expect:
        return None
    from ..services import agent_inbox
    rows = conn.execute(
        "SELECT id, role_slug, kind, payload_json, created_at FROM activities "
        "WHERE task_id=? AND id>? ORDER BY id LIMIT 3", (tid, expect)
    ).fetchall()
    return JSONResponse(status_code=409, content={
        "detail": "held",
        "latest_activity_id": latest,
        "items": [{"id": r["id"], "role": r["role_slug"] or "system", "kind": r["kind"],
                   "text": agent_inbox._text(r)} for r in rows],
    })


def _strip_leading_mention(conn, message: str, target: str) -> str:
    """消息开头的 @target / @显示名 去掉（时间线用独立标签展示，避免重复）。"""
    names = [target]
    row = conn.execute("SELECT display_name FROM roles WHERE slug=?", (target,)).fetchone()
    if row and row["display_name"]:
        names.append(row["display_name"])
    for name in names:
        m = re.match(rf"^@\s*{re.escape(name)}\s*[，,:：]?\s*", message, flags=re.IGNORECASE)
        if m:
            return message[m.end():]
    return message


def _assignee_slugs(conn, tid: str) -> list[str]:
    return [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at", (tid,))]


def _clarify_if_needed(conn, request: Request, tid: str, planner: str,
                       assignees: list[str], *, human_message: str | None = None,
                       sender: str = "human", reason: str | None = None):
    """首次规划阶段优先进入澄清；返回响应 dict，或 None 表示继续规划。"""
    task = services.tasks.get(conn, tid)
    if task is None or not services.clarification.can_clarify(
            conn, tid, planner=planner):
        return None
    status = str(task.get("clarification_status") or "none")
    if status in ("none", "stale"):
        services.clarification.start(
            conn, tid, planner=planner, actor=sender, reason=reason)
    elif status == "in_progress":
        if human_message is None:
            return {
                "mode": "clarifying",
                "planner": planner,
                "clarification": services.clarification.get_state(conn, tid),
            }
    else:
        return None
    run = services.clarification.start_clarify_run(
        conn, request.app.state.db_path,
        getattr(request.app.state, "orchestrator", None),
        tid, planner, assignees=assignees, human_message=human_message,
        replace_active=True, reason=reason)
    state = services.clarification.get_state(conn, tid)
    events.publish(
        "task", {"id": tid, "kind": "clarify", "round": state["round"]})
    return {"mode": "clarifying", "planner": planner,
            "clarification": state, "run": run}


def _clarification_planner(conn, task: dict) -> str:
    planner = str(task.get("planner") or "")
    if planner:
        return planner
    planner = services.roles.ensure_onboarding(conn)["slug"]
    conn.execute(
        "UPDATE tasks SET planner=?, updated_at=? WHERE id=?",
        (planner, now_iso(), task["id"]))
    conn.commit()
    return planner


def _start_planner_run(conn, request: Request, tid: str, planner: str,
                       assignees: list[str], extra_constraint: str = "",
                       replace_active: bool = False):
    """唤醒规划者（onboarding 助理 / 任务 planner）生成草案。"""
    run = services.planning.start_planner_run(
        conn,
        request.app.state.db_path,
        getattr(request.app.state, "orchestrator", None),
        tid,
        planner,
        assignees=assignees,
        extra_constraint=extra_constraint,
        replace_active=replace_active,
    )
    events.publish("task", {"id": tid, "kind": "plan", "planner": planner})
    return run


@router.post("/{tid}/plan", status_code=201)
def plan_task(tid: str, request: Request, conn=Depends(get_db),
              auth: Auth = Depends(require_human)):
    """P1-0 计划先行：唤醒 planner 角色生成工作流草案（只拉起 planner，其余角色等批准后按依赖自动派发）。"""
    _assert_session_task(conn, auth, tid)
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = t.get("planner")
    if not planner:
        # plan_first 任务未显式设置 planner：回退到 onboarding 助理（Cindy），
        # 与 /dispatch 的选择逻辑保持一致；回退结果落库，后续按钮不再禁用。
        if os.environ.get("ANVIL_ONBOARDING", "1") != "0":
            planner = services.roles.ensure_onboarding(conn)["slug"]
            conn.execute(
                "UPDATE tasks SET planner=?, updated_at=? WHERE id=?",
                (planner, now_iso(), tid))
            conn.commit()
        else:
            raise HTTPException(
                status_code=409,
                detail="task has no planner and onboarding is disabled",
            )
    if services.roles.get(conn, planner) is None:
        raise HTTPException(status_code=409, detail=f"planner {planner} not found")
    assignees = _assignee_slugs(conn, tid)
    # onboarding 助理（Cindy）不是任务参与角色，但可以为任意任务做规划。
    if planner not in assignees and not services.roles.is_onboarding(conn, planner):
        raise HTTPException(status_code=409, detail=f"planner {planner} is not a task assignee")
    clarified = _clarify_if_needed(conn, request, tid, planner, assignees)
    if clarified:
        return clarified
    return _start_planner_run(conn, request, tid, planner, assignees)


@router.post("/{tid}/dispatch", status_code=201)
def dispatch_task(tid: str, request: Request, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    """派发任务（owner 2026-10-19 计划先行默认化）。

    - 多角色任务 → 先唤醒 onboarding 助理（Cindy）分析任务并设计工作流；
      草案进入人工审批（收件箱），批准后子任务按依赖自动派发。
    - 单角色 / 关闭 onboarding → 直接唤醒参与角色。
    - 已有待审草案 → 返回 pending_review（不重复规划）。
    """
    import os

    _assert_session_task(conn, auth, tid)
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    assignees = _assignee_slugs(conn, tid)
    for role in assignees:
        role_row = services.roles.get(conn, role)
        if role_row is None:
            if services.roles.is_deleted(conn, role):
                raise HTTPException(
                    status_code=409, detail=f"role {role} has been deleted")
            # 兼容历史/测试中的未建角色引用；已删除角色由快照明确拦截。
            continue
        if not role_row.get("enabled", True):
            raise HTTPException(
                status_code=409, detail=f"role {role} is disabled")
    pending = conn.execute(
        "SELECT id, kind FROM board_drafts WHERE source_task=? AND status='pending'", (tid,)
    ).fetchone()
    if pending:
        return {"mode": "pending_review", "draft_id": pending["id"],
                "draft_kind": pending["kind"] or "tasks"}

    onboarding_enabled = os.environ.get("ANVIL_ONBOARDING", "1") != "0"
    running_workflow = conn.execute(
        "SELECT 1 FROM workflows WHERE task_id=? AND status='running' LIMIT 1", (tid,)
    ).fetchone()
    force_board_plan = bool(t.get("issue_board_enabled")) and running_workflow is None
    planner = None
    if force_board_plan:
        # 问题板必须有 reviewer 与人审环节，单角色任务也必须先由 planner 指定。
        planner = t.get("planner") or (
            services.roles.ensure_onboarding(conn)["slug"] if onboarding_enabled else None)
        if not planner:
            raise HTTPException(
                status_code=409,
                detail="issue board requires onboarding/planner; enable onboarding or set a planner",
            )
        if services.roles.get(conn, planner) is None:
            raise HTTPException(status_code=409, detail=f"planner {planner} not found")
    elif t.get("plan_first"):
        # plan_first 是硬语义：无论单角色/多角色，都先由 planner（缺省 Cindy）规划，
        # 不允许因为知识绑定状态而退化成直接派发。
        planner = t.get("planner") or (
            services.roles.ensure_onboarding(conn)["slug"] if onboarding_enabled else None)
        if not planner:
            raise HTTPException(
                status_code=409,
                detail="plan_first requires onboarding/planner; enable onboarding or set a planner",
            )
        if services.roles.get(conn, planner) is None:
            raise HTTPException(status_code=409, detail=f"planner {planner} not found")
        if not t.get("planner"):
            conn.execute(
                "UPDATE tasks SET planner=?, updated_at=? WHERE id=?",
                (planner, now_iso(), tid))
            conn.commit()
    elif not assignees and onboarding_enabled:
        # 没有绑定角色：Cindy 先评估并给出角色建议。
        planner = services.roles.ensure_onboarding(conn)["slug"]
    elif len(assignees) >= 2 and onboarding_enabled:
        planner = services.roles.ensure_onboarding(conn)["slug"]
    else:
        # 无 tags（含单角色直派）或确定性匹配只是弱建议：让 Cindy 做一次
        # 轻量知识路由，生成 tags + knowledge_repos；草案仍由人工批准。
        cindy_mode = str(getattr(config, "KNOWLEDGE_CINDY_MODE", "tagless")).lower()
        tagless = not (t.get("tags") or [])
        weak = t.get("knowledge_binding_state") in (
            "suggested", "needs_cindy", "fallback")
        if (onboarding_enabled and cindy_mode != "off"
                and getattr(config, "KNOWLEDGE_REPOS_ENABLED", True)
                and str(getattr(config, "KNOWLEDGE_BINDING_MODE", "auto")).lower() != "off"
                and (cindy_mode == "always" or tagless or weak)):
            planner = services.roles.ensure_onboarding(conn)["slug"]

    if not assignees and planner is None:
        raise HTTPException(
            status_code=409,
            detail="task has no assignees and onboarding is disabled; bind at least one role",
        )

    if planner:
        if planner not in assignees and not services.roles.is_onboarding(conn, planner):
            raise HTTPException(status_code=409, detail=f"planner {planner} is not a task assignee")
        clarified = _clarify_if_needed(conn, request, tid, planner, assignees)
        if clarified:
            return clarified
        run = _start_planner_run(conn, request, tid, planner, assignees)
        return {"mode": "planning", "planner": planner, "run": run}

    runs = []
    orch = getattr(request.app.state, "orchestrator", None)
    for role in assignees:
        existing = services.runs.find_active_run(conn, tid, role, slice_key=None)
        run = existing or services.runs.create_run(conn, role, tid)
        if not existing and orch:
            try:
                orch.pump(request.app.state.db_path, run["id"], "wake", {})
            except Exception:  # noqa: BLE001
                pass
        runs.append(run)
    return {"mode": "direct", "runs": runs}


@router.get("/{tid}/clarification")
def get_clarification(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """读取任务简报与澄清状态。"""
    _assert_session_task(conn, auth, tid)
    try:
        return services.clarification.get_state(conn, tid)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.post("/{tid}/clarification/start", status_code=201)
def start_clarification(tid: str, request: Request, body: dict | None = None,
                        conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """human 主动开始/重新开始首次规划前的澄清。"""
    _assert_session_task(conn, auth, tid)
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = _clarification_planner(conn, task)
    reason = str((body or {}).get("reason") or "") or None
    try:
        services.clarification.start(
            conn, tid, planner=planner, actor="human", reason=reason)
        run = services.clarification.start_clarify_run(
            conn, request.app.state.db_path,
            getattr(request.app.state, "orchestrator", None),
            tid, planner, assignees=_assignee_slugs(conn, tid), reason=reason)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    events.publish("task", {"id": tid, "kind": "clarify"})
    return {"clarification": services.clarification.get_state(conn, tid),
            "run": run}


@router.put("/{tid}/clarification/draft")
def save_clarification_draft(tid: str, body: dict, conn=Depends(get_db),
                             auth: Auth = Depends(get_auth)):
    """Cindy 通过 CLI 更新简报草稿；human 也可代写。"""
    _assert_session_task(conn, auth, tid)
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if auth.role != "human":
        planner = str(task.get("planner") or "")
        if not (auth.role == planner
                or services.roles.is_onboarding(conn, auth.role or "")):
            raise HTTPException(
                status_code=403,
                detail="only the task planner can write the brief draft")
    brief = body.get("brief")
    if not isinstance(brief, dict):
        raise HTTPException(status_code=422, detail="brief is required")
    try:
        state = services.clarification.save_draft(
            conn, tid, brief, actor=auth.role or "cindy")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    events.publish("task", {"id": tid, "kind": "clarification_brief"})
    return state


@router.post("/{tid}/clarification/confirm")
def confirm_clarification(tid: str, body: dict, request: Request,
                          conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """human 确认简报：写版本/强度，自动启动 planner run。"""
    _assert_session_task(conn, auth, tid)
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = _clarification_planner(conn, task)
    try:
        services.clarification.confirm(
            conn, tid, brief=body.get("brief"), weight=body.get("weight"),
            plan_shape=body.get("plan_shape"),
            acknowledge_open_questions=bool(
                body.get("acknowledge_open_questions")),
            actor="human", reason=body.get("reason"))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    run = _start_planner_run(
        conn, request, tid, planner, _assignee_slugs(conn, tid))
    return {"clarification": services.clarification.get_state(conn, tid),
            "run": run}


@router.post("/{tid}/clarification/skip")
def skip_clarification(tid: str, request: Request, body: dict | None = None,
                       conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """human 跳过澄清：按今天的行为启动 planner（无简报）。"""
    _assert_session_task(conn, auth, tid)
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = _clarification_planner(conn, task)
    try:
        services.clarification.skip(
            conn, tid, actor="human",
            reason=str((body or {}).get("reason") or "") or None)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    run = _start_planner_run(
        conn, request, tid, planner, _assignee_slugs(conn, tid))
    return {"clarification": services.clarification.get_state(conn, tid),
            "run": run}


@router.put("/{tid}/clarification/brief")
def edit_clarification_brief(tid: str, body: dict, conn=Depends(get_db),
                             auth: Auth = Depends(require_human)):
    """human 编辑已确认简报；有活跃 planner run / pending 草案时拒绝。"""
    _assert_session_task(conn, auth, tid)
    brief = body.get("brief")
    if not isinstance(brief, dict):
        raise HTTPException(status_code=422, detail="brief is required")
    try:
        state = services.clarification.update_brief(
            conn, tid, brief, actor="human", reason=body.get("reason"))
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return state


@router.get("/{tid}/clarification/versions")
def clarification_versions(tid: str, conn=Depends(get_db),
                           auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return services.clarification.versions(conn, tid)


@router.post("/{tid}/deps", response_model=TaskOut)
def add_deps(tid: str, body: DepIn, conn=Depends(get_db),
             auth: Auth = Depends(get_auth)):
    t = services.tasks.add_dep(conn, tid, body.depends_on)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t


@router.post("/{tid}/dispatches", status_code=201)
def create_dispatch(tid: str, body: dict, request: Request, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    """创建一张来源 task 下的**派遣子任务**（不是 delegation，也不是顶层任务）。

    与现有 `POST /{tid}/dispatch`（启动/规划）区分：这里的目标角色在全新上下文
    工作，来源 task 只保留软关联，不阻塞、不共享对话。
    """
    _assert_session_task(conn, auth, tid)
    if body.get("repo") or body.get("workspace_id") or body.get("base_branch"):
        raise HTTPException(
            status_code=422,
            detail=("dispatch inherits the source task's repo/workspace/base_branch; "
                    "remove repo/workspace_id/base_branch from the request"))
    target_role = str(body.get("target_role") or "").strip()
    message = str(body.get("message") or "").strip()
    if not target_role or not message:
        raise HTTPException(status_code=422, detail="target_role and message are required")
    source_run_id = body.get("source_run_id") or auth.run_id
    source_slice_key = str(body.get("slice_key") or "").strip() or None
    source_step_id = str(body.get("source_step_id") or "").strip() or None
    if source_run_id:
        run = conn.execute(
            "SELECT task_id, slice_key, step_id FROM runs WHERE id=?",
            (source_run_id,)).fetchone()
        if run is None or run["task_id"] != tid:
            source_run_id = None
        else:
            source_slice_key = source_slice_key or run["slice_key"]
            source_step_id = source_step_id or run["step_id"]
    requester = ("human" if auth.role in (None, "human") or auth.test_bypass
                 else auth.role)
    try:
        result = services.dispatch.create_dispatch(
            conn, source_task_id=tid, target_role=target_role, message=message,
            requester=requester,
            title=body.get("title"),
            acceptance=[str(x) for x in (body.get("acceptance") or [])],
            scope_paths=[str(x) for x in (body.get("scope_paths") or [])],
            source_slice_key=source_slice_key,
            source_step_id=source_step_id,
            source_run_id=source_run_id,
            force=bool(body.get("force")),
            reason_code="explicit_mention" if source_run_id else "explicit",
            reason_json={"explicit_mode": body.get("context_mode") or "dispatch"},
        )
    except services.dispatch.DispatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    run = result.get("run")
    orch = getattr(request.app.state, "orchestrator", None)
    if orch and run and not result.get("reused"):
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    events.publish("task", {"id": tid, "kind": "dispatch"})
    return result


@router.get("/{tid}/dispatches", response_model=list[TaskDispatchOut])
def list_dispatches(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    return services.dispatch.list_dispatches(conn, tid)
