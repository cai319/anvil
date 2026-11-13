import type { RoleRuntimeStatus, Task, Workflow, WorkflowStep } from "./api/client";

/** 五色语义：橙=处理中，黄=等人工，红=需关注，绿=空闲/成功，灰=未开始/未参与/已结束。 */
export type StatusTone = "orange" | "yellow" | "red" | "green" | "gray";

export interface RoleStatus {
  key: string;
  label: string;
  tone: StatusTone;
  detail: string;
  title: string;
  queued: number;
  workingHere: boolean;
  busyElsewhere: boolean;
}

type WorkflowRoleInput = Pick<Workflow, "status" | "steps">;

interface WorkflowRoleSignal {
  hasSteps: boolean;
  failed: boolean;
  rejected: boolean;
  retryWait: boolean;
  running: boolean;
  submitted: boolean;
  pending: boolean;
  allDone: boolean;
}

function workflowRoleSignal(
  workflow: WorkflowRoleInput | null | undefined,
  role: string,
  sliceKey?: string | null,
): WorkflowRoleSignal {
  const steps = (workflow?.steps ?? []).filter((s) => s.role_slug === role
    && (sliceKey === undefined || (s.slice_key ?? null) === (sliceKey ?? null)));
  return {
    hasSteps: steps.length > 0,
    failed: steps.some((s) => s.status === "failed"),
    rejected: steps.some((s) => s.status === "rejected"),
    retryWait: steps.some((s) => s.status === "pending"
      && ((s.recovery_count ?? 0) > 0 || Boolean(s.next_retry_at))),
    running: steps.some((s) => s.status === "running"),
    submitted: steps.some((s) => s.status === "submitted"),
    pending: steps.some((s) => s.status === "pending"),
    allDone: steps.length > 0
      && steps.every((s) => s.status === "approved" || s.status === "skipped"),
  };
}

export interface TaskRoleStateInput {
  task: Task;
  role: string;
  runtime?: RoleRuntimeStatus;
  workflow?: WorkflowRoleInput | null;
  /** 当前任务 + 它的委派/派遣子任务；排队和上下文按这个集合归属。 */
  relatedTaskIds?: string[];
  sliceKey?: string | null;
  runStatus?: string | null;
  returnPending?: boolean;
}

function taskQueueCount(runtime: RoleRuntimeStatus | undefined, related: Set<string>): number {
  const byTask = runtime?.queue_tasks?.filter((q) => related.has(q.id)) ?? [];
  if (byTask.length > 0) {
    return byTask.reduce((sum, q) => sum + Math.max(0, q.count || 0), 0);
  }
  return (runtime?.queue_contexts ?? [])
    .filter((q) => q.task_id && related.has(q.task_id))
    .reduce((sum, q) => sum + Math.max(0, q.count || 0), 0);
}

/**
 * 任务内角色状态：回答“角色是否投入当前任务/切片/子任务”。
 * claim/task 状态只作为无运行时信息时的兜底，不能让 released 覆盖正在执行。
 */
export function taskRoleState(input: TaskRoleStateInput): RoleStatus {
  const {
    task, role, runtime, workflow, sliceKey, runStatus, returnPending = false,
  } = input;
  const related = new Set([task.id, ...(input.relatedTaskIds ?? [])]);
  const claim = task.role_claims?.find((c) => c.role_slug === role);
  const wf = workflowRoleSignal(workflow, role, sliceKey);

  const busy = Boolean(runtime?.alive && runtime.status === "busy");
  const busyTaskId = runtime?.current_task_id ?? null;
  const workingHere = Boolean(
    (busy && busyTaskId && related.has(busyTaskId))
    || (runtime?.alive && runtime.context_task_id && related.has(runtime.context_task_id)
      && wf.running),
  );
  const busyElsewhere = Boolean(busy && busyTaskId && !related.has(busyTaskId));
  const queued = taskQueueCount(runtime, related);
  const hasContext = Boolean(
    runtime?.contexts?.some((c) => c.task_id && related.has(c.task_id) && c.has_session),
  );

  const taskClosed = task.status === "closed" || workflow?.status === "terminated";
  const taskDone = task.status === "done";
  const taskInReview = task.status === "in_review";
  const runFailed = runStatus === "failed";
  const runWaiting = runStatus === "submitted" || runStatus === "reviewing";
  const failed = runFailed || wf.failed || wf.rejected;
  const waitingHuman = taskInReview || runWaiting || wf.submitted;
  const allDone = taskDone || claim?.status === "approved" || wf.allDone;
  const released = claim?.status === "released";
  const pending = claim?.status === "pending"
    || (wf.hasSteps && !wf.running && !wf.submitted && !failureSignal(wf) && !wf.allDone);

  const base = {
    queued,
    workingHere,
    busyElsewhere,
  };

  if (workingHere) {
    const step = runtime?.current_step_id;
    return make("working", "进行中", "orange",
      step ? `正在本任务工作 · 步骤 ${step}` : "正在本任务工作", base);
  }
  if (taskClosed) {
    return make("closed", "已终止", "gray", "任务已终止", base);
  }
  if (taskDone) {
    return make("done", "完成", "green", "本任务工作已完成", base);
  }
  if (failed) {
    const rework = wf.rejected && !wf.failed;
    return make(rework ? "rework" : "failed",
      rework ? "返工" : "失败", "red",
      rework ? "本任务步骤被驳回，等待返工" : "本任务 run/步骤失败，需要处理", base);
  }
  if (queued > 0) {
    return make("queued", "排队中", "red",
      `本任务有待处理队列 ${queued} 条；包括 @、委派、指派、派遣和回复`, base);
  }
  if (wf.retryWait) {
    return make("retry", "等待重试", "red",
      "本任务步骤等待自动恢复重试", base);
  }
  if (waitingHuman) {
    return make("waiting_human", "等待人工", "yellow",
      "等待人工评审/确认", base);
  }
  if (allDone) {
    return make("done", "完成", "green", "本任务工作已完成", base);
  }
  if (busyElsewhere && queued === 0) {
    return make("not_involved", "未参与", "gray",
      "正在其他任务工作；本任务没有排队", base);
  }
  if (released) {
    return make("released", "已释放", "gray", "已释放任务认领", base);
  }
  if (returnPending) {
    return make("return_pending", "等待回切", "orange",
      "等待派遣任务回切原切片", base);
  }
  if (hasContext && !busyElsewhere) {
    return make("idle", "空闲", "green", "空闲 · 本任务上下文已保留", base);
  }
  if (pending) {
    return make("todo", "待办", "gray", "待办 · 尚未开始", base);
  }
  return make("not_involved", "未参与", "gray",
    busyElsewhere ? "正在其他任务工作；本任务没有排队" : "本任务没有该角色的活跃上下文",
    base);
}

