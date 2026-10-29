"""Task endpoints: CRUD + manual assign + agent-side transitions (§3.1/§10)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import config, services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import ClaimIn, CommentIn, DepIn, ReviewIn, SubmitIn, TaskIn, TaskOut, TaskPatch
from ..services import repo_catalog
from ..services.tasks import ConflictError
from ..util import now_iso
from . import events

router = APIRouter()


def _assert_session_task(conn, auth: Auth, tid: str) -> None:
    """运行级会话只能操作自己绑定/参与的任务。

    - task 绑定 token：必须与 URL 任务一致；
    - 角色级常驻 token：任务已有绑定角色时，只允许 assignee/lead/planner/onboarding；
      无绑定角色的开放任务仍允许任意角色 claim。
    """
    if auth.task_id and auth.task_id != tid:
        raise HTTPException(status_code=403, detail="session is bound to another task")
    if auth.role and auth.role != "human" and auth.task_id is None:
        t = services.tasks.get(conn, tid)
        if t is not None:
            # 仅团队任务按 Roster 限制；单负责人/开放任务交给 claim 的排他逻辑返回 409。
            bound = set(t.get("assignees") or [])
            if bound:
                if t.get("assignee"):
                    bound.add(t["assignee"])
                if t.get("planner"):
                    bound.add(t["planner"])
                if auth.role not in bound and not services.roles.is_onboarding(conn, auth.role):
                    raise HTTPException(status_code=403, detail="role is not a participant of this task")


def _pump_after_submit(request: Request, conn, tid: str) -> None:
    """任务 submit 成功后 pump 运行图（run running→submitted→reviewing→interrupt）。"""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return
    active = services.runs.find_active_run(conn, tid)
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
    active = services.runs.find_active_run(conn, tid)
    if active and active["status"] == "reviewing":
        try:
            orch.resume_review(request.app.state.db_path, active["id"], approve)
        except Exception:  # noqa: BLE001
            pass


@router.post("", status_code=201, response_model=TaskOut)
def create_task(body: TaskIn, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if is_real_agent:
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
    if auth.role:
        body.created_by = ("human" if auth.role == "human" else f"agent:{auth.role}")
    repos = [r.model_dump() if hasattr(r, "model_dump") else r
             for r in (body.knowledge_repos or [])]
    try:
        task = services.tasks.create(
        conn, id=body.id, title=body.title, type=body.type, priority=body.priority,
        tags=body.tags, repo=body.repo, workspace_id=body.workspace_id, base_branch=body.base_branch,
        scope_paths=body.scope_paths, description=body.description,
        acceptance=body.acceptance, assignee=body.assignee, assignees=body.assignees,
        plan_first=body.plan_first, planner=body.planner, plan_seed=body.plan_seed,
        created_by=body.created_by,
        parent_id=body.parent_id, depends_on=body.depends_on,
        knowledge_repos=repos or None,
        knowledge_repos_source="human" if auth.role in (None, "human") else "agent",
        )
        if is_real_agent:
            services.tasks.audit_create(
                conn, task["id"], auth.role,
                parent_id=body.parent_id,
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
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
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

    result = (services.tasks.add_assignees(conn, tid, roles) if existing
              else services.tasks.assign_roles(conn, tid, roles))
    summary = "、".join(roles)
    from ..util import jdumps, now_iso
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (tid, agent, "comment",
         jdumps({"body": (f"在现有 Roster 上追加角色：{summary}。" if existing
                          else f"基于现有角色库绑定任务 Roster：{summary}。")}),
         now_iso()),
    )
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (agent, tid, "task.assign_existing",
         jdumps({"roles": roles, "added": added, "existing": existing,
                 "planner": planner}), now_iso()),
    )
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
               conn=Depends(get_db)):
    return services.tasks.list_all(conn, status=status, assignee=assignee,
                                   workspace_id=workspace_id, type=type, tag=tag,
                                   parent_id=parent_id,
                                   include_archived=include_archived,
                                   archived_only=archived_only)


@router.get("/{tid}", response_model=TaskOut)
def get_task(tid: str, conn=Depends(get_db)):
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t


@router.get("/{tid}/knowledge")
def list_task_knowledge(tid: str, include_subtasks: bool = False, deep: bool = False,
                        conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    task = services.tasks.get(conn, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if is_real_agent:
        allowed = (
            services.tasks.is_task_participant(conn, tid, auth.role)
            or task.get("planner") == auth.role
            or services.roles.is_onboarding(conn, auth.role)
        )
        if not allowed:
            raise HTTPException(
                status_code=403, detail="agent is not a participant of this task")
    try:
        return services.task_knowledge.list_for_task(
            conn, tid, include_subtasks=include_subtasks, deep=deep)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.post("/{tid}/knowledge", status_code=201)
def create_task_knowledge(tid: str, body: dict, conn=Depends(get_db),
                          auth: Auth = Depends(get_auth)):
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    if is_real_agent and not services.tasks.is_task_participant(conn, tid, auth.role):
        raise HTTPException(status_code=403, detail="agent is not a participant of this task")
    try:
        return services.task_knowledge.create(
            conn, tid,
            title=str(body.get("title") or "").strip(),
            tldr=str(body.get("tldr") or "").strip(),
            kind=str(body.get("kind") or ""),
            fields=body.get("fields") or {},
            tags=body.get("tags") or [],
            source_role=auth.role,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/{tid}/knowledge/{kid}", status_code=204)
def delete_task_knowledge(tid: str, kid: str, conn=Depends(get_db),
                          auth: Auth = Depends(require_human)):
    if not services.task_knowledge.delete(conn, kid, task_id=tid):
        raise HTTPException(status_code=404, detail="knowledge not found")
    return None


@router.get("/{tid}/timeline")
def task_timeline(tid: str, conn=Depends(get_db)):
    """任务会话时间线：多角色交互流（认领/提交/评审/评论/运行生命周期）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return services.tasks.timeline(conn, tid)


