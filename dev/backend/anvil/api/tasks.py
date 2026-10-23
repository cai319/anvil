"""Task endpoints: CRUD + manual assign + agent-side transitions (§3.1/§10)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import ClaimIn, CommentIn, DepIn, ReviewIn, SubmitIn, TaskIn, TaskOut, TaskPatch
from ..services.tasks import ConflictError
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
    if auth.role:
        body.created_by = ("human" if auth.role == "human" else f"agent:{auth.role}")
    try:
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


@router.get("", response_model=list[TaskOut])
def list_tasks(status: str | None = Query(default=None), assignee: str | None = Query(default=None),
               workspace_id: str | None = Query(default=None), type: str | None = Query(default=None),
               tag: str | None = Query(default=None),
               parent_id: str | None = Query(default=None), conn=Depends(get_db)):
    return services.tasks.list_all(conn, status=status, assignee=assignee,
                                   workspace_id=workspace_id, type=type, tag=tag,
                                   parent_id=parent_id)


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
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/reopen", response_model=TaskOut)
def reopen_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        result = services.tasks.reopen(conn, tid, auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.patch("/{tid}", response_model=TaskOut)
def patch_task(tid: str, body: TaskPatch, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    t = services.tasks.update(conn, tid, **body.model_dump(exclude_none=True))
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t


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
    events.publish("task", {"id": tid, "status": result["status"]})
    return result


@router.post("/{tid}/heartbeat", response_model=TaskOut)
def heartbeat_task(tid: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    _assert_session_task(conn, auth, tid)
    try:
        return services.tasks.heartbeat(conn, tid, auth.role)
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
    if wf is not None:
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
            or (target == (task.get("planner") or "") and target not in bound
                and wf is not None))
        # Cindy（内置 onboarding 助理）即使没有 roles 行也应可被 @ 触发重规划。
        target_exists = (
            target == services.roles.ONBOARDING_SLUG
            or (target_role is not None and bool(target_role.get("enabled", True)))
        )
        if not target_exists:
            pass  # 角色不存在/已禁用：只记录 mention，不唤醒
        elif is_planner and task.get("status") not in ("done", "closed"):
            # planner/Cindy 被 @：走重规划 run（workflow 结构只能由 planner 调整）。
            try:
                services.planning.start_planner_run(
                    conn,
                    request.app.state.db_path,
                    getattr(request.app.state, "orchestrator", None),
                    tid,
                    target,
                    assignees=_assignee_slugs(conn, tid),
                    extra_constraint=(
                        f"{auth.role or 'human'} 通过 @{target} 提出 workflow/loop "
                        f"调整诉求：\n{message}\n\n"
                        "请重新审阅当前 workflow，必要时提交修订后的 workflow 草案；"
                        "角色分配只能由 planner 在 workflow/loop 模板中定义，"
                        "不要让执行角色在切片计划里覆盖。"),
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
