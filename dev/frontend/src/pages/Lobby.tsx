import { useEffect, useState } from "react";
import { api, subscribeEvents, type Role, type Run, type Task } from "../api/client";
import { navigate } from "../router";
import { NewTaskModal } from "./NewTaskModal";

const COLUMNS: Task["status"][] = ["todo", "in_progress", "in_review", "done", "closed"];
const STATUS_LABEL: Record<Task["status"], string> = {
  todo: "待办",
  in_progress: "进行中",
  in_review: "待评审",
  done: "完成",
  closed: "已关闭",
};

const AVATAR_COLORS = ["#6d7cff", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa"];

function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

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
  cancel: "取消",
  reopen: "重开",
  plan: "规划",
};

/** 活跃会话的 run 状态（常驻模式下 suspended = 等待下一条消息）。 */
const RUN_STATUS_LABEL: Record<string, string> = {
  created: "已创建",
  projecting: "投影中",
  running: "运行中",
  submitted: "已提交",
  reviewing: "评审中",
  suspended: "等待唤醒",
};

function fmtElapsed(start: string | null): string {
  if (!start) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(start).getTime()) / 1000));
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
  throw new Error(`不支持 ${s} → ${target}`);
}

export function Lobby() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [showNew, setShowNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);

  const refresh = () =>
    Promise.all([api.listTasks(), api.listRoles(), api.listActiveRuns()])
      .then(([t, r, n]) => {
        setTasks(t);
        setRoles(r);
        setRuns(n);
      })
      .catch((e) => setError(String(e)));

  const refreshActivity = () =>
    api.listAudit({ limit: 200 })
      .then((a) => setActivity((a as Record<string, unknown>[]).map(describe)))
      .catch(() => {});

  useEffect(() => {
    refresh();
    refreshActivity();
    return subscribeEvents((ev) => {
      if (ev.topic === "task" || ev.topic === "tick") refresh();
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

  // 任务大厅只展示高层任务（workflow 步骤不是 task）
  const boardTasks = tasks.filter((t) => !t.parent_id);

  // 右栏只保留最近 30 分钟的活动，且过滤心跳噪声；完整历史在 /activity。
  const cutoff = Date.now() - 30 * 60 * 1000;
  const recentActivity = activity
    .filter((a) => a.action !== "心跳" && a.action !== "删除"
      && a.time && new Date(a.time).getTime() >= cutoff)
    .slice(0, 20);

  return (
    <div>
      <div className="toolbar">
        <button className="primary new-task-btn" onClick={() => setShowNew(true)}>
          <span className="plus">＋</span> 新建任务
        </button>
        <small className="muted">选择参与成员后创建；多角色任务会先由 Cindy 规划工作流</small>
      </div>

      {error && <p className="error">{error}</p>}

      <div className="lobby">
        {/* 中栏：任务看板 */}
        <section>
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
                {boardTasks.filter((t) => t.status === status).map((t) => (
                  <div
                    key={t.id}
                    className="card"
                    draggable
                    onDragStart={() => setDragId(t.id)}
                    onDragEnd={() => setDragId(null)}
                  >
                    <a className="card-link" href={`/tasks/${t.id}`} draggable={false}>
                      <strong>{t.title}</strong>
                      <div className="card-meta">
                        {t.assignees?.length ? (
                          <span>👥 {t.assignees.join("、")}</span>
                        ) : t.assignee ? (
                          <span>👤 {t.assignee}</span>
                        ) : (
                          <span className="muted">未指派</span>
                        )}
                        {t.tags?.length > 0 && <span className="muted">#{t.tags.join(" #")}</span>}
                      </div>
                    </a>
                    {status === "in_review" && (
                      <div className="card-meta" style={{ marginTop: 2 }}>
                        <a className="badge review-flag" href="/inbox" draggable={false}
                          title="进入收件箱处理">⏳ 等人类评审 → 收件箱</a>
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
                ))}
              </div>
            ))}
          </div>
        </section>

        {/* 右栏：实时会话 + 最近活动（历史见 /activity） */}
        <section>
          <h2 className="pane-title">实时会话 · {runs.length}</h2>
          {runs.length === 0 && (
            <p className="muted">当前没有活跃会话。角色空闲时进程释放，不是故障。</p>
          )}
          {runs.map((r) => {
            const task = tasks.find((t) => t.id === r.task_id);
            const roleName = roles.find((x) => x.slug === r.role_slug)?.display_name ?? r.role_slug;
            const live = r.status === "running" || r.status === "projecting";
            return (
              <a key={r.id} className="run-card" href={`/runs/${r.id}`} title="查看该会话详情">
                <span className="role-avatar" style={{ background: avatarColor(r.role_slug), width: 28, height: 28, fontSize: 12 }}>
                  {roleName.slice(0, 1)}
                </span>
                <div className="run-body">
                  <div className="run-title">
                    {roleName} · <span className={live ? "run-live" : "muted"}>{RUN_STATUS_LABEL[r.status] ?? r.status}</span>
                  </div>
                  <div className="muted run-sub">{task?.title ?? "无任务会话"} · {fmtElapsed(r.started_at)}</div>
                </div>
                <span className={`status-light ${live ? "running" : "idle"}`} />
              </a>
            );
          })}

          <h2 className="pane-title activity-head">
            <span>最近活动（30 分钟）</span>
            <a className="muted history-link" href="/activity">全部历史 →</a>
          </h2>
          <ul className="activity">
            {recentActivity.length === 0 && <li className="muted">最近 30 分钟没有新活动。</li>}
            {recentActivity.map((a, i) => (
              <li key={i}>
                <div className="act-meta">
                  <span>{a.time.slice(11, 19)}</span>
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
