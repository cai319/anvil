import { useEffect, useState } from "react";
import { api, subscribeEvents, type RoleRuntimeStatus, type Task } from "./api/client";

/** 任务相关角色的实时运行时状态（忙碌任务、队列、各任务上下文槽）。 */
export function useRoleRuntimes(task: Task | null) {
  const [runtimes, setRuntimes] = useState<Record<string, RoleRuntimeStatus>>({});
  const slugs = task
    ? Array.from(new Set([
      ...(task.assignees?.length
        ? task.assignees
        : (task.role_claims ?? []).map((c) => c.role_slug)),
      ...(task.assignee ? [task.assignee] : []),
      ...(task.planner ? [task.planner] : []),
    ])).filter(Boolean)
    : [];
  const slugKey = slugs.join(",");

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      if (!task) return;
      for (const slug of slugs) {
        api.getRoleRuntime(slug)
          .then((rt) => {
            if (!cancelled) setRuntimes((prev) => ({ ...prev, [slug]: rt }));
          })
          .catch(() => {});
      }
    };
    load();
    const off = subscribeEvents((ev) => {
      if (ev.topic === "task" || ev.topic === "tick") load();
    });
    return () => {
      cancelled = true;
      off();
    };
  }, [task?.id, task?.status, slugKey]);

  return { slugs, runtimes };
}
