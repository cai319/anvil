import { useEffect, useMemo, useState } from "react";
import { api, subscribeEvents, type Channel, type Role, type RoleRuntimeStatus, type Run, type Task, type TaskRoleStatusSummary, type Workflow } from "../api/client";
import { navigate } from "../router";
import { formatClock, formatDateTime, formatRelative, parseTime, useNow } from "../time";
import { usePanelLayout } from "../usePanelLayout";
import { NewTaskModal } from "./NewTaskModal";
import { RoleAvatar } from "../components/RoleAvatar";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/ui";
import { globalRoleState, taskRoleState, type StatusTone } from "../roleStatus";

const COLUMNS: Task["status"][] = ["todo", "in_progress", "in_review", "done", "closed"];
const MAX_CARD_TAGS = 2;
const STATUS_LABEL: Record<Task["status"], string> = {
  todo: "待办",
  in_progress: "进行中",
  in_review: "待评审",
  done: "完成",
  closed: "已终止",
};

interface Activity {
  time: string;
  role: string | null;
  task: string | null;
  action: string;
  detail?: string;
}

const ACTION_LABEL: Record<string, string> = {
  claim: "认领",
  unclaim: "放弃",
  submit: "提交",
  review: "评审",
  heartbeat: "心跳",
  wake: "唤醒",
  starvation: "饥饿升级",
  delete: "删除",
  cancel: "人工终止",
  reopen: "重新打开",
  plan: "规划",
};

/** 活跃会话的 run 状态（常驻模式下 suspended = 等待下一条消息）。 */
const RUN_STATUS_LABEL: Record<string, string> = {
  created: "已创建",
  projecting: "准备中",
  running: "运行中",
  submitted: "已提交",
  reviewing: "评审中",
  suspended: "等待唤醒",
};

/** 任务卡头像：只显示头像，阴影色表示该角色是否投入这张任务。 */
function TaskRoleAvatar({ task, slug, roles, runtime, relatedTaskIds, workflow, runStatus }: {
  task: Task;
  slug: string;
  roles: Role[];
  runtime?: RoleRuntimeStatus;
  relatedTaskIds: string[];
  workflow?: Pick<Workflow, "status" | "steps"> | null;
  runStatus?: string | null;
}) {
  const role = roles.find((r) => r.slug === slug);
  const state = taskRoleState({
    task, role: slug, runtime, relatedTaskIds, workflow, runStatus,
  });
  const name = role?.display_name ?? slug;
  return (
    <RoleAvatar
      slug={slug}
      name={name}
      avatar={role?.avatar}
      color={role?.avatar_color}
      size={28}
      tone={state.tone}
      label={`${name} · ${state.label} · ${state.detail}`}
    />
  );
}