@router.get("/{tid}/activities")
def task_activities(tid: str, conn=Depends(get_db)):
    """任务活动事件（评论 / @提及 / run 迁移）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return services.tasks.list_activities(conn, tid)


@router.get("/{tid}/diff")
def task_diff(tid: str, conn=Depends(get_db)):
    """任务当前改动 + 越界文件（前端 diff 折叠区）。"""
    try:
        return services.tasks.task_diff(conn, tid)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.get("/{tid}/config-snapshot")
def task_config_snapshot(tid: str, conn=Depends(get_db)):
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return services.tasks.task_config_snapshot(conn, tid)


@router.get("/{tid}/bids")
def task_bids(tid: str, conn=Depends(get_db)):
    """认领出价记录（score 策略为 v2；先到先得模式下为空）。"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
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
def patch_task(tid: str, body: TaskPatch, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    fields = body.model_dump(exclude_none=True)
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
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("human", tid, "task.knowledge_suggestion_accept", "{}", now_iso()))
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
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("human", tid, "task.knowledge_suggestion_dismiss", "{}", now_iso()))
    conn.commit()
    return _knowledge_repo_payload(conn, services.tasks.get(conn, tid))


@router.post("/{tid}/claim", response_model=TaskOut)
def claim_task(tid: str, body: ClaimIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
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
    team = services.tasks.is_team_task(conn, tid)
    try:
        result = services.tasks.submit(conn, tid, summary=body.summary, branch=body.branch,
                                       changed_paths=body.changed_paths, role=auth.role)
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
        result = services.tasks.review(conn, tid, body.approve, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _resume_after_review(request, conn, tid, body.approve)
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/comment", response_model=TaskOut)
def comment_task(tid: str, body: CommentIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    held = _held_response(conn, tid, auth, body.model_dump())
    if held is not None:
        return held
    try:
        result = services.tasks.comment(conn, tid, auth.role or "human", body.body)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "kind": "comment"})
    return result


def _wake_mentioned_role(conn, request: Request, task: dict, target: str,
                         message: str, sender: str, wf: dict | None) -> None:
    """唤醒被 @ 的角色：有活跃 run 则 resume；否则起 reply/normal run。

    - 工作流任务：起 `[anvil:reply]` run（只讨论，不改 workflow 结构）；
    - 普通任务/子任务：起普通 run，让被 @ 的角色可以直接 claim 执行。
    """
    orch = getattr(request.app.state, "orchestrator", None)
    active = services.runs.find_active_run(conn, task["id"], target)
    if active:
        if orch:
            try:
                orch.pump(request.app.state.db_path, active["id"], "resume", {})
            except Exception:  # noqa: BLE001
                pass
        return
    if wf is not None or target == services.roles.ONBOARDING_SLUG:
        # 工作流任务或 Cindy：起回复 run（只讨论，不 claim/改 workflow）。
        run = services.runs.create_run(
            conn, target, task["id"],
            prompt=("[anvil:reply]你在任务会话中被 "
                    f"{sender} @：\n{message}\n\n"
                    "请评估该反馈并回复你的判断与依据。"))
    else:
        run = services.runs.create_run(conn, target, task["id"])
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
    result = services.tasks.mention(conn, tid, auth.role or "human", target, message)
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
            # 自然人或其它角色 @planner/Cindy：由 planner 自己判断如何处理。
            # 这是“让有职责的角色处理”，不是系统替人决策。
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
                events.publish("task", {"id": tid, "kind": "plan", "planner": target})
            except Exception:  # noqa: BLE001 —— 规划失败不阻断已记录的 mention
                pass
        else:
            _wake_mentioned_role(conn, request, task, target, message,
                                 auth.role or "human", wf)
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
def plan_task(tid: str, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """P1-0 计划先行：唤醒 planner 角色生成工作流草案（只拉起 planner，其余角色等批准后按依赖自动派发）。"""
    _assert_session_task(conn, auth, tid)
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = t.get("planner")
    if not planner:
        raise HTTPException(status_code=409, detail="task has no planner; set planner first")
    if services.roles.get(conn, planner) is None:
        raise HTTPException(status_code=409, detail=f"planner {planner} not found")
    assignees = _assignee_slugs(conn, tid)
    # onboarding 助理（Cindy）不是任务参与角色，但可以为任意任务做规划。
    if planner not in assignees and not services.roles.is_onboarding(conn, planner):
        raise HTTPException(status_code=409, detail=f"planner {planner} is not a task assignee")
    return _start_planner_run(conn, request, tid, planner, assignees)


@router.post("/{tid}/dispatch", status_code=201)
def dispatch_task(tid: str, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
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
    pending = conn.execute(
        "SELECT id, kind FROM board_drafts WHERE source_task=? AND status='pending'", (tid,)
    ).fetchone()
    if pending:
        return {"mode": "pending_review", "draft_id": pending["id"],
                "draft_kind": pending["kind"] or "tasks"}

    onboarding_enabled = os.environ.get("ANVIL_ONBOARDING", "1") != "0"
    planner = None
    if t.get("plan_first") and t.get("planner"):
        planner = t["planner"]
        if services.roles.get(conn, planner) is None:
            raise HTTPException(status_code=409, detail=f"planner {planner} not found")
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
        run = _start_planner_run(conn, request, tid, planner, assignees)
        return {"mode": "planning", "planner": planner, "run": run}

    runs = []
    orch = getattr(request.app.state, "orchestrator", None)
    for role in assignees:
        existing = services.runs.find_active_run(conn, tid, role)
        run = existing or services.runs.create_run(conn, role, tid)
        if not existing and orch:
            try:
                orch.pump(request.app.state.db_path, run["id"], "wake", {})
            except Exception:  # noqa: BLE001
                pass
        runs.append(run)
    return {"mode": "direct", "runs": runs}


@router.post("/{tid}/deps", response_model=TaskOut)
def add_deps(tid: str, body: DepIn, conn=Depends(get_db),
             auth: Auth = Depends(get_auth)):
    t = services.tasks.add_dep(conn, tid, body.depends_on)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t
