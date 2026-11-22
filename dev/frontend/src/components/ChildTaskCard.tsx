import { useState } from "react";
import {
  api, type Delegation, type Task, type TaskChild, type TaskDispatch,
} from "../api/client";
import { DISPATCH_STATUS_META } from "../statusVocabulary";
import { formatClock, formatDateTime } from "../time";
import { confirmDialog, promptDialog } from "./dialogs";

export type ChildState = {
  label: string;
  cls: "todo" | "in_progress" | "in_review" | "done" | "closed";
};

/** 委派/派遣共用同一套状态词表；差异只在待验收/待回切等关系语义。 */
export function childState(c: TaskChild): ChildState {
  if (c.relation_type === "dispatch" && c.return_pending)
    return { label: "待回切", cls: "in_progress" };
  if (c.status === "done") return { label: "已完成", cls: "done" };
  if (c.status === "closed")
    return { label: c.relation_type === "delegation" ? "已取消" : "已终止", cls: "closed" };
  if (c.status === "in_review")
    return { label: c.relation_type === "delegation" ? "待验收" : "待评审", cls: "in_review" };
  if (c.run_status === "failed" || c.run_status === "abandoned")
    return { label: "异常", cls: "closed" };
  if (c.run_status === "running") return { label: "进行中", cls: "in_progress" };
  if (c.run_status === "created" || c.run_status === "projecting")
    return { label: "排队中", cls: "in_progress" };
  if (c.status === "in_progress") return { label: "进行中", cls: "in_progress" };
  return { label: "待认领", cls: "todo" };
}

/** 旧后端（无 /children 端点）时的兼容投影，待服务端重启后自然退场。 */
export function childFromDelegation(d: Delegation): TaskChild {
  return {
    relation_type: "delegation",
    relation_id: null,
    task_id: d.id,
    title: d.title,
    status: d.status,
    type: d.type,
    assignee: d.assignee,
    assignees: d.assignees ?? [],
    created_by: d.created_by,
    parent_step_id: d.parent_step_id,
    step_title: d.step_title,
    due_at: d.due_at,
    acceptance: d.acceptance ?? [],
    scope_paths: d.scope_paths ?? [],
    description: d.description,
    submitted_summary: d.submitted_summary,
    run_id: d.run_id,
    run_status: d.run_status,
    run_failure_class: d.run_failure_class,
    blocking: true,
    dispatch_status: null,
    return_pending: false,
    return_context_key: null,
    returned_at: null,
    created_at: d.created_at,
    updated_at: d.updated_at,
    raw: d,
  };
}

export function childFromDispatch(d: TaskDispatch): TaskChild {
  return {
    relation_type: "dispatch",
    relation_id: d.id,
    task_id: d.target_task_id,
    title: d.target_title || d.target_task_id,
    status: (d.target_status as Task["status"]) || "todo",
    type: "dispatch",
    assignee: d.target_role,
    assignees: d.target_role ? [d.target_role] : [],
    created_by: d.requested_by
      ? (d.requested_by === "human" ? "human" : `agent:${d.requested_by}`)
      : null,
    parent_step_id: d.source_step_id,
    step_title: null,
    due_at: null,
    acceptance: [],
    scope_paths: [],
    description: null,
    submitted_summary: null,
    run_id: null,
    run_status: null,
    run_failure_class: null,
    blocking: false,
    dispatch_status: d.status,
    return_pending: d.status === "return_ready" || d.status === "returning",
    return_context_key: d.return_context_key,
    returned_at: d.returned_at,
    created_at: d.created_at,
    updated_at: d.updated_at,
    raw: d,
  };
}

export function ChildTaskCard({ c, onChanged }: { c: TaskChild; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const state = childState(c);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const nudge = () => {
    if (!c.assignee) return;
    void run(() => api.mentionTask(c.task_id, c.assignee!, "催办：请同步子任务进展"));
  };

  const accept = () => {
    void run(() => api.acceptDelegation(c.task_id));
  };

  const reject = async () => {
    const feedback = await promptDialog({
      title: "打回委派",
      message: "请说明需要负责人返工的问题。",
      input: { label: "打回原因", multiline: true },
      inputRequired: true,
      confirmLabel: "打回",
      danger: true,
    });
    if (!feedback) return;
    void run(() => api.rejectDelegation(c.task_id, feedback));
  };

  const cancel = async () => {
    const ok = await confirmDialog({
      title: "取消委派？",
      message: "取消后视为该委派已解除，父 step 会被自动唤醒继续；子任务保留审计但不再参与门禁。",
      confirmLabel: "取消委派",
      danger: true,
    });
    if (!ok) return;
    void run(() => api.cancelTask(c.task_id));
  };

  const isDelegation = c.relation_type === "delegation";
  // 只有绑定父 step 的委派工单才有验收/打回语义。
  const canAccept = isDelegation && Boolean(c.parent_step_id) && c.status === "in_review";
  const canCancel = isDelegation && (c.status === "todo" || c.status === "in_progress");
  const requester = c.created_by?.startsWith("agent:")
    ? c.created_by.slice("agent:".length)
    : (c.created_by || "—");
  const owner = c.assignee || (c.assignees?.length ? c.assignees.join("、") : "无人认领");

  return (
    <div className={`delegation-card delegation-${state.cls}`}>
      <div className="delegation-head">
        <a className="delegation-title" href={`/tasks/${c.task_id}`}>{c.title}</a>
        <span className={`chip relation-${c.relation_type}`}>
          {isDelegation ? "委派" : "派遣"}
        </span>
        <span className={`status status-${state.cls}`}>{state.label}</span>
        {c.run_failure_class && (
          <span className="badge badge-in_progress" title="失败分类">{c.run_failure_class}</span>
        )}
      </div>
      <div className="delegation-meta">
        <span>{requester} → {owner}</span>
        <span>{c.step_title || "未关联步骤"}</span>
        {isDelegation ? (
          <span>截止：{c.due_at ? formatDateTime(c.due_at) : "未设置"}</span>
        ) : (
          c.dispatch_status && (
            <span>派遣：{DISPATCH_STATUS_META[c.dispatch_status]?.label ?? c.dispatch_status}</span>
          )
        )}
        {c.created_at && <span className="muted">{formatClock(c.created_at)}</span>}
      </div>
      {c.acceptance?.length > 0 && (
        <div className="delegation-acceptance">验收：{c.acceptance.join("；")}</div>
      )}
      {c.scope_paths?.length > 0 && (
        <div className="muted">scope：{c.scope_paths.join("、")}</div>
      )}
      {c.submitted_summary && (
        <div className="delegation-result">结果：{c.submitted_summary}</div>
      )}
      {!isDelegation && c.return_context_key && (
        <div className="muted mono" title="完成后回切的上下文">回切：{c.return_context_key}</div>
      )}
      <div className="delegation-actions">
        <a href={`/tasks/${c.task_id}`}>{isDelegation ? "打开详情" : "打开派遣任务"}</a>
        {c.assignee && (
          <button className="small" disabled={busy} onClick={nudge}>催办</button>
        )}
        {canAccept && (
          <button className="small" disabled={busy} onClick={accept}>通过验收</button>
        )}
        {canAccept && (
          <button className="small" disabled={busy} onClick={reject}>打回</button>
        )}
        {canCancel && (
          <button className="small danger" disabled={busy} onClick={cancel}>取消</button>
        )}
        {c.returned_at && <span className="muted">已回切 {formatClock(c.returned_at)}</span>}
      </div>
    </div>
  );
}
