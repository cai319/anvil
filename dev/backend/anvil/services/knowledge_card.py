"""统一知识卡：类型化必填校验、Markdown 渲染、完整度评分。

所有知识（频道/任务/个人/lessons）共用这一层；引用实践：
ADR、SOP/runbook、规则卡、Postmortem、Zettelkasten、docs-as-code。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

KINDS = (
    "convention",
    "decision",
    "workflow",
    "role_requirement",
    "insight",
    "task_finding",
    "personal_lesson",
)

# 每种类型的完整审批必填字段（proposal 只要求 title/tldr/正文）。
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "convention": ("rule", "scope", "rationale", "examples", "verification", "exceptions"),
    "decision": ("context", "decision", "alternatives", "consequences"),
    "workflow": ("purpose", "preconditions", "steps", "verification", "rollback"),
    "role_requirement": ("capability", "why", "when", "evidence"),
    "insight": ("observation", "evidence", "lesson", "applicability", "counterexamples"),
    "task_finding": ("task_context", "finding", "verification", "reuse_scope"),
    "personal_lesson": ("practice", "outcome", "lesson", "applicability", "confidence"),
}

FIELD_LABELS: dict[str, str] = {
    "scope": "适用范围",
    "rule": "规则",
    "context": "背景",
    "decision": "决定",
    "alternatives": "备选方案",
    "consequences": "后果与取舍",
    "rationale": "理由",
    "evidence": "证据与来源",
    "examples": "示例",
    "verification": "如何验证",
    "exceptions": "例外",
    "known_failures": "已知失败模式",
    "purpose": "目的",
    "preconditions": "前置条件",
    "steps": "步骤",
    "rollback": "回滚/失败处理",
    "capability": "能力",
    "why": "为什么需要",
    "when": "何时需要",
    "observation": "观察",
    "lesson": "结论/经验",
    "applicability": "适用性",
    "counterexamples": "反例",
    "task_context": "任务上下文",
    "finding": "发现",
    "reuse_scope": "可复用范围",
    "practice": "实践任务/run",
    "outcome": "结果",
    "confidence": "置信度",
    "use_when": "使用场景",
    "expected_effect": "作用效果",
    "effect_type": "效果类型",
    "exclusions": "不适用/反例",
}

# 跨类型质量字段：审批硬门禁要求前三个；后两个是可选边界。
QUALITY_FIELDS = ("use_when", "expected_effect", "evidence",
                  "preconditions", "exclusions")
QUALITY_REQUIRED = ("use_when", "expected_effect", "evidence")
QUALITY_ALIASES: dict[str, tuple[str, ...]] = {
    "use_when": ("use_when", "scope", "applicability", "reuse_scope", "when"),
    "expected_effect": ("expected_effect", "effect", "outcome", "consequences"),
    "evidence": ("evidence",),
    "exclusions": ("exclusions", "counterexamples", "exceptions"),
}
EFFECT_TYPES = (
    "avoid_error", "reduce_rework", "speed", "compliance",
    "quality", "other",
)
READABILITY_LIMITS = {"title": 80, "tldr": 200, "body": 4000}
GENERIC_TITLES = ("经验", "记录", "总结", "审计问题", "注意事项", "发现")
GENERIC_EFFECTS = ("提高效率", "提升效率", "有用", "更好", "有价值", "有帮助")

VAGUE_WORDS = ("尽量", "适当", "合理", "大概", "也许", "可能应该")


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def normalize_fields(fields: dict | None) -> dict:
    if not isinstance(fields, dict):
        return {}
    return {str(k): v for k, v in fields.items() if _has_value(v)}


def resolve_quality_field(fields: dict | None, canonical: str) -> tuple[str | None, object]:
    """返回质量字段的命中键和值；兼容旧卡的 kind-specific 字段。"""
    fields = fields or {}
    for key in QUALITY_ALIASES.get(canonical, (canonical,)):
        if _has_value(fields.get(key)):
            return key, fields[key]
    return None, None


def _quality_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key} {val}" for key, val in value.items())
    return str(value or "").strip()


def evaluate_quality(kind: str, title: str, tldr: str, body_md: str,
                     fields: dict | None, *,
                     authority: str = "reference",
                     status: str = "proposed",
                     source_task_id: str | None = None,
                     source_step_id: str | None = None,
                     source_role: str | None = None,
                     review_at: str | None = None,
                     expires_at: str | None = None,
                     legacy: bool = False) -> dict:
    """确定性的知识质量评估。

    `blocking` 用于审批硬门禁；`score` 只用于排序/展示，不能替代人工。
    """
    fields = normalize_fields(fields)
    title = str(title or "").strip()
    tldr = str(tldr or "").strip()
    body_md = str(body_md or "").strip()
    missing: list[str] = []
    blocking: list[str] = []
    warnings: list[str] = []

    for canonical in QUALITY_REQUIRED:
        _key, value = resolve_quality_field(fields, canonical)
        if not _has_value(value):
            missing.append(canonical)
            blocking.append(canonical)
    for key in REQUIRED_FIELDS.get(kind, ()):
        if not _has_value(fields.get(key)) and key not in missing:
            missing.append(key)
            blocking.append(key)

    if not title:
        blocking.append("title")
    elif len(title) > READABILITY_LIMITS["title"]:
        blocking.append("title_length")
        warnings.append(f"标题超过 {READABILITY_LIMITS['title']} 字")
    if not tldr:
        blocking.append("tldr")
    elif len(tldr) > READABILITY_LIMITS["tldr"]:
        blocking.append("tldr_length")
        warnings.append(f"TLDR 超过 {READABILITY_LIMITS['tldr']} 字")
    if not body_md:
        blocking.append("body")
    elif len(body_md) > READABILITY_LIMITS["body"]:
        blocking.append("body_length")
        warnings.append(f"正文超过 {READABILITY_LIMITS['body']} 字")

    text = " ".join([title, tldr, body_md,
                     *(_quality_text(v) for v in fields.values())])
    if any(word in text for word in VAGUE_WORDS):
        warnings.append("存在模糊措辞（尽量/适当/合理等），建议给出可验证规则或示例")
    if title in GENERIC_TITLES or len(title) <= 2:
        warnings.append("标题过于泛化，建议写成一句话结论")

    lines = [line for line in body_md.splitlines() if line.strip()]
    timestamp_lines = sum(
        1 for line in lines
        if re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", line))
    if len(lines) >= 20 and timestamp_lines >= len(lines) * 0.5:
        warnings.append("正文包含大段原始日志，建议提炼结论并附证据路径")
    elif len(lines) > 12 and "## " not in body_md and not body_md.startswith("# "):
        warnings.append("正文较长但缺少分节，建议按场景/证据/步骤组织")

    _effect_key, effect_value = resolve_quality_field(fields, "expected_effect")
    effect_text = _quality_text(effect_value)
    if effect_text and any(word in effect_text for word in GENERIC_EFFECTS):
        warnings.append("作用效果较泛化，建议写明避免/改善的具体结果")
    effect_type = str(fields.get("effect_type") or "").strip()
    if effect_text and effect_type and effect_type not in EFFECT_TYPES:
        warnings.append(f"未知效果类型：{effect_type}")
    if not review_at and not expires_at:
        warnings.append("未设置复核日期或过期时间")
    if not fields.get("preconditions"):
        warnings.append("未填写前置条件（可选，但建议说明适用边界）")

    evidence_key, evidence_value = resolve_quality_field(fields, "evidence")
    use_when_key, use_when_value = resolve_quality_field(fields, "use_when")
    evidence_score = 30 if _has_value(evidence_value) else (
        10 if (source_task_id or source_step_id or source_role) else 0)
    scenario_score = 25 if _has_value(use_when_value) else 0
    effect_score = 25 if _has_value(effect_value) else 0
    readability = 20
    readability -= 5 * sum(1 for item in ("title", "tldr", "body")
                           if item in blocking)
    readability -= 4 * sum(1 for item in (
        "title_length", "tldr_length", "body_length") if item in blocking)
    readability -= min(6, 3 * len([w for w in warnings
                                   if "日志" in w or "模糊" in w or "泛化" in w]))
    readability = max(0, readability)

    if legacy:
        warnings.append("历史已批准卡，未经过当前质量门；请人工复核")
    score = int(evidence_score + scenario_score + effect_score + readability)
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"
    return {
        "score": score,
        "grade": grade,
        "blocking": list(dict.fromkeys(blocking)),
        "missing": list(dict.fromkeys(missing)),
        "warnings": list(dict.fromkeys(warnings)),
        "dimensions": {
            "evidence": evidence_score,
            "scenario": scenario_score,
            "effect": effect_score,
            "readability": readability,
        },
        "readable": not any(item in blocking for item in (
            "title", "tldr", "body", "title_length", "tldr_length", "body_length")),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "gate_version": 1,
        "legacy": bool(legacy),
        "resolved_fields": {
            "use_when": use_when_key,
            "expected_effect": _effect_key,
            "evidence": evidence_key,
        },
    }


def completeness(kind: str, fields: dict | None) -> float:
    required = REQUIRED_FIELDS.get(kind, ())
    if not required:
        return 1.0 if normalize_fields(fields) else 0.0
    present = sum(1 for key in required if _has_value((fields or {}).get(key)))
    return round(present / len(required), 4)


def validate_card(kind: str, fields: dict | None, *, for_approval: bool = False) -> dict:
    """返回 {errors, warnings, completeness}；errors 阻止 approve。"""
    fields = normalize_fields(fields)
    errors: list[str] = []
    warnings: list[str] = []
    if kind not in KINDS:
        errors.append(f"unknown kind: {kind}")
        return {"errors": errors, "warnings": warnings, "completeness": 0.0}
    if for_approval:
        for key in REQUIRED_FIELDS.get(kind, ()):
            if not _has_value(fields.get(key)):
                errors.append(f"missing required field: {key} ({FIELD_LABELS.get(key, key)})")
    text = " ".join(
        str(v) for v in fields.values() if isinstance(v, (str, int, float))
    )
    if any(word in text for word in VAGUE_WORDS):
        warnings.append("存在模糊措辞（尽量/适当/合理等），建议给出可验证的规则或示例")
    if kind == "convention" and not _has_value(fields.get("examples")):
        warnings.append("约定缺少示例")
    if kind == "decision" and not _has_value(fields.get("alternatives")):
        warnings.append("决策缺少备选方案")
    if kind == "workflow" and not _has_value(fields.get("rollback")):
        warnings.append("流程缺少回滚/失败处理")
    return {
        "errors": errors,
        "warnings": warnings,
        "completeness": completeness(kind, fields),
    }


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {item}" for item in value)
    if isinstance(value, dict):
        return "\n".join(f"- {k}: {v}" for k, v in value.items())
    return str(value).strip()


def render_markdown(title: str, tldr: str, kind: str, fields: dict | None) -> str:
    """把结构化字段渲染成人类可读的 Markdown 知识卡。"""
    fields = normalize_fields(fields)
    lines = [f"# {title.strip()}", "", f"> **TL;DR**：{tldr.strip()}", ""]
    # 按类型优先展示核心字段，其余按标签顺序补齐。
    priority = list(REQUIRED_FIELDS.get(kind, ())) + list(QUALITY_FIELDS) + [
        "known_failures", "examples", "rationale", "related",
    ]
    seen: set[str] = set()
    for key in priority:
        if key in seen or not _has_value(fields.get(key)):
            continue
        seen.add(key)
        lines.extend([f"## {FIELD_LABELS.get(key, key)}", _as_text(fields[key]), ""])
    for key, value in fields.items():
        if key in seen or not _has_value(value):
            continue
        lines.extend([f"## {FIELD_LABELS.get(key, key)}", _as_text(value), ""])
    return "\n".join(lines).strip() + "\n"


def content_hash(title: str, tldr: str, body_md: str) -> str:
    raw = "\n".join([title.strip(), tldr.strip(), body_md.strip()]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_FIELD_EXAMPLES = {
    "rule": "所有 API 变更必须同步更新 OpenAPI 文档。",
    "scope": "当前频道下的后端任务；纯文档任务不适用。",
    "rationale": "避免接口漂移，评审时可机械检查。",
    "examples": "✅ 新增字段时更新 openapi.json；❌ 只改实现不更新文档。",
    "verification": "评审检查 diff 是否包含文档变更。",
    "exceptions": "纯内部重构且不影响接口签名时可豁免。",
    "context": "任务量增大，单次提交往返过多。",
    "decision": "采用批量提交，每批最多 20 个文件。",
    "alternatives": "逐文件提交；增大超时时间。",
    "consequences": "减少往返；失败时回滚范围变大。",
    "purpose": "保证发布可重复。",
    "preconditions": "依赖已安装，测试全绿。",
    "steps": "1. 运行测试；2. 构建镜像；3. 灰度发布；4. 观察指标。",
    "rollback": "灰度异常时切回上一镜像。",
    "capability": "能够独立完成数据库迁移评审。",
    "why": "迁移风险高，需要专人把关。",
    "when": "涉及 schema 变更的任务。",
    "evidence": "过去两次迁移事故均缺少评审。",
    "observation": "子任务并行时上下文切换成本高。",
    "evidence": "任务 T-123 的排队与切换记录。",
    "lesson": "同一角色的子任务应尽量串行。",
    "applicability": "同一角色被多个子任务共享时。",
    "counterexamples": "互不依赖且上下文很小的任务可并行。",
    "task_context": "为登录接口增加限流。",
    "finding": "令牌桶实现比固定窗口更适合突发流量。",
    "reuse_scope": "同仓库所有对外 API。",
    "practice": "任务 T-123 / run R-456",
    "outcome": "成功：评审一次通过，无返工。",
    "confidence": "高（已在两个任务中复现）",
    "use_when": "任务涉及 X509 且使用发行版 OpenSSL 时。",
    "expected_effect": "避免把环境差异误判为漏洞，减少一轮返工。",
    "effect_type": "avoid_error|reduce_rework|speed|compliance|quality|other",
    "exclusions": "自编译 OpenSSL 或未启用 ASan 的环境不适用。",
}


def template(kind: str | None = None) -> dict:
    """返回知识卡模板 schema + 示例；供前端/CLI/Agent 生成与校验。"""
    kinds = [kind] if kind in KINDS else list(KINDS)
    out: dict[str, dict] = {}
    for item in kinds:
        required = list(REQUIRED_FIELDS.get(item, ()))
        fields = [
            {"key": key, "label": FIELD_LABELS.get(key, key),
             "required": key in required or key in QUALITY_REQUIRED,
             "example": _FIELD_EXAMPLES.get(key, "")}
            for key in dict.fromkeys(required + list(QUALITY_FIELDS) + list(FIELD_LABELS))
        ]
        out[item] = {
            "required_fields": required,
            "fields": fields,
            "limits": {"title_chars": 80, "tldr_chars": 200, "body_chars": 4000},
            "note": "proposal 只需 title/tldr/正文；approve 时按 required_fields 完整校验。",
        }
    return {"template_version": 1, "kinds": out}
