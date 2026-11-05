"""任务执行经验捕获：失败/返工/评审驳回/完成 → 任务级或主题知识草稿。

原则：
- 只生成 `proposed` 草稿；人工批准后才生效；
- 只读任务时间线/步骤反馈/评审结论，不读角色私有 notes/MEMORY；
- 幂等：`knowledge_captures(task_id, trigger, trigger_key)` 唯一；
- 默认开启，可在「工具」页或 `ANVIL_EXPERIENCE_CAPTURE=0` 关闭。
"""
from __future__ import annotations

import hashlib
import sqlite3

from .. import config
from ..util import jdumps, jloads, new_id, now_iso
from . import knowledge_repos, repo_knowledge, task_knowledge


def _enabled() -> bool:
    return bool(config.EXPERIENCE_CAPTURE_ENABLED)


def _task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def _tags(task: dict) -> list[str]:
    return [str(t) for t in jloads(task.get("tags_json") or "[]", [])]


def _binding(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT repo, repo_id, scope FROM task_knowledge_repos "
        "WHERE task_id=? AND status='applied' "
        "ORDER BY is_primary DESC, score DESC, repo LIMIT 1",
        (task_id,)).fetchone()
    return dict(row) if row is not None else None


def _claim(conn: sqlite3.Connection, task_id: str, trigger: str,
           trigger_key: str, target: str) -> tuple[str, bool]:
    """写入幂等账本；已存在返回 (existing_id, False)。"""
    key = str(trigger_key or "none")
    existing = conn.execute(
        "SELECT id FROM knowledge_captures WHERE task_id=? AND trigger=? AND trigger_key=?",
        (task_id, trigger, key)).fetchone()
    if existing is not None:
        return str(existing["id"]), False
    cid = new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO knowledge_captures "
        "(id, task_id, trigger, trigger_key, target, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cid, task_id, trigger, key, target, "proposed", now, now))
    return cid, True


_EFFECT_BY_TRIGGER = {
    "completion": ("复用本次任务结论，减少同类任务的重复探索。", "reduce_rework"),
    "failure": ("避免同类失败再次发生，缩短定位和恢复时间。", "avoid_error"),
    "rework": ("避免同类评审驳回和返工，提前对齐验收口径。", "reduce_rework"),
    "review_reject": ("对齐评审口径，减少再次驳回。", "reduce_rework"),
}


def _capture_fields(task: dict, kind: str, body: str, trigger: str,
                    source_step_id: str | None) -> dict:
    tags = _tags(task)
    use_when = "、".join(tags) or str(task.get("title") or "同类任务")
    effect, effect_type = _EFFECT_BY_TRIGGER.get(
        trigger, ("沉淀可复用经验，减少重复探索。", "quality"))
    evidence_bits = [f"任务 {task['id']}"]
    if source_step_id:
        evidence_bits.append(f"步骤 {source_step_id}")
    if body:
        evidence_bits.append(f"任务记录：{str(body)[:500]}")
    evidence = "；".join(evidence_bits)
    common = {
        "use_when": use_when,
        "expected_effect": effect,
        "effect_type": effect_type,
        "evidence": evidence,
    }
    if kind == "task_finding":
        return {
            **common,
            "task_context": str(task.get("title") or task["id"]),
            "finding": str(body or task.get("title") or "")[:2000],
            "verification": "见任务时间线/评审记录；必要时复跑关键命令。",
            "reuse_scope": use_when,
        }
    if kind == "insight":
        return {
            **common,
            "observation": str(body or task.get("title") or "")[:1200],
            "lesson": str(body or task.get("title") or "")[:2000],
            "applicability": use_when,
            "counterexamples": "暂无已知反例",
        }
    return common


def _create_card(conn: sqlite3.Connection, task: dict, *,
                 kind: str, title: str, tldr: str, body: str,
                 trigger: str, trigger_key: str, source_event: str,
                 source_step_id: str | None = None,
                 actor: str | None = None) -> dict | None:
    binding = _binding(conn, task["id"])
    target = "repo" if binding and binding.get("scope") == "shared" else "task_temp"
    capture_id, fresh = _claim(conn, task["id"], trigger, trigger_key, target)
    if not fresh:
        return None
    tags = [*_tags(task), source_event]
    fields = _capture_fields(task, kind, body, trigger, source_step_id)
    try:
        if target == "repo":
            card = repo_knowledge.create(
                conn, binding["repo"], kind=kind, title=title, tldr=tldr,
                body_md=body, fields=fields, tags=tags, authority="reference",
                status="proposed", source_task_id=task["id"],
                source_step_id=source_step_id, source_role=actor,
                source_event=source_event, confidence=0.5,
                created_by=actor or "system")
            card_id = card.get("id")
        else:
            card = task_knowledge.create(
                conn, task["id"], title=title, tldr=tldr, kind=kind,
                fields=fields, tags=tags, source_role=actor,
                status="proposed")
            card_id = card.get("id")
        conn.execute(
            "UPDATE knowledge_captures SET card_id=?, updated_at=? WHERE id=?",
            (card_id, now_iso(), capture_id))
        conn.commit()
        return card
    except Exception:
        # 捕获失败不能阻断主流程；回滚账本，下次事件可重试。
        conn.execute("DELETE FROM knowledge_captures WHERE id=?", (capture_id,))
        conn.commit()
        raise


