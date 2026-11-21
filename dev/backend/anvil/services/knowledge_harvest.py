"""任务完成后的知识收割提醒。

Cindy 规划完成且 human 落板 workflow 时，为顶层任务生成一条 `when(task:<id>:done)`
提醒；任务完成时由现有 governance tick 触发，Cindy 用任务上下文收割任务知识、
主题知识与频道知识。Cindy 只写 proposed/待审草稿，不自动批准。
"""
from __future__ import annotations

import sqlite3

from .. import config
from . import reminders as reminders_service

HARVEST_ROLE = "cindy"
HARVEST_PREFIX = "【知识收割】"
_CONDITION = "task:{task_id}:done"


def _enabled() -> bool:
    return bool(getattr(config, "KNOWLEDGE_HARVEST_REMINDER_ENABLED", True))


def build_prompt(task: dict) -> str:
    tid = str(task.get("id") or "")
    title = str(task.get("title") or "")
    workspace = str(task.get("workspace_id") or "").strip()
    repo = str(task.get("repo") or "").strip()
    channel_line = workspace or "未绑定（跳过频道知识步骤）"
    repo_line = repo or f"未绑定主主题（可用 `anvil knowledge explain --task {tid}` 查看自动绑定主题）"
    if repo:
        topic_step = f"""3. 主题知识：任务主主题是 {repo}。先 `anvil knowledge repo-cards --repo {repo}` 去重，
   再用 `anvil knowledge card-template --kind task_finding` 生成 JSON，
   `anvil knowledge card-create {repo} --file card.json` 写 proposed；
   若任务还绑定了其他主题，用实际主题名替换 REPO_NAME 后重复。"""
    else:
        topic_step = f"""3. 主题知识：先用 `anvil knowledge explain --task {tid}` 查看自动绑定主题；
   对每个强相关主题，把 REPO_NAME 换成实际主题名，先 `anvil knowledge repo-cards --repo REPO_NAME`
   去重，再用 `anvil knowledge card-template --kind task_finding` 生成 JSON，
   `anvil knowledge card-create REPO_NAME --file card.json` 写 proposed。"""
    if workspace:
        channel_step = f"""4. 频道知识：任务频道 id 是 {workspace}。先 `anvil workspace knowledge list {workspace}` 去重；
   `anvil workspace knowledge harvest {workspace} {tid}` 可生成确定性草稿，再按需用
   `anvil workspace knowledge propose {workspace} --title "..." --tldr "..." --kind convention --fields '{{"rule":"...","scope":"...","rationale":"..."}}'`
   补充频道级约定/流程/决策。"""
    else:
        channel_step = "4. 频道知识：任务未绑定频道，跳过。"
    return f"""{HARVEST_PREFIX}任务 {tid} 完成后触发知识收割。请用任务上下文整理可复用知识；只写 proposed/待审草稿，禁止 approve，审批由 human 或任务参与者完成。

任务：{title}
频道：{channel_line}
主主题 repo：{repo_line}

执行步骤：
1. 先读证据：`anvil task show {tid} --json`、`anvil task knowledge-list {tid} --include-subtasks`；查看任务描述、验收、步骤摘要、run 结论和已有知识。只依据任务时间线/步骤/run/产物，不要编造。
2. 任务知识：把仅对本任务有效的发现/决策整理为 task_finding/decision，用
   `anvil task knowledge-write {tid} --title "..." --tldr "..." --kind task_finding --fields '{{"task_context":"...","finding":"...","verification":"...","reuse_scope":"...","use_when":"...","expected_effect":"...","evidence":"..."}}'`
   写入；你的写入会以 proposed 落库（任务参与者/human 批准后生效）。已有同类卡先更新或跳过。
{topic_step}
{channel_step}
5. 每条卡都要有 use_when、expected_effect、evidence 和对应 kind 的必填字段；重复时合并/更新而不是新建；没有可复用知识就结束并说明“无新增”。
"""


def _is_auto_reminder(row) -> bool:
    """只有带固定前缀的才是本功能创建、可自动删除的提醒。"""
    return str(row["prompt"] or "").startswith(HARVEST_PREFIX)


def _mentions_harvest(row) -> bool:
    """已存在的人工提醒只要语义相关就不重复创建，但删除时不动它。"""
    prompt = str(row["prompt"] or "")
    return _is_auto_reminder(row) or ("知识" in prompt and "收割" in prompt)


