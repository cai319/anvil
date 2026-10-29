"""Inbox 聚合（信息架构重构）：四类人机决策点的统一只读入口。

条目只在这里聚合；操作复用现有端点（task review / board approve / lessons / proposals）。
"""
from __future__ import annotations

import sqlite3

from ..util import jloads

# 系统伪发送者：workflow 引擎/系统通知不是真实 Agent 角色。
SYSTEM_SENDERS = {"workflow": "工作流引擎", "system": "系统"}


def collect(conn: sqlite3.Connection) -> dict:
    """聚合待人类处理条目：review / mention / draft / lesson / proposal，created_at 倒序。"""
    items: list[dict] = []
    items.extend(_reviews(conn))
    items.extend(_mentions(conn))
    items.extend(_drafts(conn))
    items.extend(_lessons(conn))
    items.extend(_proposals(conn))
    items.extend(_workflow_gates(conn))
    items.extend(_workflow_decisions(conn))
    items.extend(_action_cards(conn))
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    return {"count": len(items), "items": items}


def _reviews(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, title, updated_at FROM tasks WHERE status='in_review' ORDER BY updated_at"
    ).fetchall():
        roles = [x["role_slug"] for x in conn.execute(
            "SELECT role_slug FROM task_role_claims WHERE task_id=? AND status IN ('submitted','approved')",
            (r["id"],),
        )]
        detail = "、".join(roles) + " 已提交 · 等你通过或驳回" if roles else "等你通过或驳回"
        out.append({
            "id": f"review:{r['id']}", "kind": "review", "title": r["title"],
            "detail": detail, "task_id": r["id"], "created_at": r["updated_at"],
        })
    return out


