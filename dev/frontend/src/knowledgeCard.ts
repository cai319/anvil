import type { KnowledgeAuthority, KnowledgeKind, KnowledgeStatus } from "./api/client";

/** 知识卡类型与字段：频道页、任务页、知识库共用同一份定义。 */
export const KNOWLEDGE_KINDS: KnowledgeKind[] = [
  "convention", "decision", "workflow", "role_requirement", "insight",
  "task_finding", "personal_lesson",
];

export const KNOWLEDGE_KIND_LABEL: Record<KnowledgeKind, string> = {
  convention: "约定",
  decision: "决策",
  workflow: "流程",
  role_requirement: "角色需求",
  insight: "经验/洞察",
  task_finding: "任务发现",
  personal_lesson: "个人经验",
};

export const KNOWLEDGE_CARD_FIELDS: Record<KnowledgeKind, { key: string; label: string }[]> = {
  convention: [
    { key: "rule", label: "规则" }, { key: "scope", label: "适用范围" },
    { key: "rationale", label: "理由" }, { key: "examples", label: "示例" },
    { key: "verification", label: "验证方法" }, { key: "exceptions", label: "例外" },
  ],
  decision: [
    { key: "context", label: "背景" }, { key: "decision", label: "决定" },
    { key: "alternatives", label: "备选方案" }, { key: "consequences", label: "后果与取舍" },
  ],
  workflow: [
    { key: "purpose", label: "目的" }, { key: "preconditions", label: "前置条件" },
    { key: "steps", label: "步骤" }, { key: "verification", label: "验证" },
    { key: "rollback", label: "回滚/失败处理" },
  ],
  role_requirement: [
    { key: "capability", label: "能力" }, { key: "why", label: "为什么需要" },
    { key: "when", label: "何时需要" }, { key: "evidence", label: "证据" },
  ],
  insight: [
    { key: "observation", label: "观察" }, { key: "evidence", label: "证据" },
    { key: "lesson", label: "结论/经验" }, { key: "applicability", label: "适用性" },
    { key: "counterexamples", label: "反例" },
  ],
  task_finding: [
    { key: "task_context", label: "任务上下文" }, { key: "finding", label: "发现" },
    { key: "verification", label: "验证方式" }, { key: "reuse_scope", label: "可复用范围" },
  ],
  personal_lesson: [
    { key: "practice", label: "实践任务/run" }, { key: "outcome", label: "结果" },
    { key: "lesson", label: "提炼出的经验" }, { key: "applicability", label: "适用性" },
    { key: "confidence", label: "置信度" },
  ],
};

export const KNOWLEDGE_STATUS_LABEL: Record<KnowledgeStatus, string> = {
  draft: "草稿",
  proposed: "待批准",
  approved: "已批准",
  rejected: "已拒绝",
  deprecated: "已弃用",
  expired: "已过期",
};

export const AUTHORITY_LABEL: Record<KnowledgeAuthority, string> = {
  normative: "约束（必须遵守）",
  reference: "参考",
};

export function kindLabel(kind: KnowledgeKind): string {
  return KNOWLEDGE_KIND_LABEL[kind] ?? kind;
}