function fmtElapsed(start: string | null, now = Date.now()): string {
  const d = parseTime(start);
  if (!d) return "";
  const s = Math.max(0, Math.round((now - d.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

function describe(a: Record<string, unknown>): Activity {
  const action = String(a.action ?? "event");
  const label = ACTION_LABEL[action] ?? action;
  return {
    time: String(a.created_at ?? ""),
    role: (a.role_slug as string) ?? null,
    task: (a.task_id as string) ?? null,
    action: label,
  };
}

async function moveTask(task: Task, target: Task["status"]): Promise<unknown> {
  const s = task.status;
  if (s === target) return task;
  if (s === "todo" && target === "in_progress") {
    const team = task.assignees ?? [];
    if (team.length) await api.assignRoles(task.id, team);
    // 0 角色时 Cindy 先分析并建议角色；多角色任务默认先由 Cindy 规划；
    // 单角色任务直接派发。
    await api.dispatchTask(task.id);
    return task;
  }
  if (s === "in_progress" && target === "in_review") return api.submitTask(task.id);
  if (s === "in_review" && target === "done") return api.reviewTask(task.id, true);
  if (s === "in_review" && target === "in_progress") return api.reviewTask(task.id, false);
  if (s === "in_progress" && target === "todo") return api.unclaimTask(task.id);
  throw new Error(`不支持 ${s} -> ${target}`);
}

export function Lobby() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [roleRuntimes, setRoleRuntimes] = useState<RoleRuntimeStatus[]>([]);
  const [taskSummaries, setTaskSummaries] = useState<Map<string, TaskRoleStatusSummary>>(
    () => new Map(),
  );
  const [activity, setActivity] = useState<Activity[]>([]);
  const [showNew, setShowNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const panel = usePanelLayout({ storageKey: "lobby" });
  const now = useNow(30_000); // 运行时长按系统当前时间刷新

  const runtimeByRole = new Map(roleRuntimes.map((rt) => [rt.role_slug, rt]));
  // 一个角色同一时刻只保留一个实时会话：优先当前上下文任务，其次最近创建的 run。
  // 已完成任务遗留的 suspended run 仍会留在数据库，但不再重复占据实时面板。
  const visibleRuns = useMemo(() => {
    const startedAt = (run: Run) => new Date(run.started_at ?? 0).getTime();
    const byRole = new Map<string, Run>();
    for (const run of runs) {
      const current = byRole.get(run.role_slug);
      if (!current) {
        byRole.set(run.role_slug, run);
        continue;
      }
      const contextTask = runtimeByRole.get(run.role_slug)?.context_task_id;
      const runIsCurrent = Boolean(contextTask && run.task_id === contextTask);
      const currentIsCurrent = Boolean(contextTask && current.task_id === contextTask);
      if (runIsCurrent !== currentIsCurrent) {
        if (runIsCurrent) byRole.set(run.role_slug, run);
      } else if (startedAt(run) > startedAt(current)) {
        byRole.set(run.role_slug, run);
      }
    }
    return [...byRole.values()].sort((a, b) => startedAt(b) - startedAt(a));
  }, [runs, roleRuntimes]);

  // 任务卡的角色状态按“当前任务 + 其委派/派遣子任务”归属队列与上下文。
  const relatedTaskIds = useMemo(() => {
    const children = new Map<string, Set<string>>();
    for (const t of tasks) {
      for (const parent of [t.parent_id, t.source_task_id]) {
        if (!parent) continue;
        const set = children.get(parent) ?? new Set<string>();
        set.add(t.id);
        children.set(parent, set);
      }
    }
    const out = new Map<string, string[]>();
    for (const t of tasks) {
      const ids: string[] = [];
      const walk = (id: string) => {
        if (ids.includes(id)) return;
        ids.push(id);
        for (const child of children.get(id) ?? []) walk(child);
      };
      walk(t.id);
      out.set(t.id, ids);
    }
    return out;
  }, [tasks]);

  const runStatusByTaskRole = useMemo(() => {
    const out = new Map<string, { status: string; started: string }>();
    for (const r of runs) {
      if (!r.task_id) continue;
      const key = `${r.task_id}:${r.role_slug}`;
      const started = r.started_at ?? "";
      const current = out.get(key);
      if (!current || started > current.started) {
        out.set(key, { status: r.status, started });
      }
    }
    return out;
  }, [runs]);

  // 终态 run 不在 active runs 里；用批量摘要补上“最近一次 run”，大厅才能显示失败/等待人工。
  const latestRunStatusByRole = useMemo(() => {
    const out = new Map<string, { status: string; started: string }>();
    for (const summary of taskSummaries.values()) {
      for (const [role, info] of Object.entries(summary.run_statuses)) {
        const started = info.started_at ?? "";
        const current = out.get(role);
        if (!current || started > current.started) {
          out.set(role, { status: info.status, started });
        }
      }
    }
    return out;
  }, [taskSummaries]);

  const globalToneByRole = useMemo(() => {
    const out: Record<string, StatusTone> = {};
    for (const role of roles) {
      const visible = visibleRuns.find((r) => r.role_slug === role.slug);
      out[role.slug] = globalRoleState({
        runtime: runtimeByRole.get(role.slug),
        latestRunStatus: latestRunStatusByRole.get(role.slug)?.status
          ?? visible?.status ?? null,
      }).tone;
    }
    return out;
  }, [roles, visibleRuns, roleRuntimes, latestRunStatusByRole]);

  const refresh = async () => {
    try {
      const [t, r, n, rt, cs] = await Promise.all([
        api.listTasks(),
        api.listRoles(),
        api.listActiveRuns(),
        api.listRoleRuntimes().catch(() => [] as RoleRuntimeStatus[]),
        api.listChannels().catch(() => [] as Channel[]),
      ]);
      setTasks(t);
      setRoles(r);
      setRuns(n);
      setRoleRuntimes(rt);
      setChannels(cs);
      const ids = t.map((task) => task.id);
      const summaries = ids.length
        ? await api.getTaskStatusSummaries(ids).catch(() => [])
        : [];
      setTaskSummaries(new Map(summaries.map((s) => [s.task_id, s])));
    } catch (e) {
      setError(String(e));
    }
  };

  const refreshActivity = () =>
    api.listAudit({ limit: 200 })
      .then((a) => setActivity((a as Record<string, unknown>[]).map(describe)))
      .catch(() => {});

  useEffect(() => {
    refresh();
    refreshActivity();
    return subscribeEvents((ev) => {
      if (ev.topic === "task" || ev.topic === "tick" || ev.topic === "run") refresh();
      if (ev.topic === "task") {
        const p = ev.payload as Record<string, unknown>;
        setActivity((prev) =>
          [
            { time: ev.at, role: null, task: String(p.id ?? ""), action: `任务 ${STATUS_LABEL[String(p.status) as Task["status"]] ?? p.status}` },
            ...prev,
          ].slice(0, 100)
        );
      }
    });
  }, []);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const onDrop = async (target: Task["status"]) => {
    if (!dragId) return;
    const task = tasks.find((t) => t.id === dragId);
    setDragId(null);
    if (!task || task.status === target) return;
    await act(task.id, () => moveTask(task, target));
  };

  // 大厅固定展示全部未归档任务（归档由后端默认过滤）；频道内任务在频道页查看。
  // workflow 步骤不是 task，这里只展示高层任务。
  const boardTasks = tasks.filter((t) => !t.parent_id);
  const channelName = (id?: string | null) => {
    if (!id) return "未归属频道";
    return channels.find((c) => c.id === id)?.name ?? id.slice(0, 8);
  };

  // 右栏只保留最近 30 分钟的活动，且过滤心跳噪声；完整历史在 /activity。
  const cutoff = Date.now() - 30 * 60 * 1000;
  const recentActivity = activity
    .filter((a) => a.action !== "心跳" && a.action !== "删除"
      && a.time && new Date(a.time).getTime() >= cutoff)
    .slice(0, 20);

  return (
    <div className="lobby-page">
      <PageHeader
        kicker="MISSION BOARD"
        title="任务大厅"
        subtitle={`${roles.length} 个角色 · ${visibleRuns.length} 个活跃会话`}
        actions={
          <>
            {panel.collapsed && (
              <button className="small" onClick={panel.toggle}>显示右栏</button>
            )}
            <button className="small" onClick={() =>
              act("archive-completed", () => api.archiveCompletedTasks())
            }>归档已完成</button>
            <button className="primary new-task-btn" onClick={() => setShowNew(true)}>
              <Icon name="plus" /> 新建任务
            </button>
          </>
        }
      />
      <p className="muted" style={{ marginBottom: 12 }}>
        全部未归档任务。选择参与成员后创建；多角色任务会先由 Cindy 规划工作流。
      </p>

      {error && <p className="error">{error}</p>}

      <div
        className={`lobby ${panel.collapsed ? "panel-collapsed" : ""}`}
        style={panel.style}
      >
        {/* 中栏：任务看板 */}
        <section className="lobby-main">
          <div className="kanban">
            {COLUMNS.map((status) => (
              <div
                key={status}
                className="kanban-col"
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => onDrop(status)}
              >
                <h2>
                  {STATUS_LABEL[status]}
                  <span className="n">{boardTasks.filter((t) => t.status === status).length}</span>
                </h2>
                {boardTasks.filter((t) => t.status === status).map((t) => {
                  const summary = taskSummaries.get(t.id);
                  return (
                  <div
                    key={t.id}
                    className="card"
                    draggable
                    onDragStart={() => setDragId(t.id)}
                    onDragEnd={() => setDragId(null)}
                  >
                    <a className="card-link" href={`/tasks/${t.id}`} draggable={false}>
                      <strong className="card-title" title={t.title}>{t.title}</strong>
                      {t.parent_id && (
                        <div className="card-tags" title="派遣子任务：挂在来源 task 下，不进入顶层大厅">
                          <span className="card-tag">
                            派遣任务 · 来自 {t.parent_id.slice(0, 8)}
                            {t.source_slice_key ? ` / ${t.source_slice_key}` : ""}
                          </span>
                        </div>
                      )}
                      <div className="card-meta">
                        {t.assignees?.length ? (
                          <span className="card-roles">
                            {t.assignees.map((slug) => (
                              <TaskRoleAvatar
                                key={slug}
                                task={t}
                                slug={slug}
                                roles={roles}
                                runtime={runtimeByRole.get(slug)}
                                relatedTaskIds={relatedTaskIds.get(t.id) ?? [t.id]}
                                workflow={summary?.workflow}
                                runStatus={summary?.run_statuses?.[slug]?.status
                                  ?? runStatusByTaskRole.get(`${t.id}:${slug}`)?.status}
                              />
                            ))}
                          </span>
                        ) : t.assignee ? (
                          <span className="card-roles">
                            <TaskRoleAvatar
                              task={t}
                              slug={t.assignee}
                              roles={roles}
                              runtime={runtimeByRole.get(t.assignee)}
                              relatedTaskIds={relatedTaskIds.get(t.id) ?? [t.id]}
                              workflow={summary?.workflow}
                              runStatus={summary?.run_statuses?.[t.assignee]?.status
                                ?? runStatusByTaskRole.get(`${t.id}:${t.assignee}`)?.status}
                            />
                          </span>
                        ) : (
                          <span className="muted">未指派</span>
                        )}
                      </div>
                      {t.tags?.length > 0 && (
                        <div
                          className="card-tags"
                          title={`标签：${t.tags.map((tag) => `#${tag}`).join(" ")}`}
                        >
                          {t.tags.slice(0, MAX_CARD_TAGS).map((tag) => (
                            <span className="card-tag" key={tag}>#{tag}</span>
                          ))}
                          {t.tags.length > MAX_CARD_TAGS && (
                            <span
                              className="card-tag card-tag-more"
                              aria-label={`另外 ${t.tags.length - MAX_CARD_TAGS} 个标签：${t.tags.slice(MAX_CARD_TAGS).join("、")}`}
                            >
                              +{t.tags.length - MAX_CARD_TAGS}
                            </span>
                          )}
                        </div>
                      )}
                      <div className="card-tags">
                        <span className="card-tag" title="所属频道"># {channelName(t.workspace_id)}</span>
                        <span className="card-tag" title={
                          t.knowledge_mode === "local" ? "任务级知识库（工作空间内）"
                            : t.knowledge_mode === "temp" ? "临时知识空间"
                              : "主题知识库"
                        }>◎ {t.repo || "临时知识"}</span>
                      </div>
                      {t.updated_at && (
                        <div className="card-foot">
                          <time
                            dateTime={t.updated_at}
                            title={`${formatDateTime(t.updated_at)} · ${t.id}`}
                          >
                            {formatRelative(t.updated_at, now)}更新
                          </time>
                        </div>
                      )}
                    </a>
                    {status === "in_review" && (
                      <div className="card-meta" style={{ marginTop: 2 }}>
                        <a className="badge review-flag" href="/inbox" draggable={false}
                          title="进入收件箱处理"><Icon name="clock" /> 等人类评审</a>
                      </div>
                    )}
                    {status === "todo" && (t.assignees?.length ?? 0) > 0 && (
                      <div className="card-actions">
                        <button
                          className="small"
                          onClick={() => act(t.id, () => moveTask(t, "in_progress"))}
                          disabled={busy === t.id}
                          title={t.assignees.length >= 2 ? "先由 Cindy 规划工作流，批准后按依赖执行" : "直接派发该角色"}
                        >{t.assignees.length >= 2 ? "派发（Cindy 规划）" : "派发并执行"}</button>
                      </div>
                    )}
                    {status === "in_review" && (
                      <div className="card-actions">
                        <button className="small" onClick={() => act(t.id, () => api.reviewTask(t.id, true))} disabled={busy === t.id}>通过</button>
                        <button className="small danger" onClick={() => act(t.id, () => api.reviewTask(t.id, false))} disabled={busy === t.id}>驳回</button>
                      </div>
                    )}
                  </div>
                  );
                })}
                {boardTasks.filter((t) => t.status === status).length === 0 && (
                  <p className="muted kanban-empty">暂无任务</p>
                )}
              </div>
            ))}
          </div>
        </section>

        <div
          className={`splitter ${panel.dragging ? "dragging" : ""}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整右栏宽度"
          aria-valuemin={300}
          aria-valuemax={560}
          aria-valuenow={panel.width}
          tabIndex={0}
          onPointerDown={panel.onPointerDown}
          onKeyDown={panel.onKeyDown}
          onDoubleClick={panel.reset}
        />

        {/* 右栏：实时会话 + 最近活动（历史见 /activity） */}
        <section className="lobby-side" aria-hidden={panel.collapsed || undefined}>
          <div className="pane-head">
            <h2 className="pane-title">实时会话 · {visibleRuns.length}</h2>
            <button
              type="button"
              className="icon-btn"
              onClick={panel.toggle}
              aria-label="折叠右栏"
              title="折叠右栏"
            >
              <Icon name="panel-right" />
            </button>
          </div>
          {visibleRuns.length === 0 && (
            <p className="muted">当前没有活跃会话。角色空闲时进程释放，不是故障。</p>
          )}
          {visibleRuns.map((r) => {
            const task = tasks.find((t) => t.id === r.task_id);
            const roleName = roles.find((x) => x.slug === r.role_slug)?.display_name ?? r.role_slug;
            const state = globalRoleState({
              runtime: runtimeByRole.get(r.role_slug),
              latestRunStatus: r.status,
            });
            const live = state.tone === "orange";
            return (
              <a key={r.id} className="run-card" href={`/runs/${r.id}`}
                title={`${state.label} · ${state.detail} · run ${RUN_STATUS_LABEL[r.status] ?? r.status}`}>
                <RoleAvatar
                  slug={r.role_slug}
                  name={roleName}
                  avatar={roles.find((x) => x.slug === r.role_slug)?.avatar}
                  color={roles.find((x) => x.slug === r.role_slug)?.avatar_color}
                  size={36}
                  tone={state.tone}
                  label={`${roleName} · ${state.label}`}
                />
                <div className="run-body">
                  <div className="run-title">
                    {roleName} · <span className={live ? "run-live" : "muted"}>{state.label}</span>
                  </div>
                  <div className="muted run-sub">
                    {task?.title ?? "无任务会话"} · {RUN_STATUS_LABEL[r.status] ?? r.status} · {fmtElapsed(r.started_at, now)}
                  </div>
                </div>
                <span className={`status-light ${state.tone}`} />
              </a>
            );
          })}

          <h2 className="pane-title activity-head">
            <span>最近活动（30 分钟）</span>
            <a className="muted history-link" href="/activity">全部历史 <Icon name="chevron-right" size={12} /></a>
          </h2>
          <ul className="activity">
            {recentActivity.length === 0 && <li className="muted">最近 30 分钟没有新活动。</li>}
            {recentActivity.map((a, i) => (
              <li key={i}>
                <div className="act-meta">
                  <span>{formatClock(a.time)}</span>
                  <span>{a.role ?? ""}</span>
                </div>
                <div className="act-body">
                  <span className={`badge badge-in_progress`}>{a.action}</span>
                  {a.task ? <a href={`/tasks/${a.task}`}>{a.task.slice(0, 8)}</a> : ""}
                </div>
              </li>
            ))}
          </ul>
        </section>
      </div>

      {showNew && (
        <NewTaskModal
          roles={roles}
          channels={channels}
          defaultChannelId={null}
          roleStatus={globalToneByRole}
          onClose={() => setShowNew(false)}
          onCreated={(task) => {
            setShowNew(false);
            refresh();
            navigate(`/tasks/${task.id}`); // 创建后直接进入任务会话
          }}
        />
      )}
    </div>
  );
}
