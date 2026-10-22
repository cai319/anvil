"""Agent self-service endpoints: memory / notes / proposals / lessons (§3.2/§3.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import config, services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import InstructionIn, LessonIn, MemoryAppendIn, NoteWriteIn, ProposalIn
from ..util import ConflictError, jdumps, now_iso
from . import events

router = APIRouter()


@router.get("/me")
def get_me(conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """anvil self show — 返回自身身份 + 并发额度 + 已用成本（SE §3.3）。"""
    from ..util import jloads

    role_slug = auth.role or "qa-role"
    cost = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM cost_ledger WHERE role_slug=?", (role_slug,)
    ).fetchone()[0]
    profile: dict = {}
    role = services.roles.get(conn, role_slug)
    if role:
        profile = jloads(role["definition_json"], {})
    concurrency = profile.get("concurrency", {})
    mgr = services.daemon.get_manager()
    st = mgr.status(role_slug) if mgr and mgr.enabled else {}
    window = int(st.get("context_window") or config.DEFAULT_CONTEXT_WINDOW)
    cic_threshold = int(st.get("cic_threshold_tokens") or (
        config.CIC_THRESHOLD_TOKENS if config.CIC_THRESHOLD_TOKENS > 0
        else int(window * config.CIC_RESET_RATIO)))
    return {
        "role_slug": role_slug,
        "profile": profile,
        "concurrency_quota": concurrency,
        "cost_used_usd": cost,
        # 供角色自主判断是否 reset-context：上下文占用 + 队列/当前 run + 模型窗口。
        "session": {
            "context_tokens": int(st.get("context_tokens") or 0),
            "queue_len": int(st.get("queue_len") or 0),
            "current_run_id": st.get("current_run_id"),
            "current_task_id": st.get("current_task_id"),
            "model_id": st.get("model_id"),
            "served_model_id": st.get("served_model_id"),
            "model_pool": st.get("model_pool") or [],
            "context_window_floor": st.get("context_window_floor"),
            "active_context_window": int(st.get("active_context_window") or window),
            "context_window": window,
            "context_fit": bool(st.get("context_fit", True)),
            "context_safe_fit": bool(st.get("context_safe_fit", True)),
            "model_pool_windows": st.get("model_pool_windows") or [],
            "cic_threshold_tokens": cic_threshold,
            "hint_tokens": int(st.get("hint_tokens") or int(window * config.CONTEXT_HINT_RATIO)),
            "hint_ratio": config.CONTEXT_HINT_RATIO,
        },
    }


@router.post("/me/session/reset")
def reset_my_session(body: dict, request: Request, conn=Depends(get_db),
                     auth: Auth = Depends(get_auth)):
    """角色自主决定：丢弃当前会话上下文，用新会话重投当前 run。

    这是 agent-controlled 的上下文管理入口：角色在唤醒时看到上下文占用/任务切换提示后，
    自行判断是否调用。硬护栏由 daemon 提供（当前 run 匹配、无排队消息、进程存活）。
    """
    role = auth.role
    if not role or role == "human":
        raise HTTPException(status_code=403, detail="agent session required")
    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id is required")
    if auth.task_id and auth.task_id != task_id:
        raise HTTPException(status_code=403, detail="session is bound to another task")

    run = services.runs.find_active_run(conn, task_id, role)
    if run is None:
        raise HTTPException(status_code=409, detail="no active run for this role/task")
    mgr = services.daemon.get_manager()
    if mgr is None or not mgr.enabled:
        raise HTTPException(status_code=409, detail="persistent runtime is not enabled")
    st = mgr.status(role)
    if not st.get("alive"):
        raise HTTPException(status_code=409, detail="role process is not alive")
    if st.get("current_run_id") != run["id"]:
        raise HTTPException(status_code=409, detail="role is not currently processing this run")
    if st.get("current_task_id") not in (None, task_id):
        raise HTTPException(status_code=409, detail="role is busy with another task")
    if st.get("queue_len"):
        raise HTTPException(
            status_code=409,
            detail="role has queued messages; wait until they are processed before resetting")

    tokens = st.get("context_tokens")
    reason = str(body.get("reason") or "")[:500]
    mgr.reset_session(role, cic=True)
    services.runs.set_native_session(conn, run["id"], None)
    services.runs.set_status(conn, run["id"], "created")
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (role, task_id, "session.reset_context",
         jdumps({"run_id": run["id"], "reason": reason}), now_iso()),
    )
    conn.commit()
    rewoken = False
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
            rewoken = True
        except Exception:  # noqa: BLE001 —— scheduler 会在下一轮拾取 created run
            rewoken = False
    events.publish("task", {"id": task_id, "kind": "session_reset", "role": role})
    return {
        "role": role, "task_id": task_id, "run_id": run["id"],
        "reset": True, "cic": True, "rewoken": rewoken,
        "context_tokens": tokens,
    }


@router.post("/me/memory")
def append_memory(body: MemoryAppendIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.append_memory(conn, auth.role or "qa-role", body.content)


@router.get("/me/memory/search")
def search_memory(q: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.search_memory(conn, auth.role or "qa-role", q)


@router.post("/me/notes")
def write_note(body: NoteWriteIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.write_note(conn, auth.role or "qa-role", body.path, body.content)


@router.post("/me/instruction")
def write_instruction(body: InstructionIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """`anvil self edit-instruction`（自主档，§9.2）：直接写 instruction.md。"""
    return services.memory.write_instruction(conn, auth.role or "qa-role", body.content)


@router.get("/proposals")
def list_proposals(status: str | None = None, conn=Depends(get_db)):
    return services.memory.list_proposals(conn, status)


@router.post("/proposals", status_code=201)
def create_proposal(body: ProposalIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    proposal = services.memory.create_proposal(
        conn, auth.role or "qa-role", body.field, body.proposed, body.rationale, body.risk,
    )
    events.publish("proposal", {"id": proposal["id"], "status": "pending"})
    return proposal


@router.post("/proposals/{pid}/approve")
def approve_proposal(pid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """审批通过：把提案字段落到角色配置（§9.2 审批档）。"""
    p = services.memory.approve_proposal(conn, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    events.publish("proposal", {"id": pid, "status": "approved"})
    return p


@router.post("/proposals/{pid}/reject")
def reject_proposal(pid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    p = services.memory.reject_proposal(conn, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    events.publish("proposal", {"id": pid, "status": "rejected"})
    return p


@router.get("/lessons")
def list_lessons(status: str | None = None, repo: str | None = None,
                 q: str | None = None, conn=Depends(get_db)):
    return services.memory.list_lessons(conn, status, repo, q)


@router.post("/lessons/pending", status_code=201)
def propose_lesson(body: LessonIn, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    return services.memory.create_lesson(
        conn, body.content, body.tags, repo=body.repo or "shared", source_task=body.source_task,
    )


@router.get("/lessons/pending")
def list_pending_lessons(conn=Depends(get_db)):
    return services.memory.list_lessons(conn, status="pending")


@router.put("/lessons/{lid}")
def update_lesson(lid: str, body: dict, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    lesson = services.memory.update_lesson(
        conn, lid, content=body.get("content"), tags=body.get("tags"), repo=body.get("repo"),
    )
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


@router.post("/lessons/{lid}/approve")
def approve_lesson(lid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        lesson = services.memory.approve_lesson(conn, lid)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    events.publish("proposal", {"id": lid, "kind": "lesson", "status": lesson["status"]})
    return lesson


@router.post("/lessons/{lid}/reject")
def reject_lesson(lid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    lesson = services.memory.reject_lesson(conn, lid)
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


@router.delete("/lessons/{lid}", status_code=204)
def delete_lesson(lid: str, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    if not services.memory.delete_lesson(conn, lid):
        raise HTTPException(status_code=404, detail="lesson not found")
    return None
