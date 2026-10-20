import { useEffect, useState } from "react";
import { api, subscribeEvents, type Role, type Run, type Task, type TimelineEvent, type Workflow } from "../api/client";
import { roleStatusFromWorkflow } from "../workflowStatus";

/** 任务详情「子任务」页签：每个参与角色当前的会话状态 + 执行日志 + 历史上下文。 */

const RUN_LABEL: Record<string, string> = {
  created: "已创建", projecting: "投影中", running: "运行中", submitted: "已提交",
  reviewing: "评审中", completed: "已完成", failed: "失败", abandoned: "已放弃", suspended: "等待唤醒",
};

const AVATAR_COLORS = ["#6d7cff", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa"];
function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

type Claim = Task["role_claims"][number];

/** 角色当前会话状态：待办 / 进行中 / 待确认 / 完成 / 关闭。 */
function roleStatus(claim?: Claim, run?: Run): { label: string; cls: string } {
  if (claim?.status === "approved") return { label: "完成", cls: "done" };
  if (claim?.status === "submitted") return { label: "待确认", cls: "in_review" };
  if (claim?.status === "released") return { label: "关闭", cls: "closed" };
  if (run?.status === "failed" || run?.status === "abandoned") return { label: "进行中（异常）", cls: "in_progress" };
  if (claim?.status === "working") return { label: "进行中", cls: "in_progress" };
  return { label: "待办", cls: "todo" };
}

function describeEvent(ev: TimelineEvent): string {
  const p = ev.payload ?? {};
  switch (ev.kind) {
    case "comment": return String(p.body ?? "");
    case "mention": return `@${p.target ?? "human"}：${p.message ?? ""}`;
    case "submit": return `提交：${p.summary ?? ""}`;
    case "review": return p.approve ? "评审通过" : "评审驳回";
    case "claim": return "认领任务";
    case "run": return `运行状态 → ${RUN_LABEL[String(p.status)] ?? p.status}`;
    default: return ev.kind;
  }
}

export function RoleSessions({ task, roles, workflow }: { task: Task; roles: Role[]; workflow?: Workflow | null }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<"log" | "timeline">("log");
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [timelines, setTimelines] = useState<Record<string, TimelineEvent[]>>({});

  useEffect(() => {
    let lastLogAt = 0;
    const load = (ev?: { topic?: string }) => {
      if (expanded && ev?.topic === "tick" && Date.now() - lastLogAt > 3000) {
        lastLogAt = Date.now();
        api.getRunLog(expanded)
          .then((r) => setLogs((prev) => ({ ...prev, [expanded]: r.exists ? r.content : "（该会话暂无日志）" })))
          .catch(() => {});
      }
      api.listTaskRuns(task.id)
        .then((r) => { setRuns(r); setRunsLoaded(true); })
        .catch(() => setRunsLoaded(true));
    };
    load();
    return subscribeEvents((ev) => {
      if (ev.topic === "task" || ev.topic === "tick") load(ev);
    });
  }, [task.id, task.status, expanded]);

  const latestByRole = new Map<string, Run>();
  for (const r of runs) {
    const cur = latestByRole.get(r.role_slug);
    if (!cur || (r.started_at ?? "") > (cur.started_at ?? "")) latestByRole.set(r.role_slug, r);
  }
  const claims = new Map((task.role_claims ?? []).map((c) => [c.role_slug, c]));
  // 工作流任务优先展示该角色最后一个「步骤 run」，避免被后来的回复 run 干扰
  const stepRunByRole = new Map<string, string>();
  for (const st of workflow?.steps ?? []) {
    if (st.run_id) stepRunByRole.set(st.role_slug, st.run_id);
  }
  const slugs = (task.assignees?.length ? task.assignees : [...claims.keys()]);
  const roleName = (slug: string) => roles.find((r) => r.slug === slug)?.display_name ?? slug;

  const open = (run: Run | undefined) => {
    if (!run) return;
    setExpanded((cur) => (cur === run.id ? null : run.id));
    setSubTab("log");
    if (!(run.id in logs)) {
      api.getRunLog(run.id)
        .then((r) => setLogs((prev) => ({ ...prev, [run.id]: r.exists ? r.content : "（该会话暂无日志）" })))
        .catch(() => setLogs((prev) => ({ ...prev, [run.id]: "（日志读取失败）" })));
    }
    if (!(run.id in timelines)) {
      api.getRunTimeline(run.id)
        .then((t) => setTimelines((prev) => ({ ...prev, [run.id]: t })))
        .catch(() => setTimelines((prev) => ({ ...prev, [run.id]: [] })));
    }
  };

  return (
    <div className="role-sessions">
      {slugs.length === 0 && <p className="muted">该任务还没有参与角色。可在新建任务时选择成员。</p>}
      {slugs.length > 0 && (
        <p className="muted">每个角色的当前会话状态；点击行展开执行日志与历史上下文。</p>
      )}
      {slugs.map((slug) => {
        const claim = claims.get(slug);
        const stepRunId = stepRunByRole.get(slug);
        const run = (stepRunId && runs.find((r) => r.id === stepRunId)) || latestByRole.get(slug);
        const st = roleStatusFromWorkflow(workflow, slug) ?? roleStatus(claim, run);
        const isOpen = expanded === run?.id && !!run;
        return (
          <div key={slug} className="rs-row-wrap">
            <button className="rs-row" onClick={() => open(run)} disabled={!run}>
              <span className="role-avatar" style={{ background: avatarColor(slug) }}>
                {roleName(slug).slice(0, 1)}
              </span>
              <span className="rs-name">
                {roleName(slug)}
                <small className="muted"> {slug}</small>
              </span>
              <span className={`status status-${st.cls}`}>{st.label}</span>
              {run ? (
                <span className="rs-run muted">
                  {RUN_LABEL[run.status] ?? run.status} · {run.started_at?.slice(11, 19)}
                </span>
              ) : (
                <span className="rs-run muted">{runsLoaded ? "尚无会话" : "加载中…"}</span>
              )}
              <span className="rs-chevron">{isOpen ? "▾" : "▸"}</span>
            </button>
            {claim?.summary && <div className="rs-summary muted">提交摘要：{claim.summary}</div>}

            {isOpen && run && (
              <div className="rs-detail">
                <div className="rs-detail-meta">
                  <span className="mono">run {run.id.slice(0, 8)}</span>
                  <span className={`status status-${run.status}`}>{RUN_LABEL[run.status] ?? run.status}</span>
                  {run.failure_class && <span className="badge badge-in_progress">{run.failure_class}</span>}
                  <a href={`/runs/${run.id}`}>打开完整会话页 →</a>
                </div>
                <div className="rs-tabs">
                  <button className={`small ${subTab === "log" ? "primary" : ""}`} onClick={() => setSubTab("log")}>完整交互（上下文 / 模型 / 工具）</button>
                  <button className={`small ${subTab === "timeline" ? "primary" : ""}`} onClick={() => setSubTab("timeline")}>历史上下文（时间线）</button>
                  <button className="small" onClick={() => {
                    api.getRunLog(run.id)
                      .then((r) => setLogs((prev) => ({ ...prev, [run.id]: r.exists ? r.content : "（该会话暂无日志）" })))
                      .catch(() => {});
                  }}>刷新日志</button>
                </div>
                {subTab === "log" ? (
                  <pre className="run-log">{logs[run.id] ?? "加载中…"}</pre>
                ) : (
                  <ul className="rs-timeline">
                    {(timelines[run.id] ?? []).length === 0 && <li className="muted">暂无上下文事件。</li>}
                    {(timelines[run.id] ?? []).map((ev) => (
                      <li key={ev.key}>
                        <span className="muted mono">{ev.ts?.slice(11, 19)}</span>{" "}
                        <strong>{ev.role ?? "system"}</strong>{" "}
                        <span>{describeEvent(ev)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
