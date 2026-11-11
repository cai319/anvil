"""上下文路由：复用当前上下文、切换切片上下文，还是创建派遣子任务。

身份规则优先（同一 task/slice、已有槽、参与者关系），只有身份不足以判定时
才用轻量词法相关度。不调用模型、不做 embedding；每次决策都可解释、可审计。
"""
from __future__ import annotations

import sqlite3

from . import knowledge as knowledge_service

REUSE_SCORE = 0.15
SWITCH_SCORE = 0.08


def _daemon():
    from . import daemon  # 惰性导入，避免 db→router→daemon 循环
    return daemon


def _context_key(task_id: str | None, slice_key: str | None = None) -> str:
    return _daemon().context_key(task_id, slice_key)


def _parts(key: str | None) -> tuple[str | None, str | None]:
    return _daemon().context_parts(key)


def _norm_slice(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def current_context(conn: sqlite3.Connection, role: str) -> dict:
    """角色当前加载的上下文（优先取 daemon 内存态，回退 DB 镜像）。"""
    from . import daemon as daemon_service

    mgr = daemon_service.get_manager()
    if mgr is not None and getattr(mgr, "enabled", False):
        st = mgr.status(role)
        key = st.get("context_key")
        if key:
            task_id, slice_key = _parts(key)
            return {
                "context_key": key, "task_id": task_id, "slice_key": slice_key,
                "run_id": st.get("current_run_id"),
                "status": st.get("status"),
            }
    row = conn.execute(
        "SELECT current_context_key, current_task_id, current_slice_key, "
        "current_run_id, status FROM role_sessions WHERE role_slug=?",
        (role,)).fetchone()
    if row is None:
        slot = conn.execute(
            "SELECT task_key, task_id, slice_key, status FROM role_task_sessions "
            "WHERE role_slug=? ORDER BY COALESCE(last_active_at, updated_at) DESC LIMIT 1",
            (role,)).fetchone()
        if slot is None:
            return {"context_key": None, "task_id": None, "slice_key": None,
                    "run_id": None, "status": "unknown"}
        task_id, slice_key = slot["task_id"], slot["slice_key"]
        if task_id is None:
            task_id, slice_key = _parts(slot["task_key"])
        return {
            "context_key": slot["task_key"], "task_id": task_id,
            "slice_key": slice_key, "run_id": None, "status": slot["status"],
        }
    key = row["current_context_key"]
    task_id, slice_key = _parts(key)
    if task_id is None and row["current_task_id"]:
        task_id = row["current_task_id"]
        slice_key = row["current_slice_key"]
        key = _context_key(task_id, slice_key)
    if not key:
        # 进程空闲释放/服务重启后 current_* 会被清空；用最近活跃槽作为
        # “最后工作上下文”，避免路由和派遣回切丢失返回点。
        slot = conn.execute(
            "SELECT task_key, task_id, slice_key, status FROM role_task_sessions "
            "WHERE role_slug=? ORDER BY COALESCE(last_active_at, updated_at) DESC LIMIT 1",
            (role,)).fetchone()
        if slot is not None:
            key = slot["task_key"]
            task_id = slot["task_id"]
            slice_key = slot["slice_key"]
            if task_id is None:
                task_id, slice_key = _parts(key)
            return {
                "context_key": key, "task_id": task_id, "slice_key": slice_key,
                "run_id": None, "status": slot["status"],
            }
    return {
        "context_key": key, "task_id": task_id, "slice_key": slice_key,
        "run_id": row["current_run_id"], "status": row["status"],
    }


def _slots(conn: sqlite3.Connection, role: str) -> list[dict]:
    out: list[dict] = []
    for row in conn.execute(
        "SELECT task_key, task_id, slice_key, session_id, status, "
        "COALESCE(last_active_at, updated_at) AS active_at "
        "FROM role_task_sessions WHERE role_slug=? ORDER BY active_at DESC",
        (role,)).fetchall():
        task_id = row["task_id"]
        slice_key = row["slice_key"]
        if task_id is None:
            task_id, parsed_slice = _parts(row["task_key"])
            slice_key = slice_key or parsed_slice
        if task_id is None:
            continue
        out.append({
            "context_key": row["task_key"], "task_id": task_id,
            "slice_key": slice_key, "has_session": bool(row["session_id"]),
            "status": row["status"], "active_at": row["active_at"],
        })
    return out


def is_participant(conn: sqlite3.Connection, task_id: str | None, role: str,
                   slice_key: str | None = None) -> bool:
    return _is_participant(conn, task_id, role, slice_key)


def _is_participant(conn: sqlite3.Connection, task_id: str | None, role: str,
                    slice_key: str | None = None) -> bool:
    if not task_id:
        return False
    task = conn.execute(
        "SELECT assignee, planner FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return False
    if task["assignee"] == role or task["planner"] == role:
        return True
    if conn.execute(
            "SELECT 1 FROM task_assignees WHERE task_id=? AND role_slug=? LIMIT 1",
            (task_id, role)).fetchone():
        return True
    if conn.execute(
            "SELECT 1 FROM task_role_claims WHERE task_id=? AND role_slug=? "
            "AND status IN ('pending','working','submitted') LIMIT 1",
            (task_id, role)).fetchone():
        return True
    q = ("SELECT 1 FROM workflow_steps ws JOIN workflows w ON w.id=ws.workflow_id "
         "WHERE w.task_id=? AND ws.role_slug=?")
    params: list = [task_id, role]
    if slice_key:
        q += " AND ws.slice_key=?"
        params.append(slice_key)
    return conn.execute(q + " LIMIT 1", params).fetchone() is not None


def _has_slice_role(conn: sqlite3.Connection, task_id: str | None, role: str,
                    slice_key: str | None) -> bool:
    if not task_id or not slice_key:
        return False
    return conn.execute(
        "SELECT 1 FROM workflow_steps ws JOIN workflows w ON w.id=ws.workflow_id "
        "WHERE w.task_id=? AND ws.slice_key=? AND ws.role_slug=? LIMIT 1",
        (task_id, slice_key, role)).fetchone() is not None


def _scope_text(conn: sqlite3.Connection, task_id: str | None,
                slice_key: str | None) -> str:
    if not task_id:
        return ""
    parts: list[str] = []
    task = conn.execute(
        "SELECT title, description, tags_json, scope_paths_json FROM tasks WHERE id=?",
        (task_id,)).fetchone()
    if task is not None:
        parts.extend([str(task["title"] or ""), str(task["description"] or "")])
        import json
        try:
            parts.extend(str(x) for x in json.loads(task["scope_paths_json"] or "[]"))
            parts.extend(str(x) for x in json.loads(task["tags_json"] or "[]"))
        except (TypeError, ValueError):
            pass
    if slice_key:
        q = ("SELECT ws.title, ws.instructions, ws.scope_paths_json "
             "FROM workflow_steps ws JOIN workflows w ON w.id=ws.workflow_id "
             "WHERE w.task_id=? AND ws.slice_key=?")
        for row in conn.execute(q, (task_id, slice_key)).fetchall():
            parts.extend([str(row["title"] or ""), str(row["instructions"] or "")])
            try:
                import json
                parts.extend(str(x) for x in json.loads(row["scope_paths_json"] or "[]"))
            except (TypeError, ValueError):
                pass
    return "\n".join(p for p in parts if p)


def _score(message: str, target_text: str) -> float:
    request = set(knowledge_service.tokenize_for_match(message))
    target = set(knowledge_service.tokenize_for_match(target_text))
    if not request or not target:
        return 0.0
    return round(len(request & target) / len(request | target), 4)


def route(conn: sqlite3.Connection, *, target_role: str,
          source_task_id: str | None, source_slice_key: str | None = None,
          source_step_id: str | None = None, source_run_id: str | None = None,
          action_kind: str = "mention", message: str = "",
          explicit_mode: str | None = None,
          target_task_id: str | None = None,
          target_slice_key: str | None = None,
          scope_paths: list[str] | None = None) -> dict:
    """返回 ContextDecision dict。

    mode: reuse | switch | new_slice | dispatch | queue
    """
    explicit_mode = (explicit_mode or "").strip().lower() or None
    target_task = target_task_id or source_task_id
    target_slice = _norm_slice(
        target_slice_key if target_slice_key is not None else source_slice_key)
    current = current_context(conn, target_role)
    slots = _slots(conn, target_role)

    def decision(mode: str, *, context_key: str | None = None,
                 task_id: str | None = None, slice_key: str | None = None,
                 reasons: list[str] | None = None, score: float | None = None,
                 confidence: str = "hard") -> dict:
        return {
            "mode": mode,
            "context_key": context_key,
            "target_task_id": task_id,
            "target_slice_key": slice_key,
            "current_context_key": current.get("context_key"),
            "current_run_id": current.get("run_id"),
            "reasons": reasons or [],
            "score": score,
            "confidence": confidence,
        }

    if explicit_mode == "dispatch":
        return decision("dispatch", task_id=target_task, slice_key=target_slice,
                        reasons=["explicit:dispatch"])
    if explicit_mode == "reuse":
        return decision("reuse", context_key=current.get("context_key"),
                        task_id=current.get("task_id"),
                        slice_key=current.get("slice_key"),
                        reasons=["explicit:reuse"])
    if explicit_mode == "switch":
        key = _context_key(target_task, target_slice)
        return decision("switch", context_key=key, task_id=target_task,
                        slice_key=target_slice, reasons=["explicit:switch"])

    desired_key = _context_key(target_task, target_slice) if target_task else None
    if desired_key and desired_key == current.get("context_key"):
        return decision("reuse", context_key=desired_key, task_id=target_task,
                        slice_key=target_slice, reasons=["same_task_slice"])

    slot_by_key = {s["context_key"]: s for s in slots}
    if desired_key and desired_key in slot_by_key:
        return decision("switch", context_key=desired_key, task_id=target_task,
                        slice_key=target_slice, reasons=["known_slice_slot"])
    if target_task and target_slice is None:
        same_task_slots = [s for s in slots if s["task_id"] == target_task]
        if same_task_slots:
            chosen = same_task_slots[0]
            return decision(
                "switch", context_key=chosen["context_key"], task_id=target_task,
                slice_key=chosen["slice_key"], reasons=["known_task_slot"])

    participant = _is_participant(conn, target_task, target_role, target_slice)
    if participant:
        target_text = _scope_text(conn, target_task, target_slice)
        request_text = " ".join([message, *(scope_paths or [])])
        participant_score = _score(request_text, target_text)
        # 角色空闲且请求就在来源 task：这是“开始/继续本任务”，即使词法分数低
        # 也不升级为派遣。角色正在别的 task、或请求跨 task 时才进入 dispatch 判定。
        same_task = bool(
            target_task and (
                target_task == current.get("task_id")
                or (
                    target_task == source_task_id
                    and not current.get("task_id")
                )
            ))
        slice_role = bool(target_slice) and _has_slice_role(
            conn, target_task, target_role, target_slice)
        if target_slice:
            if slice_role:
                return decision(
                    "new_slice", context_key=_context_key(target_task, target_slice),
                    task_id=target_task, slice_key=target_slice,
                    reasons=["participant_new_slice"])
            if participant_score < SWITCH_SCORE:
                # 参与者但请求不属于其切片/职责且相关度低：允许转为派遣子任务。
                return decision(
                    "dispatch", task_id=target_task, slice_key=target_slice,
                    reasons=["participant_low_relevance"], score=participant_score,
                    confidence="scored")
            return decision(
                "new_slice", context_key=_context_key(target_task, target_slice),
                task_id=target_task, slice_key=target_slice,
                reasons=["participant_slice_relevant"], score=participant_score,
                confidence="scored")
        if not same_task and participant_score < SWITCH_SCORE:
            return decision(
                "dispatch", task_id=target_task, slice_key=None,
                reasons=["participant_low_relevance"], score=participant_score,
                confidence="scored")
        return decision(
            "new_slice", context_key=_context_key(target_task), task_id=target_task,
            slice_key=None, reasons=["participant_task_level"],
            score=participant_score, confidence="scored")

    # 身份规则无法判定：按轻量相关度选最接近的已有上下文。
    target_text = _scope_text(conn, target_task, target_slice)
    request_text = " ".join([message, *(scope_paths or [])])
    base_score = _score(request_text, target_text)
    best = None
    best_score = 0.0
    for slot in slots:
        text = _scope_text(conn, slot["task_id"], slot["slice_key"])
        sc = _score(request_text, text)
        if sc > best_score:
            best, best_score = slot, sc
    if best is not None and best_score >= REUSE_SCORE:
        return decision(
            "switch", context_key=best["context_key"], task_id=best["task_id"],
            slice_key=best["slice_key"], reasons=["scored_known_slot"],
            score=best_score, confidence="scored")
    if best is not None and best_score >= SWITCH_SCORE and not target_task:
        return decision(
            "switch", context_key=best["context_key"], task_id=best["task_id"],
            slice_key=best["slice_key"], reasons=["scored_weak_known_slot"],
            score=best_score, confidence="scored")
    if base_score >= REUSE_SCORE:
        return decision(
            "new_slice", context_key=_context_key(target_task, target_slice),
            task_id=target_task, slice_key=target_slice,
            reasons=["scored_relevant_task"], score=base_score, confidence="scored")
    return decision(
        "dispatch", task_id=target_task, slice_key=target_slice,
        reasons=["low_relevance_no_owner"], score=base_score, confidence="scored")
