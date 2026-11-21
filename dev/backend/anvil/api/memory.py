"""Agent self-service endpoints: memory / notes / proposals / lessons (§3.2/§3.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import config, services
from ..deps import Auth, get_auth, get_db, require_human
from ..services import tokens as tokens_service
from ..services import wake as wake_service
from ..schemas import InstructionIn, LessonIn, MemoryAppendIn, NoteWriteIn, ProposalIn
from ..util import ConflictError, jdumps, jloads, now_iso
from . import events

router = APIRouter()


@router.get("/me")
def get_me(conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """anvil self show — 返回自身身份 + 并发额度 + Token 用量（SE §3.3）。"""
    from ..util import jloads

    role_slug = auth.role or "qa-role"
    token_usage = tokens_service.summary(conn, scope=role_slug)
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
        "token_usage": {
            "input_tokens": token_usage["input_tokens"],
            "output_tokens": token_usage["output_tokens"],
            "total_tokens": token_usage["total_tokens"],
            "today_total_tokens": token_usage["today_total_tokens"],
        },
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
            "hard_limit_tokens": int(st.get("hard_limit_tokens") or window),
            "payload_chars": int(st.get("payload_chars") or 0),
            "payload_limit_chars": int(
                st.get("payload_limit_chars") or config.PAYLOAD_HARD_RESET_CHARS),
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

    mgr = services.daemon.get_manager()
    if mgr is None or not mgr.enabled:
        raise HTTPException(status_code=409, detail="persistent runtime is not enabled")
    st = mgr.status(role)
    if not st.get("alive"):
        raise HTTPException(status_code=409, detail="role process is not alive")
    # 以进程真实 current_run 为准：同 task 可能有多个 slice 的 suspended run，
    # 不能再用 find_active_run 猜“最新一个”。
    run = services.runs.get_run(conn, st.get("current_run_id") or "")
    if run is None or run.get("task_id") != task_id or run.get("role_slug") != role:
        raise HTTPException(
            status_code=409, detail="role is not currently processing this task")
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
    # 主动换会话前也固化任务知识，避免只有 daemon 触发的轮换才有快照。
    try:
        services.memory.consolidate_context(
            conn, role, task_id, "self-reset", run["id"],
            slice_key=run.get("slice_key"))
    except Exception:  # noqa: BLE001 —— 快照失败不阻断 reset
        pass
    mgr.reset_session(
        role, cic=True, task_id=task_id, slice_key=run.get("slice_key"))
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
    role = auth.role or "qa-role"
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    status = services.memory.resolve_practice_status(
        conn,
        role=role,
        practice_task_id=body.practice_task_id or auth.task_id,
        practice_run_id=body.practice_run_id or auth.run_id,
        force=(not is_real_agent) or (body.force and not is_real_agent),
    )
    return services.memory.append_memory(
        conn, role, body.content,
        practice_task_id=body.practice_task_id or auth.task_id,
        practice_run_id=body.practice_run_id or auth.run_id,
        outcome=body.outcome,
        source_knowledge=body.source_knowledge,
        status=status,
        durable=body.durable,
        kind=body.kind,
        tags=body.tags,
        repo=body.repo,
    )


@router.get("/me/memory/principles")
def get_principles(conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    role = auth.role or "qa-role"
    return {"role": role, "content": services.memory.read_principles(role)}


@router.put("/me/memory/principles")
def put_principles(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    role = auth.role or "qa-role"
    return services.memory.write_principles(
        conn, role, str(body.get("content") or ""),
        mode=str(body.get("mode") or "replace"), actor=role)


@router.post("/me/memory/compact")
def compact_memory(body: dict | None = None, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    """把 legacy MEMORY.md 拆成 PRINCIPLES.md + 经验卡；默认 dry-run 预览。"""
    body = body or {}
    role = str(body.get("role") or auth.role or "qa-role")
    if auth.role not in (None, "human") and role != auth.role:
        raise HTTPException(status_code=403, detail="agents may only compact their own memory")
    return services.memory.migrate_memory_file(
        conn, role, dry_run=bool(body.get("dry_run", True)))


@router.get("/me/memory/search")
def search_memory(q: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.search_memory(conn, auth.role or "qa-role", q)


@router.post("/me/notes")
def write_note(body: NoteWriteIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    role = auth.role
    if not role or role == "human":
        raise HTTPException(status_code=422, detail="agent session required")
    task_id = body.task_id or auth.task_id
    if auth.task_id and body.task_id and auth.task_id != body.task_id:
        raise HTTPException(status_code=403, detail="session is bound to another task")
    scope = body.scope or ("task" if task_id else "role")
    if scope == "task" and not task_id:
        raise HTTPException(status_code=422, detail="scope=task requires task_id")
    if body.kind and body.kind not in services.knowledge.NOTE_KINDS:
        raise HTTPException(status_code=422, detail=f"invalid note kind: {body.kind}")
    if scope not in services.knowledge.NOTE_SCOPES:
        raise HTTPException(status_code=422, detail=f"invalid note scope: {scope}")
    slice_key = body.slice_key
    if slice_key is None and scope == "task" and auth.run_id:
        run = conn.execute(
            "SELECT task_id, slice_key FROM runs WHERE id=?", (auth.run_id,)).fetchone()
        if run is not None and run["task_id"] == task_id:
            slice_key = run["slice_key"]
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    status = services.memory.resolve_practice_status(
        conn,
        role=role,
        practice_task_id=body.practice_task_id or auth.task_id,
        practice_run_id=body.practice_run_id or auth.run_id,
        force=(not is_real_agent) or (body.force and not is_real_agent),
    )
    try:
        return services.memory.write_note(
            conn, role, body.path, body.content,
            kind=body.kind, scope=scope, task_id=task_id, slice_key=slice_key,
            source_task=body.source_task, tags=body.tags, repo=body.repo,
            module=body.module, confidence=body.confidence, mode=body.mode,
            practice_task_id=body.practice_task_id or auth.task_id,
            practice_run_id=body.practice_run_id or auth.run_id,
            outcome=body.outcome,
            source_knowledge=body.source_knowledge,
            status=status, title=body.title, tldr=body.tldr,
            fields=body.fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _knowledge_role(conn, auth: Auth, requested: str | None) -> str:
    role = auth.role
    if role == "human":
        if not requested:
            raise HTTPException(
                status_code=422, detail="human browsing requires ?role=<slug>")
        role = requested
    elif requested and requested != role:
        raise HTTPException(status_code=403, detail="cannot search another role's knowledge")
    if not role:
        raise HTTPException(status_code=422, detail="role is required")
    return role


def _knowledge_task(conn, auth: Auth, role: str, task_id: str | None,
                    include_snapshots: bool) -> str | None:
    task_id = task_id or auth.task_id
    if auth.task_id and task_id and auth.task_id != task_id:
        raise HTTPException(status_code=403, detail="session is bound to another task")
    if include_snapshots and not task_id:
        raise HTTPException(status_code=422, detail="include_snapshots requires task_id")
    if task_id and auth.task_id is None and auth.role != "human":
        # 角色级常驻 token 可跨任务，但只能读取自己参与的任务。
        if services.roles.is_onboarding(conn, role):
            return task_id
        row = conn.execute(
            "SELECT 1 FROM tasks t WHERE t.id=? AND (t.assignee=? OR t.planner=? "
            "OR EXISTS (SELECT 1 FROM task_assignees a WHERE a.task_id=t.id AND a.role_slug=?))",
            (task_id, role, role, role)).fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="role is not bound to this task")
    return task_id


def _role_note_row(conn, role_slug: str, note_id: str):
    return conn.execute(
        "SELECT * FROM notes WHERE id=? AND role_slug=? AND scope='role'",
        (note_id, role_slug)).fetchone()


def _role_note_ready(row) -> tuple[bool | None, list[str]]:
    kind = row["kind"] or "note"
    if kind == "task_snapshot":
        return None, []
    errors = services.knowledge_card.submission_errors(
        kind, row["title"] or "", row["tldr"] or "", row["content"] or "",
        jloads(row["fields_json"], {}), strict_kind=False)
    return not errors, errors


def _role_queued(role_slug: str) -> bool:
    mgr = services.daemon.get_manager()
    st = mgr.status(role_slug) if mgr and getattr(mgr, "enabled", False) else {}
    return bool(st.get("busy") or st.get("queue_len"))


def _update_role_note(conn, role_slug: str, note_id: str, body: dict,
                      *, source: str = "human") -> dict | None:
    row = _role_note_row(conn, role_slug, note_id)
    if row is None:
        return None
    current_fields = jloads(row["fields_json"], {})
    fields = body.get("fields")
    merged_fields = current_fields if fields is None else {
        **current_fields, **(fields or {})}
    content = body.get("content", row["content"])
    services.memory.write_note(
        conn, role_slug, row["path"], str(content or ""),
        kind=row["kind"], scope="role",
        task_id=row["task_id"], source_task=row["source_task"],
        tags=body.get("tags", jloads(row["tags_json"], [])),
        repo=row["repo"], module=row["module"],
        slice_key=row["slice_key"] if "slice_key" in row.keys() else None,
        confidence=row["confidence"] or 0.5, mode="replace", source=source,
        practice_task_id=row["practice_task_id"],
        practice_run_id=row["practice_run_id"], outcome=row["outcome"],
        status=row["status"],
        title=body.get("title", row["title"]),
        tldr=body.get("tldr", row["tldr"]),
        fields=merged_fields,
    )
    return services.knowledge.get_knowledge(conn, role_slug, note_id)


def _role_note_completion_prompt(row) -> str:
    kind = row["kind"] or "note"
    fields = jloads(row["fields_json"], {})
    required = list(services.knowledge_card.QUALITY_REQUIRED) + list(
        services.knowledge_card.REQUIRED_FIELDS.get(kind, ()))
    required = list(dict.fromkeys(required))
    practice = []
    if row["practice_task_id"]:
        practice.append(f"task={row['practice_task_id']}")
    if row["practice_run_id"]:
        practice.append(f"run={row['practice_run_id']}")
    if row["outcome"]:
        practice.append(f"outcome={row['outcome']}")
    practice_text = ("；实践证据：" + "，".join(practice)) if practice else ""
    return (
        f"请补全并精炼角色知识 note={row['id']}（role={row['role_slug']}，"
        f"path={row['path']}，kind={kind}）。{practice_text}\n"
        f"当前标题：{row['title'] or '（空）'}\n"
        f"当前 TLDR：{row['tldr'] or '（空）'}\n"
        f"当前正文：\n{row['content'] or '（空）'}\n"
        f"当前字段：{jdumps(fields)}\n\n"
        "目标：标题 ≤80 字且能概括结论；TLDR ≤200 字；正文 ≤4000 字；"
        "去掉大段原始日志，保留结论、适用场景、证据路径与执行/验证方式。\n"
        f"按 kind={kind} 补齐字段：{', '.join(required) or 'use_when, expected_effect, evidence'}；"
        "通用 use_when / expected_effect / evidence 必须填写，单字段 ≤2000 字。\n"
        f"完成后执行：anvil knowledge fill {row['id']} --title \"...\" --tldr \"...\" "
        "--content-file <精炼后的正文文件> --field use_when=\"...\" "
        "--field expected_effect=\"...\" --field evidence=\"...\" ...\n"
        "请使用你对该知识产生过程的上下文与证据，不要编造；无法确认的边界写明确。"
    )


@router.get("/me/knowledge/search")
def search_knowledge(
    q: str = "", task_id: str | None = None, scope: str = "auto",
    kind: str | None = None, tags: str | None = None,
    repo: str | None = None, repos: str | None = None,
    module: str | None = None,
    include_snapshots: bool = False, include_unclassified: bool = False,
    limit: int = 8, role: str | None = None,
    conn=Depends(get_db), auth: Auth = Depends(get_auth),
):
    role_slug = _knowledge_role(conn, auth, role)
    task_id = _knowledge_task(conn, auth, role_slug, task_id, include_snapshots)
    if scope not in ("auto", "role", "task"):
        raise HTTPException(status_code=422, detail=f"invalid scope: {scope}")
    if scope == "task" and not task_id:
        raise HTTPException(status_code=422, detail="scope=task requires task_id")
    kinds = tuple(x.strip() for x in (kind or "").split(",") if x.strip()) or None
    tag_list = tuple(x.strip() for x in (tags or "").split(",") if x.strip()) or None
    scope_repos: list[str] | None = None
    if repos:
        scope_repos = [x.strip() for x in repos.split(",") if x.strip()]
    elif task_id and not repo:
        task_row = services.tasks.get(conn, task_id) or {}
        scope_repos = services.knowledge.scope_repos(
            conn, task_id, task_row.get("repo"))
    try:
        hits = services.knowledge.search_knowledge(
            conn, role_slug, q, task_id=task_id, scope=scope,
            kinds=kinds, tags=tag_list, repo=repo, module=module,
            repo_scope="bound_or_shared" if scope_repos else "any",
            repos=scope_repos,
            include_snapshots=include_snapshots,
            include_unclassified=include_unclassified, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"role_slug": role_slug, "task_id": task_id, "query": q,
            "count": len(hits), "results": hits}


@router.get("/me/knowledge/index")
def knowledge_index(task_id: str | None = None, role: str | None = None,
                    conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    role_slug = _knowledge_role(conn, auth, role)
    task_id = _knowledge_task(conn, auth, role_slug, task_id, False)
    return {"role_slug": role_slug, "task_id": task_id,
            **services.knowledge.knowledge_index(conn, role_slug, task_id)}


@router.post("/me/knowledge/reindex")
def reindex_knowledge(body: dict | None = None, role: str | None = None,
                      conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    role_slug = _knowledge_role(conn, auth, role)
    body = body or {}
    try:
        return services.knowledge.reindex_notes(
            conn, role_slug, dry_run=bool(body.get("dry_run")),
            prune=bool(body.get("prune")), require_practice=True)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/me/knowledge/{note_id}")
def get_knowledge(note_id: str, task_id: str | None = None,
                  role: str | None = None, conn=Depends(get_db),
                  auth: Auth = Depends(get_auth)):
    role_slug = _knowledge_role(conn, auth, role)
    task_id = _knowledge_task(conn, auth, role_slug, task_id, False)
    item = services.knowledge.get_knowledge(
        conn, role_slug, note_id, task_id=task_id,
        include_task_scope=auth.role == "human")
    if item is None:
        raise HTTPException(status_code=404, detail="knowledge not found")
    return item


@router.delete("/me/knowledge/{note_id}", status_code=204)
def delete_knowledge(note_id: str, role: str | None = None,
                     conn=Depends(get_db), auth: Auth = Depends(require_human)):
    role_slug = _knowledge_role(conn, auth, role)
    if not services.knowledge.delete_note(conn, role_slug, note_id):
        raise HTTPException(status_code=404, detail="knowledge not found")
    return None


@router.post("/me/knowledge/{note_id}/fill")
def fill_role_knowledge(note_id: str, body: dict, role: str | None = None,
                        conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """角色补全/精炼自己的角色知识；human 也可代为调用。"""
    role_slug = _knowledge_role(conn, auth, role)
    actor = auth.role or "human"
    if actor not in ("human", role_slug) and not auth.test_bypass:
        raise HTTPException(
            status_code=403, detail="only the owning role or human can fill this knowledge")
    try:
        updated = _update_role_note(conn, role_slug, note_id, body, source="agent")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="knowledge not found")
    services.knowledge_completion.complete(
        conn, "role_note", note_id, role_slug=role_slug)
    conn.commit()
    return services.knowledge.get_knowledge(
        conn, role_slug, note_id, include_task_scope=True)


@router.post("/me/knowledge/{note_id}/request-completion")
def request_role_knowledge_completion(
    note_id: str, request: Request, role: str | None = None,
    conn=Depends(get_db), auth: Auth = Depends(require_human),
):
    """让角色用自身上下文补全/精炼已有角色知识。"""
    role_slug = _knowledge_role(conn, auth, role)
    row = _role_note_row(conn, role_slug, note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="knowledge not found")
    if row["kind"] == "task_snapshot":
        raise HTTPException(
            status_code=422, detail="task snapshot is not a knowledge card")
    role_row = services.roles.get(conn, role_slug)
    if role_row is None or not role_row.get("enabled", True):
        raise HTTPException(
            status_code=422, detail=f"role is unavailable: {role_slug}")
    ready, errors = _role_note_ready(row)
    if ready:
        return {
            "status": "already_complete", "role": role_slug,
            "completion": services.knowledge_completion.status_for(
                conn, "role_note", note_id, ready=True),
        }
    active = services.knowledge_completion.status_for(
        conn, "role_note", note_id, ready=False)
    if active and active.get("active"):
        return {"status": "already_queued", "role": role_slug, "completion": active}
    prompt = _role_note_completion_prompt(row)
    queued = _role_queued(role_slug)
    context_task = row["task_id"] or row["source_task"]
    task = services.tasks.get(conn, context_task) if context_task else None
    role_bound = bool(task) and (
        role_slug in (task.get("assignees") or [])
        or task.get("assignee") == role_slug)
    active = conn.execute(
        "SELECT status FROM runs WHERE task_id=? AND role_slug=? "
        "AND status IN ('created','projecting','running','submitted','reviewing','suspended') "
        "ORDER BY started_at DESC LIMIT 1",
        (context_task, role_slug)).fetchone() if task is not None else None
    hitl_blocked = bool(active and active["status"] in ("submitted", "reviewing"))
    if task is not None and services.tasks.waiting_for_human(
            conn, context_task, role_slug, None):
        hitl_blocked = True
    if (task is not None and task.get("status") in ("todo", "in_progress")
            and role_bound and not hitl_blocked):
        services.tasks.mention(conn, context_task, "human", role_slug, prompt)
        req_id = wake_service.enqueue(
            conn, task_id=context_task, role_slug=role_slug,
            reason=f"knowledge.complete.{note_id}", note=prompt)
        services.knowledge_completion.create_job(
            conn, target_type="role_note", target_id=note_id,
            role_slug=role_slug, wake_request_id=req_id)
        conn.commit()
        return {
            "status": "requested", "role": role_slug,
            "task_id": context_task, "wake_request_id": req_id,
            "queued": queued,
            "completion": services.knowledge_completion.status_for(
                conn, "role_note", note_id, ready=False),
        }
    run = services.runs.create_run(
        conn, role_slug, None, prompt=prompt, origin="knowledge_complete")
    services.knowledge_completion.create_job(
        conn, target_type="role_note", target_id=note_id,
        role_slug=role_slug, run_id=run["id"])
    orch = getattr(request.app.state, "orchestrator", None)
    if orch and not queued:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    return {
        "status": "requested", "role": role_slug, "run_id": run["id"],
        "queued": queued,
        "completion": services.knowledge_completion.status_for(
            conn, "role_note", note_id, ready=False),
    }


@router.post("/knowledge/reindex")
def reindex_all(body: dict | None = None, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    """Human-only reindex for one role or all roles."""
    body = body or {}
    role = str(body.get("role") or "").strip() or None
    roles = [role] if role else [
        r["slug"] for r in conn.execute("SELECT slug FROM roles WHERE enabled=1")]
    out = []
    for slug in roles:
        try:
            out.append(services.knowledge.reindex_notes(
                conn, slug, dry_run=bool(body.get("dry_run")),
                prune=bool(body.get("prune"))))
        except Exception as exc:  # noqa: BLE001
            out.append({"role": slug, "error": str(exc)})
    return {"roles": out}


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
    try:
        return services.memory.create_lesson(
            conn, body.content, body.tags, repo=body.repo or "shared",
            source_task=body.source_task, source_role=auth.role,
            title=body.title, tldr=body.tldr, fields=body.fields,
        )
    except services.knowledge_card.IncompleteKnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/lessons/pending")
def list_pending_lessons(conn=Depends(get_db)):
    return services.memory.list_lessons(conn, status="pending")


@router.get("/lessons/{lid}")
def get_lesson(lid: str, conn=Depends(get_db)):
    lesson = services.memory.get_lesson(conn, lid)
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


@router.post("/lessons/{lid}/request-completion")
def request_lesson_completion(lid: str, request: Request, conn=Depends(get_db),
                              auth: Auth = Depends(require_human)):
    """让收割角色（source_role）用来源任务上下文补全知识字段。"""
    lesson = services.memory.get_lesson(conn, lid)
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    if lesson["status"] not in ("pending", "approved"):
        raise HTTPException(
            status_code=409, detail="only pending/approved lessons can be completed")
    role = lesson.get("source_role")
    if not role or role == "human":
        raise HTTPException(status_code=422, detail="lesson has no harvesting role")
    role_row = services.roles.get(conn, role)
    if role_row is None or not role_row.get("enabled", True):
        raise HTTPException(status_code=422, detail=f"harvesting role is unavailable: {role}")
    missing = services.memory.lesson_missing_fields(lesson)
    if not missing:
        return {
            "status": "already_complete", "role": role, "lesson": lesson,
            "completion": services.knowledge_completion.status_for(
                conn, "lesson", lid, ready=True),
        }
    active = services.knowledge_completion.status_for(
        conn, "lesson", lid, ready=False)
    if active and active.get("active"):
        return {"status": "already_queued", "role": role, "completion": active}

    def role_queued() -> bool:
        mgr = services.daemon.get_manager()
        st = mgr.status(role) if mgr and getattr(mgr, "enabled", False) else {}
        return bool(st.get("busy") or st.get("queue_len"))

    message = services.memory.lesson_completion_message(lesson)
    source_task = lesson.get("source_task")
    task = services.tasks.get(conn, source_task) if source_task else None
    role_bound = bool(task) and (
        role in (task.get("assignees") or [])
        or task.get("assignee") == role)
    active = conn.execute(
        "SELECT status FROM runs WHERE task_id=? AND role_slug=? "
        "AND status IN ('created','projecting','running','submitted','reviewing','suspended') "
        "ORDER BY started_at DESC LIMIT 1",
        (source_task, role)).fetchone() if (task is not None and role_bound) else None
    hitl_blocked = bool(active and active["status"] in ("submitted", "reviewing"))
    if task is not None and services.tasks.waiting_for_human(
            conn, source_task, role, None):
        hitl_blocked = True
    if (task is not None and task.get("status") in ("todo", "in_progress")
            and role_bound and not hitl_blocked):
        services.tasks.mention(conn, source_task, "human", role, message)
        req_id = wake_service.enqueue(
            conn, task_id=source_task, role_slug=role,
            reason=f"lesson.complete.{lid}", note=message)
        services.knowledge_completion.create_job(
            conn, target_type="lesson", target_id=lid, role_slug=role,
            wake_request_id=req_id)
        conn.commit()
        return {
            "status": "requested", "role": role, "task_id": source_task,
            "wake_request_id": req_id, "missing": missing,
            "queued": role_queued(),
            "completion": services.knowledge_completion.status_for(
                conn, "lesson", lid, ready=False),
        }
    queued = role_queued()
    run = services.runs.create_run(
        conn, role, None, prompt=message, origin="lesson_complete")
    services.knowledge_completion.create_job(
        conn, target_type="lesson", target_id=lid, role_slug=role,
        run_id=run["id"])
    orch = getattr(request.app.state, "orchestrator", None)
    if orch and not queued:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    # 角色忙/已有排队：run 保持 created，由 scheduler 的 pump_created 在角色空闲后投递。
    return {
        "status": "requested", "role": role, "run_id": run["id"],
        "missing": missing, "queued": queued,
        "completion": services.knowledge_completion.status_for(
            conn, "lesson", lid, ready=False),
    }


@router.post("/lessons/{lid}/fill")
def fill_lesson(lid: str, body: dict, conn=Depends(get_db),
                auth: Auth = Depends(get_auth)):
    """收割角色补全字段；必须一次满足知识提交前的必须条款。"""
    lesson = services.memory.get_lesson(conn, lid)
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    actor = auth.role or "human"
    if actor not in ("human", lesson.get("source_role")) and not auth.test_bypass:
        raise HTTPException(
            status_code=403, detail="only the harvesting role or human can fill this lesson")
    if lesson["status"] not in ("pending", "approved"):
        raise HTTPException(
            status_code=409, detail="only pending/approved lessons can be filled")
    title = str(body.get("title") or lesson["title"] or "")
    tldr = str(body.get("tldr") or lesson["tldr"] or "")
    content = str(body.get("content") or lesson["content"] or "")
    fields = {
        **(lesson.get("fields") or {}),
        **(body.get("fields") or {}),
    }
    try:
        services.knowledge_card.assert_submittable(
            "insight", title, tldr, content, fields)
    except services.knowledge_card.IncompleteKnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.memory.update_lesson(
        conn, lid, title=title, tldr=tldr, content=content, fields=fields)
    services.knowledge_completion.complete(
        conn, "lesson", lid, role_slug=lesson.get("source_role"))
    conn.commit()
    events.publish("proposal", {"id": lid, "kind": "lesson", "status": "pending"})
    return services.memory.get_lesson(conn, lid)


@router.put("/lessons/{lid}")
def update_lesson(lid: str, body: dict, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    lesson = services.memory.update_lesson(
        conn, lid, content=body.get("content"), tags=body.get("tags"),
        repo=body.get("repo"), title=body.get("title"), tldr=body.get("tldr"),
        fields=body.get("fields"),
    )
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


@router.post("/lessons/{lid}/approve")
def approve_lesson(lid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        lesson = services.memory.approve_lesson(conn, lid)
    except services.knowledge_card.IncompleteKnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
