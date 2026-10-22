import type { WorkflowStep } from "./api/client";

/** 工作流步骤状态的展示元数据（图谱与列表共用）。 */
export const STATUS_META: Record<WorkflowStep["status"], {
  label: string; color: string; bg: string;
}> = {
  pending:   { label: "待办",   color: "#8b94a9", bg: "rgba(139,148,169,.14)" },
  running:   { label: "进行中", color: "#60a5fa", bg: "rgba(96,165,250,.16)" },
  submitted: { label: "待审核", color: "#fbbf24", bg: "rgba(251,191,36,.16)" },
  approved:  { label: "完成",   color: "#34d399", bg: "rgba(52,211,153,.14)" },
  rejected:  { label: "已驳回", color: "#fb923c", bg: "rgba(251,146,60,.16)" },
  failed:    { label: "失败",   color: "#f87171", bg: "rgba(248,113,113,.16)" },
  skipped:   { label: "跳过",   color: "#5c6579", bg: "rgba(92,101,121,.12)" },
};

export function isDoneStatus(status: WorkflowStep["status"]): boolean {
  return status === "approved" || status === "skipped";
}

/** 运行期切片生成的步骤带 source_step_id / slice_key。 */
export function isDynamicStep(step: WorkflowStep): boolean {
  return Boolean(step.source_step_id);
}

/** 把步骤 id 后缀映射成闭环阶段名：scope/execute/verify/fix/verify2/decide。 */
export function phaseLabel(step: WorkflowStep): string {
  const suffix = step.step_id.split(".").pop() || "";
  switch (suffix) {
    case "scope": return "范围确认";
    case "verify": return "验证";
    case "fix": return "修复";
    case "verify2": return "复验";
    case "decide": return "决策";
    default: return "执行";
  }
}

function groupStatus(steps: WorkflowStep[]): WorkflowStep["status"] {
  if (steps.some((s) => s.status === "failed")) return "failed";
  if (steps.every((s) => isDoneStatus(s.status))) return "approved";
  if (steps.some((s) => s.status === "rejected")) return "rejected";
  if (steps.some((s) => s.status === "running" || s.status === "submitted")) return "running";
  if (steps.some((s) => isDoneStatus(s.status))) return "running";
  return "pending";
}

export interface LoopGroup {
  key: string;
  sourceStepId: string;
  sliceKey: string;
  title: string;
  steps: WorkflowStep[];
  total: number;
  done: number;
  status: WorkflowStep["status"];
  /** 真正处于 running/submitted/rejected/failed 的步骤（无则 null）。 */
  active: WorkflowStep | null;
  /** 当前活动步骤；若没有，则取第一个 pending 作为“下一步”。 */
  current: WorkflowStep | null;
  phase: string;
  roleSlug: string;
}

/** 按 source_step_id + slice_key 把动态步骤聚合成“一片”。 */
export function groupSliceLoops(steps: WorkflowStep[]): LoopGroup[] {
  const groups = new Map<string, WorkflowStep[]>();
  for (const step of steps) {
    if (!isDynamicStep(step)) continue;
    const key = `${step.source_step_id}::${step.slice_key ?? ""}`;
    const list = groups.get(key);
    if (list) list.push(step);
    else groups.set(key, [step]);
  }
  return [...groups.entries()].map(([key, groupSteps]) => {
    const first = groupSteps[0];
    const title = (first.title || "").split(" · ")[0] || first.slice_key || first.step_id;
    const active =
      groupSteps.find((s) =>
        s.status === "running" || s.status === "submitted"
        || s.status === "rejected" || s.status === "failed")
      ?? null;
    const current = active
      ?? groupSteps.find((s) => s.status === "pending")
      ?? null;
    return {
      key,
      sourceStepId: first.source_step_id || "",
      sliceKey: first.slice_key || "",
      title,
      steps: groupSteps,
      total: groupSteps.length,
      done: groupSteps.filter((s) => isDoneStatus(s.status)).length,
      status: groupStatus(groupSteps),
      active,
      current,
      phase: current ? phaseLabel(current) : "完成",
      roleSlug: current?.role_slug || "",
    };
  });
}

export type SlicePhaseKey = "scope" | "execute" | "verify" | "fix" | "verify2" | "decide";

export interface SlicePhase {
  key: SlicePhaseKey;
  label: string;
  steps: WorkflowStep[];
  status: WorkflowStep["status"];
  done: number;
  total: number;
  roleSlug: string;
}

const PHASE_ORDER: { key: SlicePhaseKey; label: string }[] = [
  { key: "scope", label: "范围确认" },
  { key: "execute", label: "执行" },
  { key: "verify", label: "验证" },
  { key: "fix", label: "修复" },
  { key: "verify2", label: "复验" },
  { key: "decide", label: "决策" },
];

function phaseKeyOf(step: WorkflowStep): SlicePhaseKey {
  const suffix = step.step_id.split(".").pop() || "";
  if (suffix === "scope" || suffix === "verify" || suffix === "fix"
      || suffix === "verify2" || suffix === "decide") {
    return suffix;
  }
  return "execute";
}

/** 把一片的具体步骤折叠成 scope → execute[...] → verify → fix → verify2 → decide。 */
export function slicePhases(group: LoopGroup): SlicePhase[] {
  const buckets = new Map<SlicePhaseKey, WorkflowStep[]>();
  for (const step of group.steps) {
    const key = phaseKeyOf(step);
    const list = buckets.get(key);
    if (list) list.push(step);
    else buckets.set(key, [step]);
  }
  return PHASE_ORDER.flatMap(({ key, label }) => {
    const steps = buckets.get(key);
    if (!steps || steps.length === 0) return [];
    return [{
      key,
      label,
      steps,
      status: groupStatus(steps),
      done: steps.filter((s) => isDoneStatus(s.status)).length,
      total: steps.length,
      roleSlug: steps[0].role_slug || "",
    }];
  });
}

export interface LoopSummary {
  sourceStepId: string;
  groups: LoopGroup[];
  totalSlices: number;
  doneSlices: number;
  totalSteps: number;
  doneSteps: number;
  status: WorkflowStep["status"];
  currentGroup: LoopGroup | null;
  currentStep: WorkflowStep | null;
  currentPhase: string;
  currentProgress: string;
}

/** 每个 plan_slices 步骤对应一个循环；图谱里只画一个循环节点。 */
export function summarizeLoops(steps: WorkflowStep[]): LoopSummary[] {
  const bySource = new Map<string, WorkflowStep[]>();
  for (const step of steps) {
    if (!isDynamicStep(step)) continue;
    const source = step.source_step_id || "";
    const list = bySource.get(source);
    if (list) list.push(step);
    else bySource.set(source, [step]);
  }
  return [...bySource.entries()].map(([sourceStepId, sourceSteps]) => {
    const groups = groupSliceLoops(sourceSteps);
    const currentGroup =
      groups.find((g) => g.status === "running" || g.status === "rejected")
      ?? groups.find((g) => g.status !== "approved")
      ?? groups[groups.length - 1]
      ?? null;
    const currentStep = currentGroup?.current ?? null;
    return {
      sourceStepId,
      groups,
      totalSlices: groups.length,
      doneSlices: groups.filter((g) => g.status === "approved").length,
      totalSteps: sourceSteps.length,
      doneSteps: sourceSteps.filter((s) => isDoneStatus(s.status)).length,
      status: groupStatus(sourceSteps),
      currentGroup,
      currentStep,
      currentPhase: currentGroup?.phase ?? "完成",
      currentProgress: currentGroup ? `${currentGroup.done}/${currentGroup.total}` : "0/0",
    };
  });
}
