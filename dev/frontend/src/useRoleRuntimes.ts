import { useEffect, useRef, useState } from "react";
import { api, type RoleRuntimeStatus, type Task } from "./api/client";
import { useLiveRefresh } from "./useLiveRefresh";

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

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = () => {
    if (!task) return;
    for (const slug of slugs) {
      api.getRoleRuntime(slug)
        .then((rt) => {
          if (alive.current) setRuntimes((prev) => ({ ...prev, [slug]: rt }));
        })
        .catch(() => {});
    }
  };

  useEffect(() => {
    load();
  }, [task?.id, task?.status, slugKey]);

  useLiveRefresh(load, {
    enabled: Boolean(task),
    initial: false,
    topics: ["task", "tick"],
    tickMinGapMs: 10_000,
    pollMs: 30_000,
  });

  return { slugs, runtimes };
}
