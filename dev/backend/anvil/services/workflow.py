"""Workflow（工作流）模型与执行引擎。

- **Task** 是人类的高层工作空间；**Workflow** 是 Cindy 生成的步骤状态机，属于一个 Task。
- 步骤不是独立 task 行；跨角色可并行，**同一角色的多个步骤由引擎串行调度**。
- 状态：pending → running → submitted → approved / rejected / failed；gate=1 的步骤
  提交后进入收件箱等待自然人评审（HITL）。
- 依赖满足且角色空闲时，advance() 通过 starter 创建 run；scheduler 每轮推进。
"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, jloads, new_id, now_iso

RUN_ACTIVE = ("created", "projecting", "running")
STEP_DONE = ("approved", "skipped")


def create_workflow(conn: sqlite3.Connection, task_id: str, definition: dict,
                    created_by: str = "agent:cindy") -> dict:
    """落板 workflow（Cindy 草案批准后调用）：创建步骤并把任务置为进行中。"""
    steps = definition.get("steps") or definition.get("children") or []
    if not steps:
        raise ConflictError("workflow has no steps")
    wid = new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO workflows (id, task_id, definition_json, status, created_by, "
        "created_at, approved_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (wid, task_id, jdumps(definition), "running", created_by, now, now, now),
    )
    for s in steps:
        sid = str(s.get("id") or s.get("step_id") or "")
        if not sid:
            raise ConflictError("workflow step missing id")
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, instructions, "
            "depends_on_json, acceptance_json, gate, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (wid, sid, str(s.get("title") or sid), str(s.get("role") or s.get("role_slug") or ""),
             str(s.get("instructions") or s.get("description") or ""),
             jdumps(s.get("depends_on") or []), jdumps(s.get("acceptance") or []),
             1 if s.get("gate") else 0, "pending", now),
        )
    conn.execute(
        "UPDATE tasks SET status='in_progress', assignee=NULL, lease_expires_at=NULL, "
        "claimed_at=NULL, updated_at=? WHERE id=? AND status IN ('todo','in_progress')",
        (now, task_id))
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def get_workflow(conn: sqlite3.Connection, wid: str) -> dict | None:
    r = conn.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["definition"] = jloads(d.get("definition_json") or "{}", {})
    d["steps"] = _steps(conn, wid)
    return d


def get_for_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    r = conn.execute(
        "SELECT id FROM workflows WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return get_workflow(conn, r["id"]) if r else None


def _steps(conn: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY rowid", (wid,)):
        d = dict(r)
        d["depends_on"] = jloads(d.get("depends_on_json") or "[]", [])
        d["acceptance"] = jloads(d.get("acceptance_json") or "[]", [])
        d["gate"] = bool(d["gate"])
        out.append(d)
    return out


def step_prompt(workflow: dict, step: dict, task: dict | None) -> str:
    """注入给角色的步骤说明（替代任务级协议）。"""
    deps = "、".join(step["depends_on"]) or "无"
    acceptance = "\n".join(f"- {a}" for a in step["acceptance"]) or "-（无）"
    title = (task or {}).get("title") or ""
    desc = (task or {}).get("description") or ""
    return (
        f"## 工作流步骤 {step['step_id']}：{step['title']}\n"
        f"所属任务：{title}\n"
        f"工作流 ID：{workflow['id']}\n"
        f"你的角色：{step['role_slug']}\n"
        f"依赖步骤（已完成）：{deps}\n"
        f"验收标准：\n{acceptance}\n\n"
        f"任务背景：\n{desc}\n\n"
        f"步骤说明：\n{step['instructions'] or '（无额外说明，按标题与验收标准执行）'}\n\n"
        f"完成后必须执行：`anvil step submit {workflow['id']} {step['step_id']} --summary \"完成摘要\"`。\n"
        "不要调用 `anvil task submit` / `anvil task claim`。\n"
    )


def advance(conn: sqlite3.Connection, wid: str, starter=None) -> dict:
    """推进工作流：为依赖满足且角色空闲的步骤创建 run。

    starter(role, task_id, prompt, step) -> run dict（由 scheduler/API 注入）。
    同一工作流内同角色一次只有 running/submitted 步骤；全局该角色已有活跃 run 时也跳过。
    """
    wf = get_workflow(conn, wid)
    if not wf or wf["status"] != "running":
        return {"started": [], "done": wf is not None and wf["status"] == "done"}
    steps = wf["steps"]
    by_id = {s["step_id"]: s for s in steps}
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (wf["task_id"],)).fetchone()
    started: list[str] = []
    for s in steps:
        if s["status"] != "pending":
            continue
        if any(by_id.get(d, {}).get("status") not in STEP_DONE for d in s["depends_on"]):
            continue
        role = s["role_slug"]
        if not role:
            continue
        if conn.execute(
            "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND role_slug=? "
            "AND status IN ('running','submitted')", (wid, role)
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM runs WHERE role_slug=? AND status IN (?,?,?)",
            (role, *RUN_ACTIVE),
        ).fetchone():
            continue
        prompt = step_prompt(wf, s, dict(task) if task else None)
        if starter is not None:
            run = starter(role, wf["task_id"], prompt, s)
            run_id = (run or {}).get("id")
        else:
            run_id = None
        now = now_iso()
        conn.execute(
            "UPDATE workflow_steps SET status='running', run_id=?, started_at=?, ended_at=NULL, "
            "updated_at=? WHERE workflow_id=? AND step_id=?",
            (run_id, now, now, wid, s["step_id"]),
        )
        started.append(s["step_id"])
    conn.commit()
    _finalize_if_done(conn, wid)
    wf = get_workflow(conn, wid)
    all_done = wf is not None and wf["status"] == "done"
    return {"started": started, "done": all_done}


def submit_step(conn: sqlite3.Connection, wid: str, step_id: str, role: str,
                summary: str | None) -> dict:
    """角色提交当前步骤（anvil step submit）。gate 步骤进入人工审核。"""
    from . import runs as runs_service

    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None:
        raise LookupError(step_id)
    if step["role_slug"] != role:
        raise ConflictError(f"step {step_id} belongs to {step['role_slug']}, not {role}")
    if step["status"] != "running":
        raise ConflictError(f"step {step_id} is {step['status']}, expected running")
    now = now_iso()
    conn.execute(
        "UPDATE workflow_steps SET status='submitted', summary=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=?",
        (summary, now, wid, step_id),
    )
    if step["run_id"]:
        try:
            runs_service.set_status(conn, step["run_id"], "completed", ended_at=now)
        except Exception:  # noqa: BLE001
            pass
    if step["gate"]:
        # 审核门条目由收件箱聚合（kind=gate），这里只记时间线，不重复发 mention
        conn.execute(
            "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (wf["task_id"], role, "step",
             jdumps({"step": step_id, "title": step["title"], "status": "submitted",
                     "summary": summary}),
             now),
        )
        conn.execute("UPDATE workflows SET updated_at=? WHERE id=?", (now, wid))
    else:
        conn.execute(
            "UPDATE workflow_steps SET status='approved', ended_at=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?", (now, now, wid, step_id))
    conn.commit()
    _finalize_if_done(conn, wid)
    return get_workflow(conn, wid)  # type: ignore[return-value]


def review_step(conn: sqlite3.Connection, wid: str, step_id: str, approve: bool,
                feedback: str | None = None) -> dict:
    """人工审核 gate 步骤：通过则解锁下游；驳回则回到 pending 等待角色重做。"""
    wf = get_workflow(conn, wid)
    if wf is None:
        raise LookupError(wid)
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None:
        raise LookupError(step_id)
    if step["status"] != "submitted":
        raise ConflictError(f"step {step_id} is {step['status']}, expected submitted")
    now = now_iso()
    if approve:
        conn.execute(
            "UPDATE workflow_steps SET status='approved', ended_at=?, updated_at=? "
            "WHERE workflow_id=? AND step_id=?", (now, now, wid, step_id))
    else:
        conn.execute(
            "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
            "summary=NULL, updated_at=? WHERE workflow_id=? AND step_id=?",
            (now, wid, step_id))
        _mention(conn, wf["task_id"], step["role_slug"],
                 f"步骤「{step['title']}」被驳回：{feedback or '请按评审意见修改后重新提交'}。")
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def retry_step(conn: sqlite3.Connection, wid: str, step_id: str) -> dict:
    """人工重试失败/驳回的步骤：重置为 pending，等待调度。"""
    now = now_iso()
    conn.execute(
        "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, ended_at=NULL, "
        "summary=NULL, updated_at=? WHERE workflow_id=? AND step_id=?", (now, wid, step_id))
    conn.commit()
    return get_workflow(conn, wid)  # type: ignore[return-value]


def fail_step(conn: sqlite3.Connection, wid: str, step_id: str, reason: str) -> None:
    """run 失败/失联时标记步骤失败并通知人类。"""
    wf = get_workflow(conn, wid)
    if wf is None:
        return
    step = next((s for s in wf["steps"] if s["step_id"] == step_id), None)
    if step is None or step["status"] != "running":
        return
    conn.execute(
        "UPDATE workflow_steps SET status='failed', summary=?, ended_at=?, updated_at=? "
        "WHERE workflow_id=? AND step_id=?", (reason, now_iso(), now_iso(), wid, step_id))
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (wf["task_id"], "workflow", "mention",
         jdumps({"target": "human",
                 "message": f"工作流步骤「{step['title']}」（{step['role_slug']}）执行失败：{reason}。"
                            "可在任务详情重试该步骤。",
                 "event": "step_failed", "workflow_id": wid, "step_id": step_id}),
         now_iso()),
    )
    conn.commit()


def gates(conn: sqlite3.Connection) -> list[dict]:
    """待人工审核的 gate 步骤（供收件箱聚合）。"""
    rows = conn.execute(
        """SELECT s.workflow_id, s.step_id, s.title, s.role_slug, s.summary, s.updated_at,
                  w.task_id, t.title AS task_title
             FROM workflow_steps s
             JOIN workflows w ON w.id = s.workflow_id
             LEFT JOIN tasks t ON t.id = w.task_id
            WHERE s.gate=1 AND s.status='submitted' AND w.status='running'
            ORDER BY s.updated_at"""
    ).fetchall()
    return [dict(r) for r in rows]


def _finalize_if_done(conn: sqlite3.Connection, wid: str) -> None:
    """所有步骤完成 → workflow done，任务进入最终人工评审。"""
    wf = get_workflow(conn, wid)
    if wf is None or wf["status"] != "running":
        return
    if not all(s["status"] in STEP_DONE for s in wf["steps"]):
        return
    now = now_iso()
    conn.execute("UPDATE workflows SET status='done', updated_at=? WHERE id=?", (now, wid))
    conn.execute(
        "UPDATE tasks SET status='in_review', lease_expires_at=NULL, updated_at=? "
        "WHERE id=? AND status IN ('in_progress','todo')", (now, wf["task_id"]))
    _mention(conn, wf["task_id"], "human", "工作流全部步骤完成，请做最终评审（通过/驳回）")
    conn.commit()


def _mention(conn, task_id: str, target: str, message: str) -> None:
    from . import tasks as tasks_service
    tasks_service.mention(conn, task_id, "workflow", target, message)