def capture_task_completion(conn: sqlite3.Connection, task_id: str, *,
                            run_id: str | None = None,
                            actor: str | None = None) -> dict | None:
    if not _enabled():
        return None
    task = _task(conn, task_id)
    if task is None:
        return None
    steps = conn.execute(
        "SELECT step_id, title, status, summary FROM workflow_steps "
        "WHERE workflow_id=(SELECT id FROM workflows WHERE task_id=? "
        "ORDER BY created_at DESC LIMIT 1) ORDER BY updated_at DESC LIMIT 12",
        (task_id,)).fetchall()
    body_lines = [
        f"- 任务：{task.get('title')}（{task_id}）",
        f"- 结果：done",
        f"- run：{run_id or '-'}",
    ]
    for st in steps:
        if st["summary"]:
            body_lines.append(
                f"- [{st['status']}] {st['step_id']} {st['title']}："
                f"{str(st['summary'])[:300]}")
    return _create_card(
        conn, task, kind="task_finding",
        title=f"[完成] {str(task.get('title') or task_id)[:70]}",
        tldr=f"任务 {task_id} 完成；可复用结论见正文与证据路径。",
        body="\n".join(body_lines), trigger="completion",
        trigger_key=str(run_id or task_id), source_event="task_done",
        actor=actor)


def capture_failure(conn: sqlite3.Connection, task_id: str, *,
                    run_id: str | None = None, step_id: str | None = None,
                    failure_class: str | None = None,
                    actor: str | None = None) -> dict | None:
    if not _enabled():
        return None
    task = _task(conn, task_id)
    if task is None:
        return None
    step = None
    if step_id:
        step = conn.execute(
            "SELECT step_id, title, failure_reason, recovery_count FROM workflow_steps "
            "WHERE step_id=? AND workflow_id=(SELECT id FROM workflows WHERE task_id=? "
            "ORDER BY created_at DESC LIMIT 1)",
            (step_id, task_id)).fetchone()
    title = f"[失败] {step_id or 'run'}"
    if step is not None and step["title"]:
        title += f" {step['title']}"
    body = "\n".join([
        f"- 任务：{task.get('title')}（{task_id}）",
        f"- 步骤：{step_id or '-'}",
        f"- 失败类型：{failure_class or '-'}",
        f"- run：{run_id or '-'}",
        f"- 失败原因：{str((step['failure_reason'] if step else '') or '')[:800]}",
        "- 处理方式：记录为候选经验，人工复核后决定是否晋升为约束/模式。",
    ])
    return _create_card(
        conn, task, kind="insight",
        title=title[:80],
        tldr=f"{failure_class or '失败'}：记录触发条件与纠正方式，待人工复核。",
        body=body, trigger="failure",
        trigger_key=f"{run_id or ''}:{step_id or ''}:{failure_class or ''}",
        source_event="failure", source_step_id=step_id, actor=actor)


def capture_rework(conn: sqlite3.Connection, task_id: str, *,
                   workflow_id: str | None = None, step_id: str | None = None,
                   feedback: str | None = None, actor: str | None = None) -> dict | None:
    if not _enabled():
        return None
    task = _task(conn, task_id)
    if task is None:
        return None
    body = "\n".join([
        f"- 任务：{task.get('title')}（{task_id}）",
        f"- workflow：{workflow_id or '-'}",
        f"- 步骤：{step_id or '-'}",
        f"- 驳回/返工反馈：{str(feedback or '')[:2000]}",
        "- 处理方式：提炼被驳回的假设与接受的修正，人工复核后生效。",
    ])
    digest = hashlib.sha1(str(feedback or "").encode("utf-8")).hexdigest()[:12]
    return _create_card(
        conn, task, kind="insight",
        title=f"[返工] {step_id or task_id}"[:80],
        tldr="评审驳回/返工：记录被拒绝的假设与修正。",
        body=body, trigger="rework",
        trigger_key=f"{workflow_id or ''}:{step_id or ''}:{digest}",
        source_event="rework", source_step_id=step_id, actor=actor)


def capture_review_rejection(conn: sqlite3.Connection, task_id: str, *,
                             reviewer: str | None = None,
                             feedback: str | None = None,
                             actor: str | None = None) -> dict | None:
    if not _enabled():
        return None
    task = _task(conn, task_id)
    if task is None:
        return None
    body = "\n".join([
        f"- 任务：{task.get('title')}（{task_id}）",
        f"- 评审人：{reviewer or 'human'}",
        f"- 驳回反馈：{str(feedback or '')[:2000]}",
        "- 处理方式：记录评审口径差异，人工复核后决定是否沉淀。",
    ])
    digest = hashlib.sha1(str(feedback or "").encode("utf-8")).hexdigest()[:12]
    return _create_card(
        conn, task, kind="insight",
        title=f"[驳回] {str(task.get('title') or task_id)[:70]}",
        tldr="任务评审驳回：记录评审口径与修正方向。",
        body=body, trigger="review_reject",
        trigger_key=str(reviewer or "human") + ":" + digest,
        source_event="review_reject", actor=actor)
