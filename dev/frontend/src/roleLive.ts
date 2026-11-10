import type { RoleRuntimeStatus } from "./api/client";

/** 任务内角色状态点灯：绿=空闲/成功，橙=处理中，黄=等待人工，红=排队/繁忙/失败，灰=结束/未开始。 */
export interface RoleLiveDot {
  cls: "green" | "orange" | "yellow" | "red" | "gray";
  title: string;
}

/**
 * 任务内角色状态徽标：与实时点灯一致。
 * 绿=空闲/成功，橙=处理中，黄=等待人工，红=排队/繁忙/失败，灰=结束/未开始。
 */
export type RoleLiveTone =
  | "idle" | "in_progress" | "busy" | "queued" | "done" | "todo" | "in_review"
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
    stepFailed?: boolean;
    taskClosed?: boolean;
    taskDone?: boolean;
    taskInReview?: boolean;
    hasReviewStep?: boolean;
    hasContext?: boolean;
  } = {},
): RoleLiveState {
  // 终止/完成/失败优先于实时忙碌；否则完成后的历史 failed run 会把角色染红。
  if (options.taskClosed) return state("已终止", "closed");
  if (options.taskDone) return state("完成", "done");
  if (options.runStatus === "failed") return state("失败", "failed");
  if (options.runStatus === "abandoned") return state("已放弃", "abandoned");
  // 步骤失败/返工：红色，需要处理（标签由调用方按 workflow 状态覆盖）。
  if (options.stepFailed) return state("失败", "failed");
  // 黄色只表示 HITL：等人工评审/确认。
  if (options.taskInReview || options.hasReviewStep
      || options.runStatus === "submitted" || options.runStatus === "reviewing") {
    return state("等待人工", "in_review");
  }
  if (options.retryWait) return state("等待重试", "queued");
  if (!rt) return state("未知/加载中", "unknown");
  if (rt.switching) return state("切换上下文", "in_progress");
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
  // 进程活着或上下文仍保留：空闲（绿），不是待办。
  if (rt.alive || options.hasContext) return state("空闲", "idle");
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
    case "green": return "idle";
    case "orange": return "in_progress";
    case "yellow": return "in_review";
    case "red": return "busy";
    default: return "todo";
  }
}

export function roleLiveDot(
  rt: RoleRuntimeStatus | undefined,
  taskId: string,
  options: {
    hasContext?: boolean;
    hasOpenStep?: boolean;
    allDone?: boolean;
    failed?: boolean;
    runStatus?: string | null;
    taskClosed?: boolean;
    taskDone?: boolean;
    queueAhead?: number;
    retryWait?: boolean;
    taskInReview?: boolean;
    hasReviewStep?: boolean;
    queuedHere?: number;
  } = {},
): RoleLiveDot {
  const hasContext = Boolean(options.hasContext);
  if (options.taskClosed) return { cls: "gray", title: "任务已终止" };
  if (options.taskDone) return { cls: "green", title: "任务已完成" };
  if (options.runStatus === "failed") {
    return { cls: "red", title: "本任务 run 失败，需要处理" };
  }
  if (options.runStatus === "abandoned") {
    return { cls: "gray", title: "本任务 run 已放弃/已中断" };
  }
  if (options.failed) return { cls: "red", title: "本任务步骤失败/返工，需要处理" };
  // 黄色只表示 HITL：任务/步骤/run 在等人工评审或确认。
  if (options.taskInReview || options.hasReviewStep
      || options.runStatus === "submitted" || options.runStatus === "reviewing") {
    return { cls: "yellow", title: "等待人工评审/确认" };
  }
  if (options.retryWait) return { cls: "red", title: "等待自动恢复重试" };
  if (!rt) return { cls: "gray", title: "状态加载中…" };
  if (rt.switching) return { cls: "orange", title: "正在切换任务上下文…" };
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
      cls: "orange",
      title: rt.current_step_id
        ? `正在本任务工作 · 步骤 ${rt.current_step_id}`
        : "正在本任务工作",
    };
  }
  // turn 之间但子任务尚未提交：上下文仍绑在本任务，视为仍在执行该子任务。
  if (options.hasOpenStep && rt.alive && rt.context_task_id === taskId) {
    return { cls: "orange", title: "本任务子任务进行中（等待下一轮）" };
  }
  const queueAhead = options.queueAhead ?? 0;
  if (queueAhead > 0) {
    return { cls: "red", title: "本任务消息排在其它任务之后" };
  }
  if ((options.queuedHere ?? 0) > 0 && rt.alive) {
    return { cls: "orange", title: "下一轮将处理本任务" };
  }
  if (options.allDone) return { cls: "green", title: "本任务步骤已完成" };
  if (rt.alive || hasContext) {
    return { cls: "green", title: hasContext
      ? "空闲 · 本任务上下文已保留"
      : "空闲 · 可安排任务" };
  }
  return { cls: "gray", title: "待办 · 尚未开始（首次进入将新建上下文）" };
}