def _mentions(conn: sqlite3.Connection) -> list[dict]:
    """@human 提及：每任务取最新一条；其后有评论/评审（已回应）则视为已处理。"""
    rows = conn.execute(
        """SELECT a.id AS aid, a.task_id, a.role_slug, a.payload_json, a.created_at, t.title
             FROM activities a JOIN tasks t ON t.id = a.task_id
            WHERE a.kind='mention' AND json_extract(a.payload_json, '$.target')='human'
            ORDER BY a.id"""
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        latest[r["task_id"]] = r  # 同任务后一条覆盖前一条
    out = []
    for tid, r in latest.items():
        handled = conn.execute(
            "SELECT 1 FROM activities WHERE task_id=? AND id>? AND ("
            "kind IN ('comment','review') OR (kind='mention' AND role_slug='human')) "
            "LIMIT 1",
            (tid, r["aid"]),
        ).fetchone()
        # 工作流草案在该提及之后被批准 → 也算已回应（Cindy 的「请审阅批准」不应长期滞留）
        approved = conn.execute(
            "SELECT 1 FROM board_drafts WHERE source_task=? AND status='approved' "
            "AND decided_at IS NOT NULL AND decided_at > ? LIMIT 1",
            (tid, r["created_at"]),
        ).fetchone()
        if handled or approved:
            continue
        # A done task has no agent waiting for a human reply. Info-only mentions
        # (e.g. "planning complete, no further workflow changes") stay in the
        # timeline but must not linger in the actionable inbox.
        task_status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        if task_status and task_status["status"] == "done":
            continue
        p = jloads(r["payload_json"], {})
        msg = str(p.get("message", ""))
        if p.get("event") == "role_suggestion_rejected":
            bound = conn.execute(
                "SELECT 1 FROM task_assignees WHERE task_id=? AND joined_at > ? LIMIT 1",
                (tid, r["created_at"]),
            ).fetchone()
            if bound:
                continue  # human 已绑定角色，提示不再滞留
        if p.get("event") == "rework_exhausted":
            st = conn.execute(
                "SELECT status FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (p.get("workflow_id"), p.get("step_id")),
            ).fetchone()
            if st and st["status"] != "failed":
                continue  # 已 retry/重新决策，升级提醒不再滞留
        if r["role_slug"] == "workflow" and "工作流全部步骤完成" in msg:
            tst = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            if tst and tst["status"] in ("in_review", "done"):
                continue
        if p.get("event") == "step_failed" or "执行失败" in msg:
            if p.get("event") == "step_failed":
                step = conn.execute(
                    "SELECT status FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                    (p.get("workflow_id"), p.get("step_id")),
                ).fetchone()
                resolved = step is None or step["status"] != "failed"
            else:
                # 历史记录没有结构化字段：任务当前没有任何 failed 步骤即可视为已解决
                failed = conn.execute(
                    "SELECT 1 FROM workflow_steps s JOIN workflows w ON w.id=s.workflow_id "
                    "WHERE w.task_id=? AND s.status='failed' LIMIT 1", (tid,)
                ).fetchone()
                resolved = failed is None
            if resolved:
                continue  # 已重试/已提交/已通过，失败通知不再滞留
        sender = r["role_slug"] or "human"
        is_system = sender in SYSTEM_SENDERS
        who = SYSTEM_SENDERS.get(sender, sender)
        out.append({
            "id": f"mention:{tid}:{r['aid']}", "kind": "mention",
            "title": f"{who}：{p.get('message', '')}",
            "detail": "系统通知，请处理" if is_system
            else "需要你回复后 agent 才继续",
            "task_id": tid,
            "created_at": r["created_at"],
        })
    return out


def _drafts(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, goal, tree_json, created_by, source_task, created_at, kind "
        "FROM board_drafts WHERE status='pending' ORDER BY created_at"
    ).fetchall():
        tree = jloads(r["tree_json"], {})
        kind = r["kind"] or "tasks"
        meta: dict = {"draft_kind": kind}
        raw_repos = tree.get("knowledge_repos") or []
        meta["tags"] = tree.get("tags") or []
        meta["knowledge_repos"] = [
            {"repo": x.get("repo") or x.get("name") or str(x),
             "reason": x.get("reason") if isinstance(x, dict) else None}
            for x in raw_repos if isinstance(x, (str, dict))
        ]
        if r["source_task"]:
            row = conn.execute(
                "SELECT knowledge_repos_locked FROM tasks WHERE id=?",
                (r["source_task"],)).fetchone()
            meta["binding_locked"] = bool(row and row["knowledge_repos_locked"])
            running = conn.execute(
                "SELECT id, execution_no FROM workflows "
                "WHERE task_id=? AND status='running' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (r["source_task"],),
            ).fetchone()
            meta["running_workflow"] = bool(running)
            meta["running_workflow_id"] = running["id"] if running else None
            meta["running_execution_no"] = running["execution_no"] if running else None
        if kind == "role_suggestion":
            roles = tree.get("suggested_roles") or tree.get("roles") or []
            missing = tree.get("missing_capabilities") or []
            detail = f"by {r['created_by']} · 建议 {len(roles)} 个角色"
            if missing:
                detail += " · 缺失能力：" + "、".join(str(x) for x in missing[:6])
            meta.update({
                "assessment": tree.get("assessment") or "",
                "missing_capabilities": missing,
                "suggested_roles": [
                    {"slug": x.get("slug"), "display_name": x.get("display_name"),
                     "description": x.get("description")}
                    for x in roles if isinstance(x, dict)
                ],
            })
            title = f"角色建议 — {r['goal']}"
        elif kind == "workflow_revision":
            impact = tree.get("impact") or {}
            detail = f"by {r['created_by']} · {impact.get('summary') or '增量修订'}"
            meta["workflow_revision"] = {
                "draft_id": r["id"],
                "base_workflow_id": tree.get("base_workflow_id"),
                "impact": impact,
            }
            title = f"工作流修订 — {r['goal']}"
        elif kind == "workflow":
            n = len(tree.get("steps") or [])
            detail = f"by {r['created_by']} · {n} 个步骤 · 批准后按依赖自动派发"
            meta["step_count"] = n
            title = f"工作流草案 — {r['goal']}"
        else:
            n = len(tree.get("children") or [])
            detail = f"by {r['created_by']} · {n} 个子任务 · 批准后按依赖自动派发"
            title = f"工作流草案 — {r['goal']}"
        out.append({
            "id": f"draft:{r['id']}", "kind": "draft", "title": title,
            "detail": detail, "task_id": r["source_task"], "created_at": r["created_at"],
            "meta": meta,
        })
    return out


def _action_cards(conn: sqlite3.Connection) -> list[dict]:
    """Agent 准备的危险/管理操作：等待自然人一键执行或拒绝。"""
    from . import actions as actions_service

    out = []
    for c in actions_service.pending_for_inbox(conn):
        out.append({
            "id": f"action:{c['id']}", "kind": "action",
            "title": f"操作申请：{c['label']}",
            "detail": f"by {c['role_slug']} · 任务 {c['task_id']}"
                      + (f" · {c['reason'][:50]}" if c.get("reason") else ""),
            "task_id": c["task_id"], "created_at": c["created_at"],
        })
    return out


def _workflow_gates(conn: sqlite3.Connection) -> list[dict]:
    """工作流 gate 步骤：角色已提交，等待自然人审核。"""
    from . import workflow as workflow_service

    from . import reviews as review_service
    from datetime import datetime, timezone

    out = []
    for r in workflow_service.gates(conn):
        packet = review_service.latest_packet(conn, r["workflow_id"], r["step_id"])
        waiting = 0
        if packet and packet.get("created_at"):
            try:
                start = datetime.fromisoformat(str(packet["created_at"]))
                waiting = max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
            except (TypeError, ValueError):
                waiting = 0
        question = (packet or {}).get("question") or ""
        out.append({
            "id": f"gate:{r['workflow_id']}:{r['step_id']}", "kind": "gate",
            "title": f"步骤审核：{r['title']}",
            "detail": question or (
                f"{r['role_slug']} 已提交 · {r.get('task_title') or ''}"
                + (f" · {r['summary'][:40]}" if r.get("summary") else "")),
            "task_id": r.get("task_id"), "created_at": r["updated_at"],
            "meta": {
                "workflow_id": r["workflow_id"], "step_id": r["step_id"],
                "packet_id": (packet or {}).get("id"),
                "authority": (packet or {}).get("authority") or "human",
                "question": question,
                "evidence_count": len((packet or {}).get("evidence") or []),
                "waiting_seconds": waiting,
                "deadline_at": (packet or {}).get("deadline_at"),
            },
        })
    return out


def _workflow_decisions(conn: sqlite3.Connection) -> list[dict]:
    """Decision steps waiting for a human (human or joint authority)."""
    from . import reviews as review_service
    from datetime import datetime, timezone

    out = []
    for p in review_service.human_decision_items(conn):
        waiting = 0
        if p.get("created_at"):
            try:
                start = datetime.fromisoformat(str(p["created_at"]))
                waiting = max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
            except (TypeError, ValueError):
                waiting = 0
        recommendation = p.get("recommendation") or {}
        detail = p.get("question") or f"{p.get('role_slug')} 的决策等待人工确认"
        if recommendation.get("decision"):
            detail += f" · AI 建议：{recommendation['decision']}"
            if recommendation.get("feedback"):
                detail += f"（{str(recommendation['feedback'])[:60]}）"
        out.append({
            "id": f"decision:{p['workflow_id']}:{p['step_id']}", "kind": "decision",
            "title": f"待决策：{p.get('title') or p['step_id']}",
            "detail": detail,
            "task_id": p.get("task_id"), "created_at": p["created_at"],
            "meta": {
                "workflow_id": p["workflow_id"], "step_id": p["step_id"],
                "packet_id": p["id"],
                "authority": p.get("authority") or "human",
                "question": p.get("question") or "",
                "evidence_count": len(p.get("evidence") or []),
                "waiting_seconds": waiting,
                "deadline_at": p.get("deadline_at"),
                "recommendation": recommendation,
                "task_title": p.get("task_title"),
            },
        })
    return out


def _lessons(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, content, tags_json, created_at FROM lessons WHERE status='pending' ORDER BY created_at"
    ).fetchall():
        tags = jloads(r["tags_json"], [])
        out.append({
            "id": f"lesson:{r['id']}", "kind": "lesson",
            "title": (r["content"] or "")[:60],
            "detail": "知识库升格提案" + (f" · 标签 {','.join(tags)}" if tags else ""),
            "task_id": None, "created_at": r["created_at"],
        })
    return out


def _proposals(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, role_slug, field, rationale, created_at FROM proposals "
        "WHERE status='pending' ORDER BY created_at"
    ).fetchall():
        detail = f"by {r['role_slug']} · 字段 {r['field']}"
        if r["rationale"]:
            detail += f" · 依据：{r['rationale'][:40]}"
        out.append({
            "id": f"proposal:{r['id']}", "kind": "proposal",
            "title": f"角色自改提案 — {r['field']}",
            "detail": detail, "task_id": None, "created_at": r["created_at"],
        })
    return out
