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

/** 新增知识卡表单的字段说明：悬停提示“这是做什么的、该怎么填”。 */
export const KNOWLEDGE_FIELD_HINT: Record<string, string> = {
  title: "一句话结论，建议不超过 80 字；一张卡只表达一个主张，方便检索和审批。",
  tldr: "1–2 句摘要，让人先读这一句就能判断是否与当前任务相关。",
  kind: "知识卡类型：约定、决策、流程、角色需求、经验洞察等，决定正文必填项和展示方式。",
  authority: "约束力。normative 只用于 convention，对绑定该频道的任务是“必须遵守”；其余一律用 reference。",
  rule: "明确写出必须、禁止或应该做什么；尽量可执行、可验证，避免“尽量”“适当”等模糊措辞。",
  scope: "说明这条知识在什么任务、阶段、repo 或条件下适用，以及什么时候不适用。",
  rationale: "为什么需要这条约定；写清楚背后的风险、成本或评审依据。",
  examples: "给出正确和错误的示例，帮助读者和 Agent 判断边界。",
  verification: "评审或运行时如何检查是否遵守，例如检查清单、命令或指标。",
  exceptions: "允许豁免的明确场景；没有例外也要写“无”，避免规则被随意绕过。",
  context: "做这个决定时的背景、约束和目标。",
  decision: "最终决定做什么；一句话写清选择。",
  alternatives: "考虑过但没有采用的备选方案，以及未采用的原因。",
  consequences: "决定带来的取舍、风险和后续影响。",
  purpose: "这条流程要达成的目标。",
  preconditions: "执行流程前必须满足的条件、依赖或输入。",
  steps: "按顺序列出动作和预期结果，便于 Agent 直接执行。",
  rollback: "执行失败或结果不符合预期时如何回滚、降级或求助。",
  capability: "角色需要具备的具体能力，而不是泛泛的职责描述。",
  why: "为什么这个角色需要这项能力；说明缺失时的风险。",
  when: "在什么任务类型、阶段或触发条件下需要这项能力。",
  evidence: "支持这项需求的来源任务、事故、指标或事实依据。",
  observation: "观察到的事实或现象，尽量不带主观判断。",
  lesson: "从观察中提炼出的结论或可复用经验。",
  applicability: "这条经验在哪些任务、repo 或条件下成立。",
  counterexamples: "已知不成立或需要排除的场景，避免过度泛化。",
  task_context: "这个发现来自哪个任务、环境或运行。",
  finding: "具体发现了什么；写清事实和影响。",
  reuse_scope: "这条发现可以复用到哪些任务、模块或仓库。",
  practice: "产生这条经验的实践任务或 run id，作为证据链。",
  outcome: "实践结果：成功、失败及可验证的证据。",
  confidence: "对这条经验的置信度，以及依据（复现次数、样本、指标）。",
};
