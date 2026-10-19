"""Task endpoints: CRUD + manual assign + agent-side transitions (§3.1/§10)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import services
from ..deps import Auth, get_auth, get_db
from ..schemas import ClaimIn, CommentIn, DepIn, ReviewIn, SubmitIn, TaskIn, TaskOut, TaskPatch
from ..services.tasks import ConflictError
from . import events

router = APIRouter()


def _assert_session_task(auth: Auth, tid: str) -> None:
    """运行级会话只能操作自己被绑定的任务（design §10 末段）。"""
    if auth.task_id and auth.task_id != tid:
        raise HTTPException(status_code=403, detail="session is bound to another task")


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
def create_task(body: TaskIn, conn=Depends(get_db)):
    return services.tasks.create(
        conn, id=body.id, title=body.title, type=body.type, priority=body.priority,
        tags=body.tags, repo=body.repo, workspace_id=body.workspace_id, base_branch=body.base_branch,
        scope_paths=body.scope_paths, description=body.description,
        acceptance=body.acceptance, assignee=body.assignee, assignees=body.assignees,
        plan_first=body.plan_first, planner=body.planner,
        created_by=body.created_by,
        parent_id=body.parent_id, depends_on=body.depends_on,
        cost_budget_usd=body.cost_budget_usd,
    )


@router.post("/{tid}/assign", response_model=TaskOut)
def assign_roles(tid: str, body: dict, conn=Depends(get_db)):
    """绑定任务参与角色（团队协作，多角色）。body: {roles: [slug, ...]}"""
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    roles = [str(r) for r in (body.get("roles") or [])]
    result = services.tasks.assign_roles(conn, tid, roles)
    events.publish("task", {"id": tid, "kind": "assign"})
    return result


@router.get("", response_model=list[TaskOut])
def list_tasks(status: str | None = Query(default=None), assignee: str | None = Query(default=None),
               workspace_id: str | None = Query(default=None), type: str | None = Query(default=None),
               tag: str | None = Query(default=None), conn=Depends(get_db)):
    return services.tasks.list_all(conn, status=status, assignee=assignee,
                                   workspace_id=workspace_id, type=type, tag=tag)


@router.get("/{tid}", response_model=TaskOut)
def get_task(tid: str, conn=Depends(get_db)):
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t


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
                auth: Auth = Depends(get_auth)):
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
def cancel_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    try:
        result = services.tasks.cancel(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/reopen", response_model=TaskOut)
def reopen_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    try:
        result = services.tasks.reopen(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.patch("/{tid}", response_model=TaskOut)
def patch_task(tid: str, body: TaskPatch, conn=Depends(get_db)):
    t = services.tasks.update(conn, tid, **body.model_dump(exclude_none=True))
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t


@router.post("/{tid}/claim", response_model=TaskOut)
def claim_task(tid: str, body: ClaimIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(auth, tid)
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
    _assert_session_task(auth, tid)
    try:
        result = services.tasks.unclaim(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/heartbeat", response_model=TaskOut)
def heartbeat_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(auth, tid)
    try:
        return services.tasks.heartbeat(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


@router.post("/{tid}/submit", response_model=TaskOut)
def submit_task(tid: str, body: SubmitIn, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(auth, tid)
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
def review_task(tid: str, body: ReviewIn, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
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
    _assert_session_task(auth, tid)
    try:
        result = services.tasks.comment(conn, tid, auth.role or "human", body.body)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "kind": "comment"})
    return result


@router.post("/{tid}/mention", response_model=TaskOut)
def mention_task(tid: str, body: dict, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """@提及某角色，注入注意事项/上下文；@的是角色时唤醒该角色（raft inbox：@ 即拉入）。"""
    _assert_session_task(auth, tid)
    if services.tasks.get(conn, tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    target = str(body.get("target") or "human")
    message = str(body.get("message") or "")
    result = services.tasks.mention(conn, tid, auth.role or "human", target, message)
    # @的是角色（非 human）且是任务参与角色 → 唤醒该角色（挂起中则 resume 续跑，否则新起 run）
    if target != "human":
        roles = [x["role_slug"] for x in conn.execute(
            "SELECT role_slug FROM task_assignees WHERE task_id=?", (tid,)
        )]
        if target in roles:
            orch = getattr(request.app.state, "orchestrator", None)
            active = services.runs.find_active_run(conn, tid, target)
            if active:
                if orch:
                    try:
                        orch.pump(request.app.state.db_path, active["id"], "resume", {})
                    except Exception:  # noqa: BLE001
                        pass
            else:
                # 工作流任务：空闲角色被 @ → 起一条「回复」run（只讨论，不 claim/submit 任务）
                wf = services.workflow.get_for_task(conn, tid)
                if wf is not None:
                    run = services.runs.create_run(
                        conn, target, tid,
                        prompt=("[anvil:reply]你在任务会话中被 "
                                f"{auth.role or 'human'} @：\n{message}\n\n"
                                "请评估该反馈并回复你的判断与依据。"))
                else:
                    run = services.runs.create_run(conn, target, tid)
                if orch:
                    try:
                        orch.pump(request.app.state.db_path, run["id"], "wake", {})
                    except Exception:  # noqa: BLE001
                        pass
    events.publish("task", {"id": tid, "kind": "mention", "target": target})
    return result


def _assignee_slugs(conn, tid: str) -> list[str]:
    return [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at", (tid,))]


def _start_planner_run(conn, request: Request, tid: str, planner: str,
                       assignees: list[str]):
    """唤醒规划者（onboarding 助理 / 任务 planner）生成工作流草案。"""
    from ..graph.render import PLANNING_PROTOCOL
    from ..util import jloads

    lines = []
    for slug in assignees:
        row = conn.execute("SELECT display_name, definition_json FROM roles WHERE slug=?",
                           (slug,)).fetchone()
        if row is None:
            lines.append(f"- {slug}（@提及：anvil task mention {tid} --to {slug} \"消息\"）")
            continue
        definition = jloads(row["definition_json"], {})
        desc = definition.get("description") or ""
        resp = "、".join(definition.get("responsibilities") or [])
        detail = "；".join(x for x in (desc, f"职责：{resp}" if resp else "") if x)
        lines.append(
            f"- {row['display_name']}（slug={slug}，@提及：anvil task mention {tid} --to {slug} \"消息\"）"
            + (f"：{detail}" if detail else ""))
    prompt = (PLANNING_PROTOCOL + "\n\n## 参与角色（Roster）\n" + "\n".join(lines))
    run = services.runs.create_run(conn, planner, tid, prompt=prompt)
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    events.publish("task", {"id": tid, "kind": "plan", "planner": planner})
    return run


@router.post("/{tid}/plan", status_code=201)
def plan_task(tid: str, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """P1-0 计划先行：唤醒 planner 角色生成工作流草案（只拉起 planner，其余角色等批准后按依赖自动派发）。"""
    _assert_session_task(auth, tid)
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = t.get("planner")
    if not planner:
        raise HTTPException(status_code=409, detail="task has no planner; set planner first")
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

    _assert_session_task(auth, tid)
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    assignees = _assignee_slugs(conn, tid)
    if not assignees:
        raise HTTPException(status_code=409, detail="task has no assignees")

    pending = conn.execute(
        "SELECT id FROM board_drafts WHERE source_task=? AND status='pending'", (tid,)
    ).fetchone()
    if pending:
        return {"mode": "pending_review", "draft_id": pending["id"]}

    planner = None
    if t.get("plan_first") and t.get("planner"):
        planner = t["planner"]
    elif len(assignees) >= 2 and os.environ.get("ANVIL_ONBOARDING", "1") != "0":
        onboarding = services.roles.ensure_onboarding(conn)
        planner = onboarding["slug"]

    if planner:
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
def add_deps(tid: str, body: DepIn, conn=Depends(get_db)):
    t = services.tasks.add_dep(conn, tid, body.depends_on)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t
