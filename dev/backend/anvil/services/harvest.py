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


_CHANNEL_KINDS = {"convention", "decision", "workflow", "insight", "role_requirement"}


def _adapted_channel_fields(kind: str, fields: dict, title: str,
                            tldr: str, source_task_id: str | None) -> dict:
    """把不适合频道卡类型的字段补齐为 insight 必填字段（保持旧字段不动）。"""
    out = dict(fields or {})
    if kind != "insight":
        return out

    def fill(key: str, value: object) -> None:
        if not str(out.get(key) or "").strip():
            out[key] = value

    fill("observation",
         out.get("task_context") or out.get("finding") or tldr or title)
    fill("lesson",
         out.get("finding") or out.get("rule") or out.get("decision") or title)
    fill("applicability",
         out.get("reuse_scope") or out.get("scope") or out.get("use_when") or "")
    fill("counterexamples",
         out.get("exclusions") or out.get("exceptions") or "暂无已知反例")
    fill("evidence",
         out.get("verification") or out.get("evidence")
         or (f"任务 {source_task_id}" if source_task_id else "来源主题知识卡"))
    fill("use_when",
         out.get("applicability") or out.get("reuse_scope")
         or out.get("scope") or out.get("use_when") or "")
    fill("expected_effect",
         out.get("outcome") or out.get("consequences")
         or "复用该知识，减少同类任务的重复探索。")
    return out


def repo_card_to_channel(conn: sqlite3.Connection, card_id: str,
                         workspace_id: str, *, actor: str = "human") -> dict:
    """主题知识库已批准卡 → Channel 知识草稿。"""
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card.get("status") != "approved":
        raise ConflictError("only approved repo cards can be harvested")
    _assert_approved_quality(card)
    source_kind = str(card.get("kind") or "insight")
    kind = source_kind if source_kind in _CHANNEL_KINDS else "insight"
    fields = _adapted_channel_fields(
        kind, card.get("fields") or {}, card.get("title") or "",
        card.get("tldr") or "", card.get("source_task_id"))
    return workspace_knowledge.create(
        conn, workspace_id,
        title=card.get("title") or "",
        tldr=card.get("tldr") or "",
        kind=kind,
        authority=card.get("authority") or "reference",
        fields=fields,
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
    fields = dict(card.get("fields") or {})
    # 主题卡类型可能不是 insight；晋升为共享经验时补齐 insight 卡字段，
    # 保证 experience 在提交（pending）前就是完整知识。
    if not fields.get("observation"):
        fields["observation"] = (fields.get("context") or card.get("tldr")
                                 or card.get("title") or "")
    if not fields.get("lesson"):
        fields["lesson"] = (fields.get("lesson") or fields.get("rule")
                            or fields.get("decision") or card.get("title") or "")
    if not fields.get("applicability"):
        fields["applicability"] = (fields.get("applicability") or fields.get("scope")
                                   or fields.get("reuse_scope")
                                   or fields.get("use_when") or "")
    if not fields.get("counterexamples"):
        fields["counterexamples"] = (fields.get("counterexamples")
                                     or fields.get("exclusions")
                                     or fields.get("exceptions")
                                     or "暂无已知反例")
    return memory.create_lesson(
        conn, content, tags=card.get("tags") or [], repo="shared",
        source_task=card.get("source_task_id"), source_role=actor,
        title=card.get("title"), tldr=card.get("tldr"), fields=fields)
