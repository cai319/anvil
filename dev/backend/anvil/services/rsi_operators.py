"""Fixed RSI edit operator catalog and validation (v1.50 P3)."""
from __future__ import annotations

import re

MAX_EDITS = 2
MAX_CONTENT = 400
ALLOWED_TARGETS = {
    "instruction.strategy", "principles", "memory_card", "playbook",
}
OPERATORS = {
    "append_knowledge_card": ("autonomous", "memory_card"),
    "revise_knowledge_card": ("autonomous", "memory_card"),
    "link_or_confidence_update": ("autonomous", "memory_card"),
    "append_principle": ("human_approval", "principles"),
    "replace_strategy_block": ("human_approval", "instruction.strategy"),
    "append_playbook_item": ("human_approval", "playbook"),
    "revise_playbook_item": ("human_approval", "playbook"),
    "prune_playbook_item": ("human_approval", "playbook"),
}
_ALLOWED_PREFIXES = ("notes/memory/cards/", "notes/playbooks/", "playbooks/")
_FORBIDDEN = re.compile(
    r"(忽略人工|绕过评审|绕过审批|直接修改运行时|关闭测试|改运行时权限|"
    r"permission[s]?\s*=|ANVIL_TOKEN|ANVIL_EVAL_KEY)", re.IGNORECASE)


def contains_forbidden(text: str) -> bool:
    return bool(_FORBIDDEN.search(str(text or "")))


def validate_edits(edits: list[dict]) -> list[dict]:
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list")
    if len(edits) > MAX_EDITS:
        raise ValueError(f"at most {MAX_EDITS} edits per candidate")
    normalized: list[dict] = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("edit must be an object")
        operator = str(edit.get("operator") or "")
        if operator not in OPERATORS:
            raise ValueError(f"unknown operator: {operator}")
        default_tier, expected_target = OPERATORS[operator]
        target = str(edit.get("target") or "")
        if target not in ALLOWED_TARGETS:
            raise ValueError(f"target not allowed: {target}")
        if target != expected_target:
            raise ValueError(f"operator {operator} cannot edit target {target}")
        content = str(edit.get("content_md") or "")
        if not content.strip():
            raise ValueError("content_md is required")
        if len(content) > MAX_CONTENT:
            raise ValueError(f"content_md exceeds {MAX_CONTENT} chars")
        if _FORBIDDEN.search(content):
            raise ValueError("content contains a forbidden instruction")
        refs = edit.get("portrait_refs")
        if not isinstance(refs, list) or not [x for x in refs if str(x).strip()]:
            raise ValueError("portrait_refs are required")
        if not str(edit.get("diagnosis_ref") or "").strip():
            raise ValueError("diagnosis_ref is required")
        if not isinstance(edit.get("predicted_effect"), dict):
            raise ValueError("predicted_effect is required")
        if target in ("memory_card", "playbook"):
            path = str(edit.get("path") or "").strip()
            if not path or not path.startswith(_ALLOWED_PREFIXES):
                raise ValueError(f"invalid path for {target}")
            if ".." in path.split("/"):
                raise ValueError("path traversal is not allowed")
            normalized_path = path
        else:
            normalized_path = None
        # Tier is derived from the fixed operator catalog; a reflection output
        # cannot downgrade an approval-tier edit to autonomous by claiming so.
        normalized.append({
            **edit,
            "operator": operator,
            "tier": default_tier,
            "target": target,
            **({"path": normalized_path} if normalized_path else {}),
        })
    return normalized
