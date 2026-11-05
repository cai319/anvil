"""人工按需的 LLM 知识清洗：只生成 proposed 卡的预览/修订，不自动批准。

原则：
- 只读任务时间线/卡片本身，不读角色私有 notes；
- 输出必须带原文引用；引用不成立的字段丢弃；
- 找不到证据时留空，不编造；
- 失败/关闭时原卡不变。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .. import config
from ..util import ConflictError, jdumps, jloads, now_iso
from . import knowledge_card, knowledge_quality, repo_knowledge, utility

PROMPT_VERSION = "knowledge-clean-v1"
_MAX_SOURCE_CHARS = 12000


class CleaningError(RuntimeError):
    """LLM 返回缺失/非法 JSON，或清洗预览无法安全应用。"""


def _enabled() -> bool:
    return bool(getattr(config, "KNOWLEDGE_LLM_CLEANING_ENABLED", False))


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _source_text(conn: sqlite3.Connection, card: dict) -> str:
    parts = [
        f"卡片标题：{card.get('title') or ''}",
        f"卡片摘要：{card.get('tldr') or ''}",
        f"卡片正文：{card.get('body_md') or ''}",
        f"卡片字段：{json.dumps(card.get('fields') or {}, ensure_ascii=False)}",
    ]
    task_id = card.get("source_task_id")
    if task_id:
        try:
            task = conn.execute(
                "SELECT title, description, tags_json FROM tasks WHERE id=?",
                (task_id,)).fetchone()
            if task is not None:
                parts.append(f"来源任务：{task['title']}（{task_id}）")
                parts.append(f"任务描述：{task['description'] or ''}")
                parts.append(f"任务标签：{task['tags_json'] or '[]'}")
            steps = conn.execute(
                "SELECT s.step_id, s.title, s.status, s.summary, s.feedback "
                "FROM workflow_steps s JOIN workflows w ON w.id=s.workflow_id "
                "WHERE w.task_id=? ORDER BY s.updated_at DESC LIMIT 20",
                (task_id,)).fetchall()
            for step in steps:
                parts.append(
                    f"步骤 {step['step_id']} [{step['status']}] {step['title']}："
                    f"{step['summary'] or ''} {step['feedback'] or ''}")
        except sqlite3.Error:
            pass
    return "\n".join(parts)[:_MAX_SOURCE_CHARS]


def _prompt(card: dict, source_text: str) -> str:
    return f"""你是知识库编辑，不是事实裁判。请只基于下面的来源材料，把这张知识卡整理成可人工评审的结构。

硬性规则：
1. 输出必须是单个 JSON 对象，不要 Markdown 代码块或解释。
2. 不得编造来源中没有的事实、数字、命令、版本或结论。
3. 每个新增/修改的字段必须在 citations 中给出原文引用；找不到证据就留空字符串。
4. 不要修改 kind、authority、status，不要批准卡片。
5. 保留来源材料中的限制、反例和环境差异，不要把它们删成泛泛结论。

输出 schema：
{{
  "title": "一句话结论，<=80字",
  "tldr": "何时用 + 具体作用，<=200字",
  "fields": {{
    "use_when": "...",
    "expected_effect": "...",
    "evidence": "...",
    "preconditions": "...",
    "exclusions": "...",
    "effect_type": "avoid_error|reduce_rework|speed|compliance|quality|other"
  }},
  "citations": [{{"field": "use_when", "quote": "来源原文片段"}}],
  "warnings": ["无法从来源确认的内容"]
}}

卡片类型：{card.get('kind')}
原卡标题：{card.get('title') or ''}
原卡摘要：{card.get('tldr') or ''}
原卡字段：{json.dumps(card.get('fields') or {}, ensure_ascii=False)}

