"""子任务统一投影（P0.4）。

delegation（阻塞、父 step 验收）与 dispatch（非阻塞、完成后回切）的行为差异
保留在各自服务中；这里只把两类子任务归一化成同一条 child 列表，供 API 与 UI
共用一套字段和状态词表。权威数据仍分别在 tasks / task_dispatches。
"""
from __future__ import annotations

import sqlite3

from . import dispatch as dispatch_service
from . import tasks as tasks_service

RELATION_DELEGATION = "delegation"
RELATION_DISPATCH = "dispatch"
DISPATCH_TASK_TYPES = ("dispatch", "dispatched")


def _latest_run(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT id, status, failure_class, failure_message, suspend_kind "
        "FROM runs WHERE task_id=? "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()


def _submitted_summary(conn: sqlite3.Connection, task_id: str):
    row = conn.execute(
        "SELECT COALESCE("
        "  (SELECT c.summary FROM task_role_claims c "
        "    WHERE c.task_id=? AND c.summary IS NOT NULL "
        "    ORDER BY c.submitted_at DESC LIMIT 1),"
        "  (SELECT json_extract(a.payload_json, '$.summary') FROM audit a "
        "    WHERE a.task_id=? AND a.action='submit' ORDER BY a.id DESC LIMIT 1)"
        ") AS summary", (task_id, task_id)).fetchone()
    return row["summary"] if row is not None else None


def _step_title(conn: sqlite3.Connection, workflow_id, step_id):
    if not workflow_id or not step_id:
        return None
    row = conn.execute(
        "SELECT title FROM workflow_steps WHERE workflow_id=? AND step_id=?",
        (workflow_id, step_id)).fetchone()
    return row["title"] if row is not None else None


def _base(*, relation_type: str, relation_id, task_id: str, title, status, type_,
          assignee, assignees: list, created_by, parent_step_id, step_title,
          due_at, acceptance: list, scope_paths: list, description,
          submitted_summary, run, created_at, updated_at, blocking: bool,
          dispatch_status, return_pending: bool, return_context_key,
          returned_at, raw: dict) -> dict:
    return {
        "relation_type": relation_type,
        "relation_id": relation_id,
        "task_id": task_id,
        "title": title,
        "status": status,
        "type": type_,
        "assignee": assignee,
        "assignees": assignees,
        "created_by": created_by,
        "parent_step_id": parent_step_id,
        "step_title": step_title,
        "due_at": due_at,
        "acceptance": acceptance,
        "scope_paths": scope_paths,
        "description": description,
        "submitted_summary": submitted_summary,
        "run_id": run["id"] if run is not None else None,
        "run_status": run["status"] if run is not None else None,
        "run_failure_class": run["failure_class"] if run is not None else None,
        "run_failure_message": (run["failure_message"]
                                if run is not None and "failure_message" in run.keys()
                                else None),
        "run_suspend_kind": (run["suspend_kind"]
                             if run is not None and "suspend_kind" in run.keys()
                             else None),
        "blocking": blocking,
        "dispatch_status": dispatch_status,
        "return_pending": return_pending,
        "return_context_key": return_context_key,
        "returned_at": returned_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": raw,
    }


def _delegation_child(d: dict) -> dict:
    # list_delegations 已带最新 run 的三列，避免重复查询。
    run = None
    if d.get("run_id"):
        run = {"id": d["run_id"], "status": d.get("run_status"),
               "failure_class": d.get("run_failure_class"),
               "failure_message": d.get("run_failure_message"),
               "suspend_kind": d.get("run_suspend_kind")}
    return _base(
        relation_type=RELATION_DELEGATION,
        relation_id=None,
        task_id=d["id"],
        title=d.get("title"),
        status=d.get("status"),
        type_=d.get("type"),
        assignee=d.get("assignee"),
        assignees=d.get("assignees") or [],
        created_by=d.get("created_by"),
        parent_step_id=d.get("parent_step_id"),
        step_title=d.get("step_title"),
        due_at=d.get("due_at"),
        acceptance=d.get("acceptance") or [],
        scope_paths=d.get("scope_paths") or [],
        description=d.get("description"),
        submitted_summary=d.get("submitted_summary"),
        run=run,
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        blocking=True,
        dispatch_status=None,
        return_pending=False,
        return_context_key=None,
        returned_at=None,
        raw=d,
    )


def _dispatch_child(conn: sqlite3.Connection, d: dict) -> dict:
    task = tasks_service.get(conn, d["target_task_id"]) or {}
    run = _latest_run(conn, d["target_task_id"])
    status = d.get("status")
    return _base(
        relation_type=RELATION_DISPATCH,
        relation_id=d.get("id"),
        task_id=d["target_task_id"],
        title=task.get("title") or d.get("target_title") or d["target_task_id"],
        status=task.get("status") or d.get("target_status"),
        type_=task.get("type") or "dispatch",
        assignee=task.get("assignee") or d.get("target_role"),
        assignees=task.get("assignees") or (
            [d["target_role"]] if d.get("target_role") else []),
        created_by=task.get("created_by"),
        parent_step_id=d.get("source_step_id"),
        step_title=_step_title(conn, d.get("source_workflow_id"),
                               d.get("source_step_id")),
        due_at=task.get("due_at"),
        acceptance=task.get("acceptance") or [],
        scope_paths=task.get("scope_paths") or [],
        description=task.get("description"),
        submitted_summary=_submitted_summary(conn, d["target_task_id"]),
        run=run,
        created_at=task.get("created_at") or d.get("created_at"),
        updated_at=task.get("updated_at") or d.get("updated_at"),
        blocking=False,
        dispatch_status=status,
        return_pending=status in ("return_ready", "returning"),
        return_context_key=d.get("return_context_key"),
        returned_at=d.get("returned_at"),
        raw=d,
    )


def list_children(conn: sqlite3.Connection, parent_id: str) -> dict:
    """父任务下所有委派/派遣子任务的统一投影（不含 plain 子任务）。"""
    delegations = tasks_service.list_delegations(conn, parent_id)
    dispatches = dispatch_service.list_dispatches(conn, parent_id)
    items = [_delegation_child(d) for d in delegations]
    items.extend(_dispatch_child(conn, d) for d in dispatches)
    items.sort(key=lambda item: (item.get("created_at") or "", item["task_id"]))
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "delegation": len(delegations),
            "dispatch": len(dispatches),
        },
    }
