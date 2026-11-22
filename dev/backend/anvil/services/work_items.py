"""Durable work item 统一读取接口（P0.6）。

四类队列仍是各自表的权威，处理语义不变：
- wake_requests：跨进程唤醒（scheduler 解析目标后投递）
- reminders：角色定时/条件提醒（daemon 触发）
- action_cards：Agent 起草、人类裁决的动作卡
- board_drafts：任务/工作流草案（人类审批）

本模块只归一化读取契约（kind/status/pending/payload/attempts），不合并表。
"""
from __future__ import annotations

import sqlite3

from ..util import jloads

KINDS = ("wake", "reminder", "action_card", "board_draft")


def _item(*, kind: str, id_, status: str,
          role_slug: str | None = None, task_id: str | None = None,
          created_at: str | None = None, due_at: str | None = None,
          attempts: int = 0, summary: str = "", payload: dict | None = None) -> dict:
    return {
        "kind": kind,
        "id": str(id_),
        "status": status,
        "pending": status == "pending",
        "role_slug": role_slug,
        "task_id": task_id,
        "attempts": int(attempts or 0),
        "created_at": created_at,
        "due_at": due_at,
        "summary": summary or "",
        "payload": payload or {},
    }


def _wake(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, task_id, role_slug, workflow_id, step_id, slice_key, reason, "
        "note, status, attempts, created_at FROM wake_requests").fetchall()
    for r in rows:
        yield _item(
            kind="wake", id_=r["id"], status=r["status"],
            role_slug=r["role_slug"], task_id=r["task_id"],
            created_at=r["created_at"], attempts=r["attempts"],
            summary=f"{r['reason']}：{r['note']}",
            payload={"reason": r["reason"], "note": r["note"],
                     "workflow_id": r["workflow_id"], "step_id": r["step_id"],
                     "slice_key": r["slice_key"]})


def _reminders(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, role_slug, kind, cron_expr, fire_at, when_condition, prompt, "
        "status, next_fire_at, created_at FROM reminders").fetchall()
    for r in rows:
        yield _item(
            kind="reminder", id_=r["id"], status=r["status"],
            role_slug=r["role_slug"], created_at=r["created_at"],
            due_at=r["next_fire_at"] or r["fire_at"],
            summary=r["prompt"] or r["kind"],
            payload={"kind": r["kind"], "cron_expr": r["cron_expr"],
                     "fire_at": r["fire_at"],
                     "when_condition": r["when_condition"],
                     "prompt": r["prompt"]})


def _action_cards(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, role_slug, task_id, kind, payload_json, reason, status, "
        "created_at FROM action_cards").fetchall()
    for r in rows:
        payload = jloads(r["payload_json"], {})
        if r["reason"]:
            payload = {**payload, "reason": r["reason"]}
        yield _item(
            kind="action_card", id_=r["id"], status=r["status"],
            role_slug=r["role_slug"], task_id=r["task_id"],
            created_at=r["created_at"], summary=r["kind"], payload=payload)


def _board_drafts(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, workspace_id, goal, status, created_by, created_at "
        "FROM board_drafts").fetchall()
    for r in rows:
        yield _item(
            kind="board_draft", id_=r["id"], status=r["status"],
            created_at=r["created_at"], summary=r["goal"],
            payload={"goal": r["goal"], "workspace_id": r["workspace_id"],
                     "created_by": r["created_by"]})


_BUILDERS = {
    "wake": _wake,
    "reminder": _reminders,
    "action_card": _action_cards,
    "board_draft": _board_drafts,
}


def list_items(conn: sqlite3.Connection, *, kinds: list[str] | None = None,
               pending_only: bool = True, role: str | None = None,
               task_id: str | None = None, limit: int = 500) -> dict:
    """按统一契约列出 durable work items。"""
    selected = [k for k in (kinds or KINDS) if k in _BUILDERS]
    items: list[dict] = []
    for kind in selected:
        items.extend(_BUILDERS[kind](conn))
    if pending_only:
        items = [i for i in items if i["pending"]]
    if role:
        items = [i for i in items if i["role_slug"] == role]
    if task_id:
        items = [i for i in items if i["task_id"] == task_id]
    items.sort(key=lambda i: (i["created_at"] or "", i["kind"], i["id"]),
               reverse=True)
    items = items[:max(1, min(int(limit), 2000))]
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
    counts["total"] = len(items)
    return {"items": items, "counts": counts}
