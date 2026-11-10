import type { RoleRuntimeStatus } from "./api/client";

/** 任务内角色状态点灯：绿=本任务，红=其他任务，琥珀=切换中，蓝=空闲有上下文，灰=空闲/冷启动。 */
export interface RoleLiveDot {
  cls: "green" | "red" | "amber" | "blue" | "gray";
  title: string;
}

/**
 * 任务内角色状态徽标：颜色沿用任务状态的语义（蓝=进行中，灰=待办，红=繁忙）。
 * 队列全属于本任务时不显示「排队中」：有进程 → 进行中；无进程 → 等待唤醒。
 */
export type RoleLiveTone =
  | "in_progress" | "busy" | "queued" | "done" | "todo" | "in_review"
  | "failed" | "abandoned" | "closed" | "unknown";

export interface RoleLiveState {
  label: string;
  cls: RoleLiveTone;
  /** 徽标颜色必须独立于点灯；页面用 tone 而不是 stateClassForDot(dot). */
  tone: RoleLiveTone;
}

function state(label: string, tone: RoleLiveTone): RoleLiveState {
  return { label, cls: tone, tone };
}

export function roleLiveState(
  rt: RoleRuntimeStatus | undefined,
  taskId: string,
  options: {
    hasOpenStep?: boolean;
    allDone?: boolean;
    queuedHere?: number;
    queueAhead?: number;
    runStatus?: string | null;
    retryWait?: boolean;
    taskClosed?: boolean;
    taskDone?: boolean;
  } = {},
): RoleLiveState {
  // 终止/完成/失败优先于实时忙碌；否则完成后的历史 failed run 会把角色染红。
  if (options.taskClosed) return state("已终止", "closed");
  if (options.taskDone) return state("完成", "done");
  if (options.retryWait) return state("等待重试", "queued");
  if (options.runStatus === "failed") return state("失败", "failed");
  if (options.runStatus === "abandoned") return state("已放弃", "abandoned");
  if (!rt) return state("未知/加载中", "unknown");
  if (rt.switching) return state("切换上下文", "queued");
  const busy = rt.alive && rt.status === "busy";
  if (busy && rt.current_task_id === taskId) return state("进行中", "in_progress");
  if (busy && rt.current_task_id && rt.current_task_id !== taskId) {
    return state("繁忙", "busy");
  }
  if (busy && !rt.current_task_id) return state("进行中（系统任务）", "in_progress");
  if (options.hasOpenStep && rt.alive && rt.context_task_id === taskId) {
    return state("进行中", "in_progress");
  }
  const queuedHere = options.queuedHere ?? 0;
  if (queuedHere > 0) {
    if (!rt.alive) return state("等待唤醒", "queued");
    // ahead_count 由后端按 FIFO 顺序计算；只有真的排在其他消息后面才是“排队中”。
    if ((options.queueAhead ?? 0) > 0) return state("排队中", "queued");
    return state("进行中", "in_progress");
  }
  if (options.allDone) return state("完成", "done");
  return state("待办", "todo");
}

/** 徽标颜色来自 badge tone，而不是点灯颜色。 */
export function stateClassForTone(tone: RoleLiveTone): string {
  switch (tone) {
    case "failed": return "busy";
    case "abandoned":
    case "closed": return "closed";
    case "unknown": return "todo";
    default: return tone;
  }
}

/** 兼容旧调用：点灯颜色只用于圆点，不再决定徽标颜色。 */
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
  runStatus: string | null = null,
  taskClosed = false,
  queueAhead = 0,
  retryWait = false,
): RoleLiveDot {
  if (taskClosed) return { cls: "gray", title: "任务已终止" };
  if (!rt) return { cls: "gray", title: "状态加载中…" };
  if (rt.switching) return { cls: "amber", title: "正在切换任务上下文…" };
  if (retryWait) return { cls: "amber", title: "等待自动恢复重试" };
  if (runStatus === "failed") return { cls: "red", title: "本任务 run 失败，需要处理" };
  if (runStatus === "abandoned") return { cls: "gray", title: "本任务 run 已放弃/已中断" };
  if (failed) return { cls: "red", title: "本任务步骤失败，需要处理" };
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
  // turn 之间但子任务尚未提交：上下文仍绑在本任务，视为仍在执行该子任务。
  if (hasOpenStep && rt.alive && rt.context_task_id === taskId) {
    return { cls: "green", title: "本任务子任务进行中（等待下一轮）" };
  }
  if (queueAhead > 0) return { cls: "amber", title: "本任务消息排在其它任务之后" };
  if (hasContext) return { cls: "blue", title: "空闲 · 本任务上下文已保留" };
  return { cls: "gray", title: "空闲 · 首次进入将新建上下文" };
}