来源材料：
---
{source_text}
---
"""


def _valid_citations(data: dict, source_text: str) -> tuple[list[dict], list[str]]:
    citations = data.get("citations")
    if not isinstance(citations, list):
        return [], ["LLM 未返回 citations"]
    src = _norm(source_text).lower()
    valid, warnings = [], []
    for item in citations:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        quote = _norm(item.get("quote"))
        if not field or not quote:
            continue
        if quote.lower() not in src:
            warnings.append(f"引用不成立，已丢弃：{field}")
            continue
        valid.append({"field": field, "quote": quote})
    return valid, warnings


def _filter_fields(old_fields: dict, cleaned_fields: dict,
                   citations: list[dict]) -> tuple[dict, list[str]]:
    cited_fields = {item["field"] for item in citations}
    accepted: dict = {}
    dropped: list[str] = []
    for key, value in cleaned_fields.items():
        key = str(key)
        if not isinstance(value, (str, int, float, list, dict)):
            continue
        if knowledge_card._has_value(old_fields.get(key)) \
                and str(old_fields.get(key)) == str(value):
            accepted[key] = value
            continue
        if key not in cited_fields:
            dropped.append(key)
            continue
        accepted[key] = value
    return accepted, dropped


def _effective_runtime(conn: sqlite3.Connection, runtime_id: str | None) -> str:
    try:
        return utility.resolve_runtime_id(conn, runtime_id)
    except utility.UtilityError:
        return str(runtime_id or "")


def build_preview(conn: sqlite3.Connection, card_id: str, *,
                  runtime_id: str | None = None) -> dict:
    if not _enabled():
        raise ConflictError("LLM knowledge cleaning is disabled")
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card.get("status") not in ("draft", "proposed"):
        raise ConflictError("only draft/proposed cards can be cleaned")
    source_text = _source_text(conn, card)
    prompt = _prompt(card, source_text)
    raw = utility.generate(conn, prompt, runtime_id=runtime_id)
    data = utility.extract_json_object(raw)
    if not isinstance(data, dict):
        raise CleaningError("LLM 没有返回合法 JSON 对象")
    old_fields = card.get("fields") or {}
    cleaned_fields = data.get("fields")
    if not isinstance(cleaned_fields, dict):
        cleaned_fields = {}
    citations, warnings = _valid_citations(data, source_text)
    accepted_fields, dropped = _filter_fields(old_fields, cleaned_fields, citations)
    for key in dropped:
        warnings.append(f"缺少可验证引用，已丢弃字段：{key}")
    title = str(data.get("title") or card.get("title") or "").strip()
    tldr = str(data.get("tldr") or card.get("tldr") or "").strip()
    merged_fields = {**old_fields, **accepted_fields}
    body_md = knowledge_card.render_markdown(
        title, tldr, card.get("kind") or "insight", merged_fields)
    quality = knowledge_quality.evaluate_row({
        **card, "title": title, "tldr": tldr, "body_md": body_md,
        "fields": merged_fields, "status": "proposed",
    }, "repo_knowledge")
    return {
        "card_id": card_id,
        "title": title,
        "tldr": tldr,
        "fields": accepted_fields,
        "merged_fields": merged_fields,
        "body_md": body_md,
        "citations": citations,
        "warnings": warnings,
        "quality": quality,
        "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "prompt_version": PROMPT_VERSION,
        "runtime_id": _effective_runtime(conn, runtime_id),
        "dry_run": True,
    }


def apply_preview(conn: sqlite3.Connection, card_id: str, cleaned: dict, *,
                  actor: str = "human") -> dict:
    if not _enabled():
        raise ConflictError("LLM knowledge cleaning is disabled")
    card = repo_knowledge.get(conn, card_id)
    if card is None:
        raise LookupError(card_id)
    if card.get("status") not in ("draft", "proposed"):
        raise ConflictError("only draft/proposed cards can be cleaned")
    fields = cleaned.get("merged_fields")
    if not isinstance(fields, dict):
        fields = cleaned.get("fields")
    if not isinstance(fields, dict):
        raise CleaningError("cleaned payload has no fields")
    quality_extra = {
        "llm": {
            "runtime_id": cleaned.get("runtime_id"),
            "prompt_version": cleaned.get("prompt_version") or PROMPT_VERSION,
            "applied_at": now_iso(),
            "citations": cleaned.get("citations") or [],
            "source_hash": cleaned.get("source_hash"),
        }
    }
    updated = repo_knowledge.update(
        conn, card_id,
        title=str(cleaned.get("title") or card.get("title") or ""),
        tldr=str(cleaned.get("tldr") or card.get("tldr") or ""),
        fields=fields,
        quality_extra=quality_extra,
        actor=actor,
    )
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, card.get("source_task_id"), "knowledge.clean.apply",
         jdumps({"table": "repo_knowledge", "card_id": card_id,
                 "prompt_version": PROMPT_VERSION,
                 "runtime_id": cleaned.get("runtime_id")}), now_iso()))
    conn.commit()
    return updated


def clean_batch(conn: sqlite3.Connection, *, repo: str | None = None,
                status: str = "proposed", limit: int = 5,
                dry_run: bool = True, runtime_id: str | None = None) -> dict:
    if not _enabled():
        raise ConflictError("LLM knowledge cleaning is disabled")
    limit = max(1, min(20, int(limit or 5)))
    cards = repo_knowledge.list_cards(
        conn, repo=repo, status=status, limit=limit)
    results = []
    for card in cards:
        try:
            preview = build_preview(
                conn, card["id"], runtime_id=runtime_id)
            item = {"card_id": card["id"], "ok": True, "preview": preview}
            if not dry_run:
                item["applied"] = apply_preview(
                    conn, card["id"], preview, actor="human")
            results.append(item)
        except Exception as exc:  # noqa: BLE001 —— 单卡失败不阻断批次
            results.append({"card_id": card.get("id"), "ok": False,
                            "error": str(exc)})
    return {"dry_run": dry_run, "count": len(results), "results": results}
