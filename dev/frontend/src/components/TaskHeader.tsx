import type { Task } from "../api/client";
import { UNBOUND_CHANNEL_VIEW } from "../channelView";
import { DISPATCH_STATUS_META, TASK_STATUS_META } from "../statusVocabulary";
import { Icon } from "./Icon";

export function TaskHeader({ task }: { task: Task }) {
  return (
    <div className="session-head">
      <div className="session-title">
        <h1>{task.title}</h1>
        <span className={`status status-${task.status}`}>
          {TASK_STATUS_META[task.status].label}
        </span>
        {task.archived_at && <span className="chip">已归档</span>}
        {task.source_task_id && (
          <a className="chip" href={`/tasks/${task.source_task_id}`}
            title="派遣子任务的来源任务（parent_id 指向来源 task）">
            来自任务 {task.source_task_id.slice(0, 8)}
            {task.source_slice_key ? ` / 切片 ${task.source_slice_key}` : ""}
          </a>
        )}
        {task.dispatch_status && task.dispatch_status !== "none" && (
          <span className="chip">
            {DISPATCH_STATUS_META[task.dispatch_status]?.label ?? task.dispatch_status}
          </span>
        )}
      </div>
      <div className="session-meta">
        {task.workspace ? (
          <a className="session-meta-item"
            href={`/channels?channel=${encodeURIComponent(task.workspace.id)}&tab=knowledge`}
            title="打开频道知识">
            <Icon name="hash" /> {task.workspace.name}
          </a>
        ) : (
          <a className="session-meta-item"
            href={`/channels?channel=${UNBOUND_CHANNEL_VIEW}`}
            title="查看未绑定频道的任务">
            <Icon name="hash" /> 未归属频道
          </a>
        )}
        {task.assignee && (
          <span className="session-meta-item"><Icon name="user" /> {task.assignee}</span>
        )}
        {task.assignees?.length > 0 ? (
          <span className="session-meta-item"><Icon name="users" /> {task.assignees.join("、")}</span>
        ) : !task.assignee ? (
          <span className="muted">未指派</span>
        ) : null}
        {task.tags?.length > 0 && <span className="muted">#{task.tags.join(" #")}</span>}
        <span className="muted mono">{task.id}</span>
      </div>
    </div>
  );
}