function failureSignal(wf: WorkflowRoleSignal): boolean {
  return wf.failed || wf.rejected;
}

function make(
  key: string,
  label: string,
  tone: StatusTone,
  detail: string,
  base: Pick<RoleStatus, "queued" | "workingHere" | "busyElsewhere">,
): RoleStatus {
  return {
    key, label, tone, detail,
    title: `${label} · ${detail}`,
    ...base,
  };
}

export interface GlobalRoleStateInput {
  runtime?: RoleRuntimeStatus;
  latestRunStatus?: string | null;
}

/** 全局角色状态：回答“角色整体上现在是否在工作”。 */
export function globalRoleState(input: GlobalRoleStateInput): RoleStatus {
  const { runtime, latestRunStatus } = input;
  const busy = Boolean(runtime?.alive && runtime.status === "busy");
  const queue = Math.max(0, runtime?.queue_len ?? 0);
  const failed = latestRunStatus === "failed";
  const waitingHuman = latestRunStatus === "submitted" || latestRunStatus === "reviewing";
  const runActive = latestRunStatus === "created" || latestRunStatus === "projecting"
    || latestRunStatus === "running";
  const hasContext = Boolean(runtime?.contexts?.some((c) => c.has_session));
  const base = { queued: queue, workingHere: busy, busyElsewhere: false };

  // runtime 尚未加载时，用当前 run 作为过渡证据，避免活跃会话先闪成灰色。
  if (!runtime && runActive) {
    return make("working", "进行中", "orange", "正在处理当前会话", base);
  }
  if (busy) {
    const task = runtime?.current_task?.title ?? runtime?.current_task_id;
    return make("working", "进行中", "orange",
      task ? `正在处理「${task}」` : "正在处理系统任务", base);
  }
  if (runtime?.switching) {
    return make("switching", "切换上下文", "orange", "正在切换任务上下文", base);
  }
  if (failed) {
    return make("failed", "失败", "red", "最近一次 run 失败", base);
  }
  if (queue > 0) {
    return make("queued", "排队中", "red",
      `有 ${queue} 条待处理队列；包括 @、委派、指派、派遣和回复`, base);
  }
  if (waitingHuman) {
    return make("waiting_human", "等待人工", "yellow", "等待人工评审/确认", base);
  }
  if (runtime?.alive) {
    return make("idle", "空闲", "green", "空闲 · 可安排任务", base);
  }
  if (hasContext) {
    return make("suspended", "等待唤醒", "gray", "进程已退出，上下文仍保留", base);
  }
  return make("offline", "未运行", "gray", "进程已退出且无上下文", base);
}

/** workflow step 的 tone；供 slice 圆点/节点和兼容旧状态类使用。 */
export function workflowStepTone(status: WorkflowStep["status"]): StatusTone {
  switch (status) {
    case "running": return "orange";
    case "submitted": return "yellow";
    case "approved": return "green";
    case "rejected":
    case "failed": return "red";
    default: return "gray";
  }
}

/** slice 聚合：failed > rejected > running > submitted > 全部完成 > pending。 */
export function workflowSliceState(steps: WorkflowStep[]): {
  key: string;
  label: string;
  tone: StatusTone;
} {
  if (steps.some((s) => s.status === "failed")) {
    return { key: "failed", label: "失败", tone: "red" };
  }
  if (steps.some((s) => s.status === "rejected")) {
    return { key: "rejected", label: "返工", tone: "red" };
  }
  if (steps.some((s) => s.status === "running")) {
    return { key: "running", label: "进行中", tone: "orange" };
  }
  if (steps.some((s) => s.status === "submitted")) {
    return { key: "submitted", label: "待审核", tone: "yellow" };
  }
  if (steps.length > 0
      && steps.every((s) => s.status === "approved" || s.status === "skipped")) {
    return { key: "done", label: "完成", tone: "green" };
  }
  return { key: "pending", label: "待办", tone: "gray" };
}

/** 兼容旧组件传入的状态字符串；新代码直接用 tone。 */
export function legacyStatusTone(status?: string | null): StatusTone | undefined {
  if (!status) return undefined;
  if (status === "orange" || status === "yellow" || status === "red"
      || status === "green" || status === "gray") {
    return status;
  }
  switch (status) {
    case "working":
    case "running":
    case "projecting":
    case "in_progress":
      return "orange";
    case "submitted":
    case "in_review":
    case "reviewing":
      return "yellow";
    case "queued":
    case "busy":
    case "failed":
    case "error":
    case "rejected":
      return "red";
    case "approved":
    case "completed":
    case "done":
    case "idle":
      return "green";
    case "pending":
    case "released":
    case "closed":
    case "abandoned":
    case "suspended":
      return "gray";
    default:
      return "gray";
  }
}
