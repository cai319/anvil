"""派遣子任务：来源 task 下的非阻塞 child + 新上下文 + 共享 workspace。

与 delegation 子任务的区别：
- 不设置 parent_id / parent_step_id，不阻塞来源 workflow；
- 目标角色不因派遣获得来源 task 的读取权限；
- 完成后可按 task_dispatches.return_* 自动回到原切片上下文。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from .. import config
from ..util import jdumps, new_id, now_iso
from . import context_router, ledger, roles as roles_service
from . import runs as runs_service
from . import tasks as tasks_service


class DispatchError(Exception):
    pass


def _request_hash(source_task_id: str, source_slice_key: str | None,
                  target_role: str, message: str) -> str:
    normalized = " ".join(str(message or "").split()).strip().lower()
    raw = "|".join([source_task_id, source_slice_key or "", target_role, normalized])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _title_from_message(message: str, target_role: str) -> str:
    text = " ".join(str(message or "").split()).strip()
    if text.startswith("@"):
        text = text.split(maxsplit=1)[1] if " " in text else ""
    first = text.splitlines()[0].strip() if text else ""
    return (first[:120] or f"{target_role} · 派遣任务")


def _dispatch_depth(conn: sqlite3.Connection, task_id: str) -> int:
    depth = 0
    seen: set[str] = set()
    cur = task_id
    while cur and cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT source_task_id FROM tasks WHERE id=?", (cur,)).fetchone()
        if row is None or not row["source_task_id"]:
            break
        depth += 1
        cur = row["source_task_id"]
    return depth


def _active_dispatch(conn: sqlite3.Connection, request_hash: str,
                     source_task_id: str, source_slice_key: str | None,
                     target_role: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM task_dispatches WHERE request_hash=? AND source_task_id=? "
        "AND COALESCE(source_slice_key,'')=COALESCE(?,'') AND target_role=? "
        "AND status IN ('open','return_ready','returning') LIMIT 1",
        (request_hash, source_task_id, source_slice_key, target_role)).fetchone()
    return dict(row) if row else None


def _return_point(conn: sqlite3.Connection, target_role: str) -> tuple[str | None, str | None, str | None]:
    current = context_router.current_context(conn, target_role)
    context_key = current.get("context_key")
    run_id = current.get("run_id")
    task_id = current.get("task_id")
    slice_key = current.get("slice_key")
    if not run_id and task_id:
        # 进程空闲释放后 current_run_id 为空：找该上下文最近一个 suspended run。
        q = ("SELECT id FROM runs WHERE task_id=? AND role_slug=? AND status='suspended'")
        params: list = [task_id, target_role]
        if slice_key:
            q += " AND slice_key=?"
            params.append(slice_key)
        else:
            q += " AND slice_key IS NULL"
        row = conn.execute(q + " ORDER BY started_at DESC LIMIT 1", params).fetchone()
        run_id = row["id"] if row else None
    step_id = None
    if run_id:
        row = conn.execute("SELECT step_id FROM runs WHERE id=?", (run_id,)).fetchone()
        step_id = row["step_id"] if row else None
    return context_key, run_id, step_id


def create_dispatch(conn: sqlite3.Connection, *, source_task_id: str,
                    target_role: str, message: str, requester: str,
                    title: str | None = None, acceptance: list[str] | None = None,
                    scope_paths: list[str] | None = None,
                    source_slice_key: str | None = None,
                    source_step_id: str | None = None,
                    source_run_id: str | None = None,
                    repo: str | None = None, workspace_id: str | None = None,
                    base_branch: str | None = None,
                    force: bool = False,
                    reason_code: str = "explicit",
                    reason_json: dict | None = None) -> dict:
    """原子创建派遣子任务 + 目标 run + task_dispatches 记录（不 pump）。"""
    target_role = str(target_role or "").strip()
    if not target_role:
        raise DispatchError("target_role is required")
    role = roles_service.get(conn, target_role)
    if role is None or not role.get("enabled", True):
        raise DispatchError(f"unknown or disabled target role: {target_role}")
    if roles_service.is_onboarding(conn, target_role):
        raise DispatchError("onboarding assistant cannot receive dispatched tasks")
    source = tasks_service.get(conn, source_task_id)
    if source is None:
        raise DispatchError(f"source task not found: {source_task_id}")
    if source.get("status") in ("done", "closed") or source.get("archived_at"):
        raise DispatchError("source task is terminal; dispatch is not allowed")
    requester = (requester or "human").strip() or "human"
    if requester != "human" and not context_router.is_participant(
            conn, source_task_id, requester):
        raise DispatchError("requester is not a participant of the source task")
    # 派遣子任务复用来源 task 的 workspace，目标角色必须是来源 task 的参与者。
    if not context_router.is_participant(conn, source_task_id, target_role):
        raise DispatchError(
            f"target role {target_role} is not a participant of the source task; "
            "add it to the source task before dispatching")
    if _dispatch_depth(conn, source_task_id) >= max(0, int(config.DISPATCH_MAX_DEPTH)):
        raise DispatchError(
            f"dispatch depth limit reached ({config.DISPATCH_MAX_DEPTH})")

    request_hash = _request_hash(source_task_id, source_slice_key, target_role, message)
    if not force:
        existing = _active_dispatch(
            conn, request_hash, source_task_id, source_slice_key, target_role)
        if existing is not None:
            task = tasks_service.get(conn, existing["target_task_id"])
            run = runs_service.find_active_run(
                conn, existing["target_task_id"], target_role, slice_key=None)
            return {"task": task, "run": run, "dispatch": existing, "reused": True}
    else:
        request_hash = f"{request_hash}:force:{new_id()}"

    source_workflow_id = None
    if source_run_id:
        row = conn.execute(
            "SELECT workflow_id FROM runs WHERE id=?", (source_run_id,)).fetchone()
        source_workflow_id = row["workflow_id"] if row else None
    if not source_workflow_id:
        row = conn.execute(
            "SELECT id FROM workflows WHERE task_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (source_task_id,)).fetchone()
        source_workflow_id = row["id"] if row else None
    return_context_key, return_run_id, return_step_id = _return_point(conn, target_role)
    dispatch_id = new_id()
    now = now_iso()
    created_by = "human" if requester == "human" else f"agent:{requester}"
    task = tasks_service.create(
        conn,
        title=(title or "").strip() or _title_from_message(message, target_role),
        # 派遣是来源 task 下的非阻塞子任务，不是顶层任务；不参与 step 委派阻塞。
        type="dispatch",
        parent_id=source_task_id,
        parent_step_id=None,
        # workspace/repo/base_branch 由 tasks.create 从 parent 继承，禁止覆盖。
        scope_paths=scope_paths or [],
        description=message,
        acceptance=acceptance or [],
        assignee=target_role,
        created_by=created_by,
        commit=False,
    )
    tid = task["id"]
    conn.execute(
        "UPDATE tasks SET source_task_id=?, source_slice_key=?, source_step_id=?, "
        "source_workflow_id=?, requested_by=?, dispatch_status='open', "
        "dispatch_reason=?, dispatch_reason_json=?, updated_at=? WHERE id=?",
        (source_task_id, source_slice_key, source_step_id, source_workflow_id,
         requester, reason_code, jdumps(reason_json or {}), now, tid),
    )
    try:
        conn.execute(
            "INSERT INTO task_dispatches "
            "(id, source_task_id, source_workflow_id, source_step_id, source_slice_key, "
            " requester_run_id, requested_by, target_task_id, target_role, request_hash, "
            " return_context_key, return_run_id, return_step_id, reason_code, reason_json, "
            " status, attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',0,?,?)",
            (dispatch_id, source_task_id, source_workflow_id,
             source_step_id, source_slice_key, source_run_id, requester, tid, target_role,
             request_hash, return_context_key, return_run_id, return_step_id,
             reason_code, jdumps(reason_json or {}), now, now),
        )
    except sqlite3.IntegrityError as exc:
        # 并发重复请求：回滚本次未提交的 task/run，复用已存在的活跃 dispatch。
        conn.rollback()
        existing = _active_dispatch(
            conn, request_hash, source_task_id, source_slice_key, target_role)
        if existing is not None:
            task = tasks_service.get(conn, existing["target_task_id"])
            run = runs_service.find_active_run(
                conn, existing["target_task_id"], target_role, slice_key=None)
            return {"task": task, "run": run, "dispatch": existing, "reused": True}
        raise DispatchError(f"duplicate dispatch request: {exc}") from exc
    prompt = (
        "[anvil:dispatch] 你收到一个派遣子任务（来源 workflow 期间临时加入的小切片）。"
        "它挂在来源任务下，不推进 workflow、不阻塞父任务；请使用本任务自己的上下文独立完成。\n\n"
        f"来源任务：{source_task_id}"
        + (f" / 切片 {source_slice_key}" if source_slice_key else "")
        + f"\n请求者：{requester}\n\n请求：\n{message}\n")
    if acceptance:
        prompt += "\n验收标准：\n" + "\n".join(f"- {x}" for x in acceptance)
    prompt += ("\n\n完成后用 `anvil task submit` 提交结果；派遣子任务提交即 done，"
               "不进入人工评审。不要把来源任务的原始对话当作本任务上下文。")
    run = runs_service.create_run(
        conn, target_role, tid, prompt=prompt,
        dispatch_id=dispatch_id, origin="dispatch", commit=False)
    ledger.emit(
        conn, "dispatch.created", task_id=source_task_id,
        slice_key=source_slice_key, step_id=source_step_id, run_id=source_run_id,
        actor_type="human" if requester == "human" else "role", actor=requester,
        payload={
            "dispatch_id": dispatch_id, "target_task_id": tid,
            "target_role": target_role, "slice_key": source_slice_key,
            "reason_code": reason_code, "reason": reason_json or {},
        },
        mirror_activity=True, activity_kind="dispatch", activity_role=requester,
    )
    conn.commit()
    dispatch_row = conn.execute(
        "SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
    return {
        "task": tasks_service.get(conn, tid),
        "run": runs_service.get_run(conn, run["id"]),
        "dispatch": dict(dispatch_row) if dispatch_row else None,
        "reused": False,
    }


def append_wake_note(conn: sqlite3.Connection, run_id: str | None, note: str,
                     *, once_marker: str | None = None) -> None:
    """追加一次性 continuation；多个派遣结果不会被 COALESCE 吞掉。

    once_marker 用于 scheduler 重试：同一 marker 已存在时不再重复追加。
    """
    if not run_id or not note.strip():
        return
    row = conn.execute(
        "SELECT wake_note FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return
    existing = str(row["wake_note"] or "").strip()
    if once_marker and once_marker in existing:
        return
    merged = (existing + "\n\n" + note.strip()) if existing else note.strip()
    conn.execute("UPDATE runs SET wake_note=? WHERE id=?", (merged, run_id))


def _result_summary(conn: sqlite3.Connection, task_id: str, limit: int = 1500) -> str:
    row = conn.execute(
        "SELECT summary FROM task_role_claims WHERE task_id=? AND summary IS NOT NULL "
        "AND summary<>'' ORDER BY submitted_at DESC LIMIT 1", (task_id,)).fetchone()
    text = str(row["summary"] or "") if row else ""
    if not text:
        row = conn.execute(
            "SELECT payload_json FROM audit WHERE task_id=? AND action='submit' "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if row:
            try:
                import json
                payload = json.loads(row["payload_json"] or "{}")
                text = str(payload.get("summary") or "")
            except (TypeError, ValueError):
                text = ""
    if not text:
        row = conn.execute("SELECT title, description FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
        text = (str(row["title"] or "") if row else "") or "任务已完成"
    return text[:limit]


def _notify_dispatch_completed(conn: sqlite3.Connection, d: dict,
                               target_task_id: str, summary: str,
                               status: str, reason: str | None) -> None:
    """结果回流幂等：mention + dispatch.completed ledger 只发一次。"""
    if d.get("result_notified_at"):
        return
    requester = str(d.get("requested_by") or "")
    if requester and requester != str(d.get("target_role") or ""):
        try:
            tasks_service.mention(
                conn, d["source_task_id"], "system",
                requester if requester != "human" else "human",
                f"派遣任务 {target_task_id}「{_task_title(conn, target_task_id)}」"
                f"已完成，结果：{summary}",
                slice_key=d.get("source_slice_key"))
        except Exception:  # noqa: BLE001 —— 通知失败不阻断回切状态
            pass
    ledger.emit(
        conn, "dispatch.completed", task_id=d["source_task_id"],
        slice_key=d.get("source_slice_key"), step_id=d.get("source_step_id"),
        run_id=d.get("requester_run_id"),
        actor_type="system", actor="dispatch",
        payload={"dispatch_id": d["id"], "target_task_id": target_task_id,
                 "target_role": d.get("target_role"), "summary": summary,
                 "return_status": status, "return_reason": reason},
        mirror_activity=True, activity_kind="dispatch_result", activity_role="system",
    )
    conn.execute(
        "UPDATE task_dispatches SET result_notified_at=? WHERE id=?",
        (now_iso(), d["id"]))


def _return_state(conn: sqlite3.Connection, row: dict) -> tuple[bool | None, str | None]:
    """True=可回切；None=稍后再试；False=放弃并标记 no_return。"""
    run_id = row.get("return_run_id")
    context_key = row.get("return_context_key")
    if not run_id or not context_key:
        return False, "no_return_run"
    run = conn.execute(
        "SELECT task_id, role_slug, slice_key, status, step_id, native_session_id "
        "FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        return False, "return_run_missing"
    if run["status"] in ("completed", "failed", "abandoned",
                         "submitted", "reviewing"):
        return False, f"return_run_{run['status']}"
    if run["status"] != "suspended":
        return None, "return_run_busy"
    # 原 run 正在等人类确认时不能被派遣回切 resume，否则会绕过 HITL。
    if tasks_service.waiting_for_human(
            conn, run["task_id"], run["role_slug"], run["slice_key"]):
        return False, "return_run_waiting_human"
    sid = str(run["native_session_id"] or "")
    if sid and not sid.startswith("rs:"):
        # one-shot 运行没有可恢复的常驻会话；resume 会新建进程并丢失原上下文。
        return False, "return_run_one_shot"
    task = conn.execute(
        "SELECT status, archived_at FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
    if task is None or task["status"] in ("done", "closed") or task["archived_at"]:
        return False, "return_task_terminal"
    if row.get("return_step_id"):
        step = conn.execute(
            "SELECT ws.status, w.status AS wf_status "
            "FROM workflow_steps ws JOIN workflows w ON w.id=ws.workflow_id "
            "WHERE ws.run_id=? AND ws.step_id=?",
            (run_id, row["return_step_id"])).fetchone()
        if step is None or step["status"] != "running" or step["wf_status"] != "running":
            return False, "return_step_not_running"
    slot = conn.execute(
        "SELECT 1 FROM role_task_sessions WHERE role_slug=? AND task_key=? LIMIT 1",
        (row.get("target_role"), context_key)).fetchone()
    if slot is None:
        return False, "return_context_missing"
    return True, None


def on_task_done(conn: sqlite3.Connection, target_task_id: str) -> dict | None:
    """派遣任务 done：写结果回流，并把回切状态推进到 return_ready/no_return。

    实际 resume 由 scheduler 的 resolve_dispatch_returns 完成，保证崩溃后可恢复。
    """
    row = conn.execute(
        "SELECT * FROM task_dispatches WHERE target_task_id=? AND status IN "
        "('open','return_ready') ORDER BY created_at DESC LIMIT 1",
        (target_task_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    summary = _result_summary(conn, target_task_id)
    ok, reason = _return_state(conn, d)
    now = now_iso()
    if ok:
        status = "return_ready"
    elif ok is None:
        # 原 run 还在执行：保持 open，等下一次对账；结果已可回流。
        status = d["status"]
    else:
        status = "no_return"
    conn.execute(
        "UPDATE task_dispatches SET status=?, updated_at=? WHERE id=?",
        (status, now, d["id"]))
    conn.execute(
        "UPDATE tasks SET dispatch_status=?, updated_at=? WHERE id=?",
        (status, now, target_task_id))
    if ok:
        append_wake_note(conn, d["return_run_id"],
                         _return_note(conn, d, target_task_id))
    _notify_dispatch_completed(conn, d, target_task_id, summary, status, reason)
    conn.commit()
    return {**d, "status": status, "result_summary": summary}


def _task_title(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
    return str(row["title"] or task_id) if row else task_id


def _return_note(conn: sqlite3.Connection, d: dict, target_task_id: str) -> str:
    summary = _result_summary(conn, target_task_id)
    return (
        f"派遣任务 {target_task_id}「{_task_title(conn, target_task_id)}」已完成。\n"
        f"结果摘要：{summary}\n"
        f"来源任务：{d['source_task_id']}"
        + (f" / 切片 {d['source_slice_key']}" if d.get("source_slice_key") else "")
        + "\n请把该结果与当前切片工作合并；不要重复创建同一个派遣任务。")


def _promote_open_returns(conn: sqlite3.Connection) -> None:
    """任务 done 时原 run 还在忙：等它 suspended 后由对账推进 return_ready。"""
    rows = conn.execute(
        "SELECT d.* FROM task_dispatches d JOIN tasks t ON t.id=d.target_task_id "
        "WHERE d.status='open' AND t.status='done'").fetchall()
    for raw in rows:
        d = dict(raw)
        ok, reason = _return_state(conn, d)
        summary = _result_summary(conn, d["target_task_id"])
        now = now_iso()
        if ok is None:
            # 崩溃恢复时即使 return run 还在忙，也要先幂等回流结果。
            _notify_dispatch_completed(conn, d, d["target_task_id"], summary,
                                       d["status"], reason)
            conn.commit()
            continue
        if ok:
            conn.execute(
                "UPDATE task_dispatches SET status='return_ready', updated_at=? WHERE id=?",
                (now, d["id"]))
            conn.execute(
                "UPDATE tasks SET dispatch_status='return_ready', updated_at=? WHERE id=?",
                (now, d["target_task_id"]))
            append_wake_note(conn, d["return_run_id"],
                             _return_note(conn, d, d["target_task_id"]))
            status = "return_ready"
        else:
            conn.execute(
                "UPDATE task_dispatches SET status='no_return', updated_at=? WHERE id=?",
                (now, d["id"]))
            conn.execute(
                "UPDATE tasks SET dispatch_status='no_return', updated_at=? WHERE id=?",
                (now, d["target_task_id"]))
            status = "no_return"
        _notify_dispatch_completed(conn, d, d["target_task_id"], summary, status, reason)
        conn.commit()


def resolve_dispatch_returns(conn: sqlite3.Connection, pump,
                             max_attempts: int | None = None) -> int:
    """调度器对账：把 return_ready 的派遣回切真正 resume 原 run。"""
    from .. import config
    limit = max_attempts if max_attempts is not None else config.DISPATCH_RETURN_MAX_ATTEMPTS
    _promote_open_returns(conn)
    rows = conn.execute(
        "SELECT * FROM task_dispatches WHERE status IN ('return_ready','returning') "
        "ORDER BY updated_at").fetchall()
    done = 0
    for raw in rows:
        d = dict(raw)
        current = conn.execute(
            "SELECT status FROM task_dispatches WHERE id=?", (d["id"],)).fetchone()
        if current is None or current["status"] not in ("return_ready", "returning"):
            continue
        ok, reason = _return_state(conn, d)
        if ok is False:
            conn.execute(
                "UPDATE task_dispatches SET status='no_return', updated_at=? WHERE id=?",
                (now_iso(), d["id"]))
            conn.execute(
                "UPDATE tasks SET dispatch_status='no_return', updated_at=? WHERE id=?",
                (now_iso(), d["target_task_id"]))
            conn.commit()
            continue
        if ok is None:
            continue
        attempts = int(d.get("attempts") or 0) + 1
        if attempts > max(0, int(limit)):
            # 已超过预算：不再尝试。
            conn.execute(
                "UPDATE task_dispatches SET status='no_return', updated_at=? WHERE id=?",
                (now_iso(), d["id"]))
            conn.execute(
                "UPDATE tasks SET dispatch_status='no_return', updated_at=? WHERE id=?",
                (now_iso(), d["target_task_id"]))
            conn.commit()
            continue
        cur = conn.execute(
            "UPDATE task_dispatches SET status='returning', attempts=?, updated_at=? "
            "WHERE id=? AND status IN ('return_ready','returning')",
            (attempts, now_iso(), d["id"]))
        conn.commit()
        if cur.rowcount != 1:
            continue
        ok_pump = bool(pump(d["return_run_id"], "resume"))
        run = conn.execute("SELECT status FROM runs WHERE id=?",
                           (d["return_run_id"],)).fetchone()
        # pump 可能因 run 并发进入 submitted/completed 而空转；只有仍可投递才算回切成功。
        returned_ok = (
            ok_pump and run is not None
            and runs_service.is_dispatchable_status(run["status"])
        )
        if returned_ok:
            now = now_iso()
            # 同一个 return run 的多个派遣结果已在 wake_note 中合并；一次 resume
            # 即视为全部返回，避免为同一 run 重复 pump。
            siblings = conn.execute(
                "SELECT id, target_task_id FROM task_dispatches "
                "WHERE return_run_id=? AND status IN ('return_ready','returning')",
                (d["return_run_id"],)).fetchall()
            for s in siblings:
                conn.execute(
                    "UPDATE task_dispatches SET status='returned', returned_at=?, "
                    "updated_at=? WHERE id=?", (now, now, s["id"]))
                conn.execute(
                    "UPDATE tasks SET dispatch_status='returned', updated_at=? WHERE id=?",
                    (now, s["target_task_id"]))
            done += 1
        elif attempts >= max(0, int(limit)):
            conn.execute(
                "UPDATE task_dispatches SET status='no_return', updated_at=? WHERE id=?",
                (now_iso(), d["id"]))
            conn.execute(
                "UPDATE tasks SET dispatch_status='no_return', updated_at=? WHERE id=?",
                (now_iso(), d["target_task_id"]))
        else:
            conn.execute(
                "UPDATE task_dispatches SET status='return_ready', updated_at=? WHERE id=?",
                (now_iso(), d["id"]))
        conn.commit()
    return done


def record_context_return(conn: sqlite3.Connection, *, switched_run_id: str,
                          return_context_key: str | None,
                          return_run_id: str | None) -> None:
    """跨切片临时处理：在切过去的 run 上记录“本轮结束后回到 B”。"""
    if not switched_run_id or not return_context_key:
        return
    if return_context_key == _context_for_run(conn, switched_run_id):
        return
    switched = conn.execute(
        "SELECT role_slug FROM runs WHERE id=?", (switched_run_id,)).fetchone()
    if switched is None:
        return
    # idle release 后 current_run_id 可能为空：按 return context 找回最近 suspended run。
    if not return_run_id:
        from . import daemon as daemon_service
        task_id, slice_key = daemon_service.context_parts(return_context_key)
        if task_id:
            q = ("SELECT id FROM runs WHERE task_id=? AND role_slug=? "
                 "AND status='suspended'")
            params: list = [task_id, switched["role_slug"]]
            if slice_key:
                q += " AND slice_key=?"
                params.append(slice_key)
            else:
                q += " AND slice_key IS NULL"
            found = conn.execute(q + " ORDER BY started_at DESC LIMIT 1", params).fetchone()
            return_run_id = found["id"] if found else None
    if not return_run_id:
        return
    ret = conn.execute(
        "SELECT status FROM runs WHERE id=?", (return_run_id,)).fetchone()
    if ret is None or ret["status"] in ("completed", "failed", "abandoned",
                                        "submitted", "reviewing"):
        return
    conn.execute(
        "UPDATE runs SET context_return_context_key=?, context_return_run_id=?, "
        "context_return_after_turn=1, context_return_attempts=0, "
        "context_return_last_attempt_at=NULL "
        "WHERE id=? AND context_return_after_turn=0",
        (return_context_key, return_run_id, switched_run_id))
    conn.commit()


def _context_for_run(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT task_id, slice_key FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or not row["task_id"]:
        return None
    from . import daemon as daemon_service
    return daemon_service.context_key(row["task_id"], row["slice_key"])


def _context_return_note(conn: sqlite3.Connection, run: dict) -> str:
    """临时处理上下文的结果摘要：让回切后的角色知道发生了什么。"""
    task = conn.execute(
        "SELECT title FROM tasks WHERE id=?", (run.get("task_id"),)).fetchone()
    title = str(task["title"] or run.get("task_id") or "") if task else ""
    status = str(run.get("status") or "")
    lines = [f"你临时去处理的上下文「{title}」已结束（run {run['id']}，状态 {status}）。"]
    if run.get("step_id"):
        step = conn.execute(
            "SELECT title, summary, feedback, failure_reason FROM workflow_steps "
            "WHERE workflow_id=? AND step_id=?",
            (run.get("workflow_id"), run.get("step_id"))).fetchone()
        if step is not None:
            if step["summary"]:
                lines.append(f"结果摘要：{str(step['summary'])[:1200]}")
            if step["feedback"]:
                lines.append(f"反馈：{str(step['feedback'])[:800]}")
            if step["failure_reason"]:
                lines.append(f"失败原因：{str(step['failure_reason'])[:400]}")
    if run.get("failure_class"):
        lines.append(f"失败分类：{run['failure_class']}")
    lines.append("请回到原切片继续；不要再次自动切回临时上下文。")
    return "\n".join(lines)


def _parse_dt(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _context_return_valid(conn: sqlite3.Connection, ret: sqlite3.Row,
                          ret_key: str) -> bool:
    if ret is None or ret["status"] != "suspended":
        return False
    sid = str(ret["native_session_id"] or "")
    if sid and not sid.startswith("rs:"):
        return False
    task = conn.execute(
        "SELECT status, archived_at FROM tasks WHERE id=?",
        (ret["task_id"],)).fetchone()
    if task is None or task["status"] in ("done", "closed") or task["archived_at"]:
        return False
    if ret["step_id"]:
        step = conn.execute(
            "SELECT ws.status, w.status AS wf_status FROM workflow_steps ws "
            "JOIN workflows w ON w.id=ws.workflow_id WHERE ws.run_id=?",
            (ret["id"],)).fetchone()
        if step is None or step["status"] != "running" or step["wf_status"] != "running":
            return False
    slot = conn.execute(
        "SELECT 1 FROM role_task_sessions WHERE role_slug=? AND task_key=? LIMIT 1",
        (ret["role_slug"], ret_key)).fetchone()
    if slot is None:
        return False
    if tasks_service.waiting_for_human(
            conn, ret["task_id"], ret["role_slug"], ret["slice_key"]):
        return False
    return True


def _clear_context_return(conn: sqlite3.Connection, run_id: str, now: str) -> None:
    conn.execute(
        "UPDATE runs SET context_return_after_turn=0, context_returned_at=? WHERE id=?",
        (now, run_id))


def resolve_context_returns(conn: sqlite3.Connection, pump) -> int:
    """临时切换的 run 已结束/失败 → 自动 resume 原来的切片/任务 run 一次。

    同一个 return run 的多个临时结果合并为一次 pump；submitted/reviewing 不等待；
    失败按 attempts/backoff 重试，避免重复追加 wake_note 或永久挂起。
    """
    rows = conn.execute(
        "SELECT * FROM runs WHERE context_return_after_turn=1 "
        "AND context_returned_at IS NULL ORDER BY id").fetchall()
    groups: dict[tuple[str, str], list[dict]] = {}
    for raw in rows:
        run = dict(raw)
        ret_id = run.get("context_return_run_id")
        ret_key = run.get("context_return_context_key")
        if not ret_id or not ret_key:
            _clear_context_return(conn, run["id"], now_iso())
            continue
        groups.setdefault((ret_id, ret_key), []).append(run)
    conn.commit()

    done = 0
    for (ret_id, ret_key), entries in groups.items():
        ret = conn.execute(
            "SELECT id, task_id, role_slug, slice_key, status, step_id, "
            "native_session_id FROM runs WHERE id=?", (ret_id,)).fetchone()
        if ret is None or ret["status"] in (
                "completed", "failed", "abandoned", "submitted", "reviewing"):
            for run in entries:
                _clear_context_return(conn, run["id"], now_iso())
            conn.commit()
            continue
        if ret["status"] != "suspended":
            # 临时上下文仍在 running/created，等它真正结束再回切。
            continue
        if not _context_return_valid(conn, ret, ret_key):
            for run in entries:
                _clear_context_return(conn, run["id"], now_iso())
            conn.commit()
            continue

        valid: list[dict] = []
        for run in entries:
            if run["status"] in ("submitted", "reviewing"):
                # 临时工作已进入 HITL；按非阻塞规则不等待，清标记。
                _clear_context_return(conn, run["id"], now_iso())
                continue
            if run["status"] not in ("suspended", "completed", "failed", "abandoned"):
                continue
            valid.append(run)
        if not valid:
            conn.commit()
            continue

        attempts = max(int(run.get("context_return_attempts") or 0) for run in valid) + 1
        limit = max(0, int(config.CONTEXT_RETURN_MAX_ATTEMPTS))
        if attempts > limit:
            for run in valid:
                _clear_context_return(conn, run["id"], now_iso())
            conn.commit()
            continue
        last_attempts = [
            parsed for parsed in (_parse_dt(run.get("context_return_last_attempt_at"))
                                  for run in valid) if parsed is not None
        ]
        if last_attempts:
            backoff = max(0.0, float(config.CONTEXT_RETURN_BACKOFF_SECONDS))
            delay = min(300.0, backoff * (2 ** max(0, attempts - 2)))
            if (datetime.now(timezone.utc) - max(last_attempts)).total_seconds() < delay:
                continue

        # 同一 return run 的多个结果合并；note 用 run id 去重，pump 失败不会重复膨胀。
        for run in valid:
            append_wake_note(
                conn, ret_id, _context_return_note(conn, run),
                once_marker=f"run {run['id']}")
        now = now_iso()
        for run in valid:
            conn.execute(
                "UPDATE runs SET context_return_attempts=?, "
                "context_return_last_attempt_at=? WHERE id=?",
                (attempts, now, run["id"]))
        conn.commit()

        ok_pump = bool(pump(ret_id, "resume"))
        ret_after = conn.execute(
            "SELECT status FROM runs WHERE id=?", (ret_id,)).fetchone()
        returned_ok = bool(
            ok_pump and ret_after is not None
            and runs_service.is_dispatchable_status(ret_after["status"]))
        if returned_ok:
            for run in valid:
                _clear_context_return(conn, run["id"], now_iso())
            done += 1
        elif attempts >= limit:
            for run in valid:
                _clear_context_return(conn, run["id"], now_iso())
        conn.commit()
    return done


def list_dispatches(conn: sqlite3.Connection, source_task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT d.*, t.title AS target_title, t.status AS target_status "
        "FROM task_dispatches d LEFT JOIN tasks t ON t.id=d.target_task_id "
        "WHERE d.source_task_id=? ORDER BY d.created_at DESC",
        (source_task_id,)).fetchall()
    return [dict(r) for r in rows]
