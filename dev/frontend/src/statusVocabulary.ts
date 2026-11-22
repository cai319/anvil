import type { RunStatus, Task } from "./api/client";

export interface StatusMeta {
  label: string;
  tone: "todo" | "in_progress" | "in_review" | "done" | "closed";
}

export const TASK_STATUS_META: Record<Task["status"], StatusMeta> = {
  todo: { label: "待办", tone: "todo" },
  in_progress: { label: "进行中", tone: "in_progress" },
  in_review: { label: "待评审", tone: "in_review" },
  done: { label: "完成", tone: "done" },
  closed: { label: "已终止", tone: "closed" },
};

export const RUN_STATUS_META: Record<RunStatus, StatusMeta> = {
  created: { label: "已创建", tone: "todo" },
  projecting: { label: "准备中", tone: "in_progress" },
  running: { label: "运行中", tone: "in_progress" },
  submitted: { label: "已提交", tone: "in_review" },
  reviewing: { label: "评审中", tone: "in_review" },
  completed: { label: "已完成", tone: "done" },
  failed: { label: "失败", tone: "closed" },
  abandoned: { label: "已放弃", tone: "closed" },
  suspended: { label: "等待唤醒", tone: "in_progress" },
};

/** 回放/时间线用的短标签（与常规语气标签区分）。 */
export const RUN_STATUS_SHORT_LABEL: Record<RunStatus, string> = {
  created: "创建",
  projecting: "准备",
  running: "运行",
  submitted: "提交",
  reviewing: "评审",
  completed: "完成",
  failed: "失败",
  abandoned: "中止",
  suspended: "等待唤醒",
};

export const DISPATCH_STATUS_META: Record<string, StatusMeta> = {
  open: { label: "进行中", tone: "in_progress" },
  return_ready: { label: "待回切", tone: "in_progress" },
  returning: { label: "回切中", tone: "in_progress" },
  returned: { label: "已回切", tone: "done" },
  no_return: { label: "不回切", tone: "closed" },
  cancelled: { label: "已取消", tone: "closed" },
};

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_META[status as Task["status"]]?.label ?? status;
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_META[status as RunStatus]?.label ?? status;
}

export function dispatchStatusLabel(status: string): string {
  return DISPATCH_STATUS_META[status]?.label ?? status;
}

export function runStatusShortLabel(status: string): string {
  return RUN_STATUS_SHORT_LABEL[status as RunStatus] ?? status;
}