def _existing(conn: sqlite3.Connection, task_id: str):
    rows = conn.execute(
        "SELECT * FROM reminders WHERE kind='when' AND when_condition=? "
        "ORDER BY created_at, rowid",
        (_CONDITION.format(task_id=task_id),),
    ).fetchall()
    for row in rows:
        if _mentions_harvest(row):
            return row
    return None


def ensure_task_reminder(conn: sqlite3.Connection, task: dict, *,
                         commit: bool = False) -> dict | None:
    """幂等确保任务有一条知识收割提醒；子任务/功能关闭/角色不可用时跳过。"""
    if not _enabled() or task.get("parent_id"):
        return None
    if conn.execute(
            "SELECT 1 FROM roles WHERE slug=? AND enabled=1", (HARVEST_ROLE,)
    ).fetchone() is None:
        return None
    task_id = str(task.get("id") or "")
    existing = _existing(conn, task_id)
    if existing is not None:
        # 刷新自动提醒的 prompt（标题/频道/repo 可能变化）；人工自定义提示不覆盖。
        prompt = build_prompt(task)
        if _is_auto_reminder(existing) and existing["prompt"] != prompt:
            conn.execute("UPDATE reminders SET prompt=? WHERE id=?",
                         (prompt, existing["id"]))
            existing = conn.execute(
                "SELECT * FROM reminders WHERE id=?", (existing["id"],)).fetchone()
        return dict(existing)
    reminder = reminders_service.create_reminder(
        conn, role_slug=HARVEST_ROLE, kind="when",
        when_condition=_CONDITION.format(task_id=task_id),
        prompt=build_prompt(task), commit=False)
    if commit:
        conn.commit()
    return reminder


def ensure_task_reminder_by_id(conn: sqlite3.Connection, task_id: str, *,
                               commit: bool = False) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    return ensure_task_reminder(conn, dict(row), commit=commit)


def remove_task_reminder(conn: sqlite3.Connection, task_id: str, *,
                         commit: bool = False) -> int:
    """只删除自动知识收割提醒，保留人工创建的同条件提醒。"""
    rows = conn.execute(
        "SELECT id, prompt FROM reminders WHERE kind='when' AND when_condition=?",
        (_CONDITION.format(task_id=task_id),),
    ).fetchall()
    removed = 0
    for row in rows:
        if _is_auto_reminder(row):
            conn.execute("DELETE FROM reminders WHERE id=?", (row["id"],))
            removed += 1
    if commit:
        conn.commit()
    return removed


def _task_id_from_condition(condition: str | None) -> str | None:
    value = str(condition or "")
    if not value.startswith("task:"):
        return None
    parts = value.split(":")
    return parts[1] if len(parts) > 1 and parts[1] else None


def _cleanup_orphans(conn: sqlite3.Connection) -> int:
    """删除不再成立的自动提醒：任务已删除/关闭，或提醒挂在子任务上。"""
    rows = conn.execute(
        "SELECT id, when_condition, prompt FROM reminders WHERE kind='when'"
    ).fetchall()
    removed = 0
    for row in rows:
        if not _is_auto_reminder(row):
            continue
        task_id = _task_id_from_condition(row["when_condition"])
        if not task_id:
            continue
        task = conn.execute(
            "SELECT status, parent_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if (task is None or task["parent_id"] or task["status"] == "closed"):
            conn.execute("DELETE FROM reminders WHERE id=?", (row["id"],))
            removed += 1
    return removed


def backfill(conn: sqlite3.Connection) -> int:
    """仅给已落板 workflow 的活动顶层任务补提醒；历史 done 不追溯触发。"""
    removed = _cleanup_orphans(conn)
    rows = conn.execute(
        "SELECT t.* FROM tasks t WHERE t.parent_id IS NULL AND t.archived_at IS NULL "
        "AND t.status IN ('todo','in_progress','in_review') "
        "AND EXISTS (SELECT 1 FROM workflows w WHERE w.task_id=t.id) "
        "ORDER BY t.created_at"
    ).fetchall()
    created = 0
    for row in rows:
        before = _existing(conn, row["id"])
        # 即使已有提醒也调用 ensure，顺带刷新自动提示文案/任务元数据。
        after = ensure_task_reminder(conn, dict(row))
        if before is None and after is not None:
            created += 1
    if rows or removed:
        conn.commit()
    return created
