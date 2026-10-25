"""统一知识卡：类型化必填校验、Markdown 渲染、完整度评分。

所有知识（频道/任务/个人/lessons）共用这一层；引用实践：
ADR、SOP/runbook、规则卡、Postmortem、Zettelkasten、docs-as-code。
"""
from __future__ import annotations

import hashlib

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
}

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
    priority = list(REQUIRED_FIELDS.get(kind, ())) + [
        "known_failures", "examples", "rationale", "evidence", "related",
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
}


def template(kind: str | None = None) -> dict:
    """返回知识卡模板 schema + 示例；供前端/CLI/Agent 生成与校验。"""
    kinds = [kind] if kind in KINDS else list(KINDS)
    out: dict[str, dict] = {}
    for item in kinds:
        required = list(REQUIRED_FIELDS.get(item, ()))
        fields = [
            {"key": key, "label": FIELD_LABELS.get(key, key),
             "required": key in required,
             "example": _FIELD_EXAMPLES.get(key, "")}
            for key in dict.fromkeys(required + list(FIELD_LABELS))
        ]
        out[item] = {
            "required_fields": required,
            "fields": fields,
            "limits": {"title_chars": 80, "tldr_chars": 200, "body_chars": 4000},
            "note": "proposal 只需 title/tldr/正文；approve 时按 required_fields 完整校验。",
        }
    return {"template_version": 1, "kinds": out}
