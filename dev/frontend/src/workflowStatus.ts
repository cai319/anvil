import type { Workflow } from "./api/client";

/** 工作流任务的「角色状态」应从 workflow steps 推导，而不是旧的 task_role_claims。 */
export function roleStatusFromWorkflow(
  wf: Workflow | null | undefined,
  role: string,
  sliceKey?: string | null,
): { label: string; cls: string } | null {
  if (!wf) return null;
  const steps = wf.steps.filter((s) => s.role_slug === role
    && (sliceKey === undefined || (s.slice_key ?? null) === (sliceKey ?? null)));
  if (steps.length === 0) return null;
  // 最终失败优先于其他并行步骤，避免“一个步骤失败、另一个仍在跑”被显示成进行中。
  if (steps.some((s) => s.status === "failed")) return { label: "失败", cls: "closed" };
  // 自动恢复中的步骤是 pending + recovery_count/next_retry_at，不是普通待办。
  if (steps.some((s) => s.status === "pending"
      && ((s.recovery_count ?? 0) > 0 || Boolean(s.next_retry_at))))
    return { label: "等待重试", cls: "queued" };
  if (steps.some((s) => s.status === "running")) return { label: "进行中", cls: "in_progress" };
  if (steps.some((s) => s.status === "submitted")) return { label: "待审核", cls: "in_review" };
  if (steps.some((s) => s.status === "rejected")) return { label: "返工", cls: "queued" };
  if (steps.every((s) => s.status === "approved" || s.status === "skipped"))
    return { label: "完成", cls: "done" };
  return { label: "待办", cls: "todo" };
}
