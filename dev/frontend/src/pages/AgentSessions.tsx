import { useEffect, useState } from "react";
import { api, subscribeEvents, type Role, type Run, type Task } from "../api/client";

const RUN_LABEL: Record<string, string> = {
  created: "已创建",
  projecting: "投影中",
  running: "运行中",
  submitted: "已提交",
  reviewing: "评审中",
  completed: "已完成",
  failed: "失败",
  abandoned: "已放弃",
  suspended: "已挂起",
};

const AVATAR_COLORS = ["#6d7cff", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa"];
function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function fmtDuration(start: string | null, end: string | null): string {
  if (!start) return "—";
  const a = new Date(start).getTime();
  const b = end ? new Date(end).getTime() : Date.now();
  const s = Math.max(0, Math.round((b - a) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function AgentSessions({ slug }: { slug: string }) {
  const [role, setRole] = useState<Role | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tasks, setTasks] = useState<Map<string, Task>>(new Map());
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    Promise.all([api.getRole(slug), api.listRoleRuns(slug), api.listTasks()])
      .then(([r, rns, ts]) => {
        setRole(r);
        setRuns(rns);
        setTasks(new Map(ts.map((t) => [t.id, t])));
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
    return subscribeEvents((ev) => {
      if (ev.topic === "tick" || ev.topic === "task") refresh();
    });
  }, [slug]);

  if (error) return <p className="error">{error}</p>;
  if (!role) return <p className="muted">加载中…</p>;

  const active = runs.some((r) => ["created", "projecting", "running", "submitted", "reviewing"].includes(r.status));

  return (
    <div className="session">
      <p className="session-back"><a href="/">← 任务大厅</a></p>

      <div className="session-head">
        <div className="session-title">
          <span className="role-avatar" style={{ background: avatarColor(slug), width: 34, height: 34 }}>{role.display_name.slice(0, 1)}</span>
          <h1>{role.display_name}</h1>
          <span className={`status-light ${active ? "running" : "idle"}`} title={active ? "运行中" : "空闲"} />
        </div>
        <div className="session-meta">
          <span className="mono">{slug}</span>
          <span className="muted">home {role.home_path}</span>
          <span>执行会话 {runs.length} 次</span>
        </div>
      </div>

      <section>
        <h2 className="pane-title">执行历史（session）</h2>
        {runs.length === 0 && <p className="muted empty">该角色还没有任何执行会话 —— 有任务被派发/认领后这里会显示它的每次运行。</p>}
        {runs.map((r) => {
          const t = r.task_id ? tasks.get(r.task_id) : null;
          return (
            <a className="run-row" href={`/runs/${r.id}`} key={r.id}>
              <span className={`status status-${r.status}`}>{RUN_LABEL[r.status] ?? r.status}</span>
              <span className="run-row-task">{t ? t.title : (r.task_id ?? "无任务")}</span>
              <span className="muted mono">{r.id.slice(0, 8)}</span>
              <span className="muted mono">{r.started_at?.slice(11, 19)}</span>
              <span className="muted">{fmtDuration(r.started_at, r.ended_at)}</span>
            </a>
          );
        })}
      </section>
    </div>
  );
}
