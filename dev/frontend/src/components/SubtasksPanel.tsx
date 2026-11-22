import type { TaskChild } from "../api/client";
import { ChildTaskCard } from "./ChildTaskCard";

export function SubtasksPanel({ items, onChanged }: {
  items: TaskChild[]; onChanged: () => void;
}) {
  const groups = new Map<string, { title: string; items: TaskChild[] }>();
  for (const c of items) {
    const key = c.parent_step_id || "__none__";
    const title = c.step_title || "未关联步骤";
    const group = groups.get(key) ?? { title, items: [] };
    group.items.push(c);
    groups.set(key, group);
  }
  const delegationCount = items.filter((c) => c.relation_type === "delegation").length;
  const dispatchCount = items.filter((c) => c.relation_type === "dispatch").length;
  const pending = items.filter(
    (c) => c.status !== "done" && c.status !== "closed").length;
  const waiting = items.filter(
    (c) => c.relation_type === "delegation" && c.status === "in_review").length;
  const returning = items.filter((c) => c.return_pending).length;
  return (
    <div className="delegations">
      <div className="delegation-summary">
        <strong>子任务 {items.length}</strong>
        <span> · 委派 {delegationCount}</span>
        <span> · 派遣 {dispatchCount}</span>
        {pending > 0 && <span> · 未完成 {pending}</span>}
        {waiting > 0 && <span> · 待验收 {waiting}</span>}
        {returning > 0 && <span> · 待回切 {returning}</span>}
      </div>
      {items.length === 0 && (
        <p className="muted">
          当前任务没有委派或派遣子任务。委派用于另一个角色独立完成一块工作，
          会阻塞父 step 并需验收；派遣为独立上下文的非阻塞任务，完成后按回切状态处理。
          并行只读探索优先用运行时 subagent。
        </p>
      )}
      {Array.from(groups.entries()).map(([key, group]) => (
        <section key={key} className="delegation-group">
          <h3 className="delegation-group-title">
            {group.title} <span className="muted">{group.items.length}</span>
          </h3>
          {group.items.map((c) => (
            <ChildTaskCard
              key={`${c.relation_type}:${c.task_id}`}
              c={c}
              onChanged={onChanged}
            />
          ))}
        </section>
      ))}
      {delegationCount > 0 && (
        <p className="muted">
          委派在父 step 提交前需全部完成；派遣不阻塞、不共享对话。
        </p>
      )}
    </div>
  );
}
