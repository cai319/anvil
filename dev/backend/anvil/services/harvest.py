"""知识晋升：任务级 → 主题知识库 → Channel → Shared。

所有晋升默认生成 `proposed`/`pending` 草稿，由人工批准；不直接写 shared 目录。
"""
from __future__ import annotations

import sqlite3

from . import (knowledge_quality, knowledge_repos, memory, repo_knowledge, task_knowledge,
               workspace_knowledge)
from ..util import ConflictError


def task_card_to_repo(conn: sqlite3.Connection, card_id: str, repo: str, *,
                      actor: str = "human",
                      status: str = "proposed") -> dict:
    """任务级知识卡 → 主题知识库草稿。"""
    card = task_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card.get("status") not in ("active", "proposed"):
        raise ConflictError(f"task card is {card.get('status')}")
    repo_row = knowledge_repos.ensure(
        conn, repo, created_by=actor, commit=False)
    created = repo_knowledge.create(
        conn, repo_row["display_name"],
        kind=card.get("kind") or "insight",
        title=card.get("title") or "",
        tldr=card.get("tldr") or "",
        body_md=card.get("body_md") or "",
        fields=card.get("fields") or {},
        tags=card.get("tags") or [],
        authority="reference",
        status=status if status in ("proposed", "draft") else "proposed",
        source_task_id=card.get("task_id"),
        source_role=card.get("source_role"),
        source_event="harvest",
        confidence=0.6,
        created_by=actor)
    # 任务级卡归档，避免与主题卡重复注入。
    task_knowledge.archive(conn, card_id)
    return created


def _assert_approved_quality(card: dict) -> None:
    quality = knowledge_quality.evaluate_row(card, "repo_knowledge")
    if quality.get("blocking") and not quality.get("legacy"):
        raise ConflictError(
            "source repo card failed the quality gate: "
            + "; ".join(quality["blocking"]))


def repo_card_to_channel(conn: sqlite3.Connection, card_id: str,
                         workspace_id: str, *, actor: str = "human") -> dict:
    """主题知识库已批准卡 → Channel 知识草稿。"""
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card.get("status") != "approved":
        raise ConflictError("only approved repo cards can be harvested")
    _assert_approved_quality(card)
    return workspace_knowledge.create(
        conn, workspace_id,
        title=card.get("title") or "",
        tldr=card.get("tldr") or "",
        kind=card.get("kind") if card.get("kind") in {
            "convention", "decision", "workflow", "insight", "role_requirement"}
        else "insight",
        authority=card.get("authority") or "reference",
        fields=card.get("fields") or {},
        tags=card.get("tags") or [],
        source_task_id=card.get("source_task_id"),
        owner=actor,
        actor=actor,
        origin="system",
        status="proposed",
    )


def repo_card_to_shared(conn: sqlite3.Connection, card_id: str, *,
                        actor: str = "human") -> dict:
    """主题知识库已批准卡 → shared lesson 待审（不直接写 data/shared）。"""
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card.get("status") != "approved":
        raise ConflictError("only approved repo cards can be harvested")
    content = (f"{card.get('title')}\n\n{card.get('tldr')}\n\n"
               f"{card.get('body_md') or ''}").strip()
    return memory.create_lesson(
        conn, content, tags=card.get("tags") or [], repo="shared",
        source_task=card.get("source_task_id"), source_role=actor)
