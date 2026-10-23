import type { RoleRuntimeStatus } from "./api/client";

/** 任务内角色状态点灯：绿=本任务，红=其他任务，琥珀=切换中，蓝=空闲有上下文，灰=空闲/冷启动。 */
export interface RoleLiveDot {
  cls: "green" | "red" | "amber" | "blue" | "gray";
  title: string;
}

/** 任务内角色状态徽标：颜色沿用任务状态的语义（蓝=进行中，灰=待办，红=繁忙）。 */
export interface RoleLiveState {
  label: string;
  cls: "in_progress" | "busy" | "queued" | "done" | "todo";
}

export function roleLiveState(
  rt: RoleRuntimeStatus | undefined,
  taskId: string,
  options: { hasOpenStep?: boolean; allDone?: boolean; queuedHere?: number } = {},
): RoleLiveState {
  if (!rt) return { label: "待办", cls: "todo" };
  if (rt.switching) return { label: "切换上下文", cls: "queued" };
  const busy = rt.alive && rt.status === "busy";
  if (busy && rt.current_task_id === taskId) return { label: "进行中", cls: "in_progress" };
  if (busy && rt.current_task_id && rt.current_task_id !== taskId) {
    return { label: "繁忙", cls: "busy" };
  }
  if (options.hasOpenStep && rt.alive && rt.context_task_id === taskId) {
    return { label: "进行中", cls: "in_progress" };
  }
  if ((options.queuedHere ?? 0) > 0) return { label: "排队中", cls: "queued" };
  if (options.allDone) return { label: "完成", cls: "done" };
  return { label: "待办", cls: "todo" };
}

/** 状态徽标颜色必须与点灯颜色一致（文字颜色跟随灯，而不是任务状态色）。 */
export function stateClassForDot(dot: RoleLiveDot["cls"]): string {
  switch (dot) {
    case "green": return "done";
    case "red": return "busy";
    case "amber": return "queued";
    case "blue": return "in_progress";
    default: return "todo";
  }
}

export function roleLiveDot(
  rt: RoleRuntimeStatus | undefined,
  taskId: string,
  hasContext: boolean,
  hasOpenStep = false,
  failed = false,
): RoleLiveDot {
  if (!rt) return { cls: "gray", title: "状态加载中…" };
  if (rt.switching) return { cls: "amber", title: "正在切换任务上下文…" };
  const busy = rt.alive && rt.status === "busy";
  const workingHere = Boolean(busy && rt.current_task_id === taskId);
  const workingElsewhere = Boolean(
    busy && rt.current_task_id && rt.current_task_id !== taskId,
  );
  if (workingElsewhere) {
    return {
      cls: "red",
      title: `正在其他任务「${rt.current_task?.title ?? rt.current_task_id}」工作`,
    };
  }
  if (workingHere) {
    return {
      cls: "green",
      title: rt.current_step_id
        ? `正在本任务工作 · 步骤 ${rt.current_step_id}`
        : "正在本任务工作",
    };
  }
  if (failed) return { cls: "red", title: "本任务步骤失败，需要处理" };
  // turn 之间但子任务尚未提交：上下文仍绑在本任务，视为仍在执行该子任务。
  if (hasOpenStep && rt.alive && rt.context_task_id === taskId) {
    return { cls: "green", title: "本任务子任务进行中（等待下一轮）" };
  }
  if (hasContext) return { cls: "blue", title: "空闲 · 本任务上下文已保留" };
  return { cls: "gray", title: "空闲 · 首次进入将新建上下文" };
}
