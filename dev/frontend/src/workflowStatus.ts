import type { Workflow } from "./api/client";

/** 工作流任务的「角色状态」应从 workflow steps 推导，而不是旧的 task_role_claims。 */
export function roleStatusFromWorkflow(
  wf: Workflow | null | undefined,
  role: string,
): { label: string; cls: string } | null {
  if (!wf) return null;
  const steps = wf.steps.filter((s) => s.role_slug === role);
  if (steps.length === 0) return null;
  if (steps.some((s) => s.status === "running")) return { label: "进行中", cls: "in_progress" };
  if (steps.some((s) => s.status === "submitted")) return { label: "待审核", cls: "in_review" };
  if (steps.some((s) => s.status === "failed")) return { label: "失败", cls: "closed" };
  if (steps.every((s) => s.status === "approved" || s.status === "skipped"))
    return { label: "完成", cls: "done" };
  return { label: "待办", cls: "todo" };
}
