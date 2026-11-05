"""解释一个任务起始上下文会带哪些知识（与 CIC/投影同一口径）。"""
from __future__ import annotations

import sqlite3

from . import knowledge_binding, knowledge_repos, memory, repo_knowledge, roles
from .tasks import get as get_task


def explain_task(conn: sqlite3.Connection, task_id: str, *,
                 role: str | None = None) -> dict:
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    scope = knowledge_binding.retrieval_scope(conn, task)
    bindings = knowledge_repos.list_task_bindings(conn, task_id)
    repo_ids = []
    repo_cards = []
    for binding in bindings:
        if (binding.get("scope") or "shared") != "shared":
            continue
        row = knowledge_repos.get(conn, binding["repo"])
        if row is None:
            continue
        repo_ids.append(row["id"])
        for card in repo_knowledge.list_cards(
                conn, repo=row["id"], status="approved", limit=8):
            repo_cards.append({
                "id": card.get("id"), "repo": card.get("repo"),
                "title": card.get("title"), "tldr": card.get("tldr"),
                "tags": card.get("tags") or [],
                "authority": card.get("authority"),
                "score": card.get("hit_count"),
            })
    principles = ""
    capabilities = ""
    if role:
        principles = memory.read_principles(role)
        capabilities = roles.capability_summary(conn, role)
    return {
        "task_id": task_id,
        "task_title": task.get("title"),
        "knowledge_mode": task.get("knowledge_mode"),
        "primary_repo": task.get("repo"),
        "retrieval_scope": scope,
        "bindings": bindings,
        "topic_repo_cards": repo_cards,
        "principles_chars": len(principles),
        "capabilities_chars": len(capabilities),
        "notes": "topic_repo_cards 为已批准卡片；任务级 local 卡片见 task_knowledge。",
    }
