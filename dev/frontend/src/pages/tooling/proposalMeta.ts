export const PROPOSAL_ACTIVE_STATUSES = new Set([
  "draft", "open", "triaged", "accepted", "implemented", "verified",
]);

export const PROPOSAL_STATUSES = [
  "draft", "open", "triaged", "accepted", "implemented", "verified",
  "closed", "rejected", "duplicate", "wontfix", "stale",
] as const;

export const PROPOSAL_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  open: "待处理",
  triaged: "已分诊",
  accepted: "已接受",
  implemented: "已实施",
  verified: "已验证",
  closed: "已关闭",
  rejected: "已拒绝",
  duplicate: "重复",
  wontfix: "不修复",
  stale: "已过期",
};

export const PROPOSAL_TERMINAL_STATUSES = new Set([
  "closed", "rejected", "duplicate", "wontfix", "stale",
]);

export const PROPOSAL_TRANSITIONS: Record<string, string[]> = {
  draft: ["open", "triaged", "rejected", "duplicate", "wontfix", "stale"],
  open: ["triaged", "accepted", "rejected", "duplicate", "wontfix", "stale"],
  triaged: ["accepted", "rejected", "duplicate", "wontfix", "stale"],
  accepted: ["implemented", "rejected", "wontfix"],
  implemented: ["verified", "rejected"],
  verified: ["closed", "rejected"],
};

export const PROPOSAL_PRIMARY_TRANSITION: Record<string, string | undefined> = {
  draft: "open",
  open: "triaged",
  triaged: "accepted",
  accepted: "implemented",
  implemented: "verified",
  verified: "closed",
};

export const PROPOSAL_TRANSITION_LABEL: Record<string, string> = {
  open: "开始处理",
  triaged: "分诊",
  accepted: "接受",
  implemented: "标记已实施",
  verified: "标记已验证",
  closed: "关闭",
  rejected: "拒绝",
  duplicate: "标记重复",
  wontfix: "不修复",
  stale: "标记过期",
};

export const PROPOSAL_COMPONENT_LABEL: Record<string, string> = {
  skill: "Skill",
  mcp_server: "MCP Server",
  runtime_yaml: "运行时 YAML",
  feature_flag: "功能开关",
  tool_preset: "工具预设",
  model: "模型",
  environment: "环境",
  harness_part: "Harness",
  docs_template: "文档模板",
  other: "其他",
};

export const PROPOSAL_SEVERITY_ORDER = [
  "blocker", "critical", "major", "high", "medium", "minor", "low", "info",
];

export const PROPOSAL_SEVERITY_LABEL: Record<string, string> = {
  blocker: "阻断",
  critical: "严重",
  major: "重要",
  high: "高",
  medium: "中",
  minor: "低",
  low: "低",
  info: "提示",
};

export function isProposalTerminal(status: string): boolean {
  return PROPOSAL_TERMINAL_STATUSES.has(status);
}

export function proposalStatusLabel(status: string): string {
  return PROPOSAL_STATUS_LABEL[status] ?? status;
}

export function proposalSeverityLabel(severity: string): string {
  return PROPOSAL_SEVERITY_LABEL[severity] ?? severity;
}

export function proposalComponentLabel(component: string): string {
  return PROPOSAL_COMPONENT_LABEL[component] ?? component;
}

export function severityRank(severity: string): number {
  const index = PROPOSAL_SEVERITY_ORDER.indexOf(severity);
  return index < 0 ? PROPOSAL_SEVERITY_ORDER.length : index;
}

export function allowedTransitions(status: string): string[] {
  return PROPOSAL_TRANSITIONS[status] ?? [];
}
