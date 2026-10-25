import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type Role,
  type Task,
  type TimelineEvent,
  type Workflow,
  type WorkflowHistoryItem,
  type WorkflowMetrics,
} from "../api/client";
import { PageHeader } from "../components/ui";
import { RoleAvatar } from "../components/RoleAvatar";
import { Icon } from "../components/Icon";
import { formatClock, formatDateTime, formatDuration, secondsBetween } from "../time";
import { STATUS_META } from "../workflowGroups";

/** A read-only replay of a task's complete activity stream and workflow state. */

const KIND_LABEL: Record<string, string> = {
  comment: "发言",
  mention: "@提及",
  step: "步骤",
  run: "运行",
  review: "人工评审",
  submit: "提交",
  claim: "认领",
  heartbeat: "心跳",
  wake: "唤醒",
  "board.approve": "批准流程",
  "task.tags_cindy": "Cindy 标签",
  "task.knowledge_bind": "知识仓库",
  "task.knowledge_bind_cindy": "Cindy 知识仓库",
};

const RUN_LABEL: Record<string, string> = {
  created: "创建",
  projecting: "准备",
  running: "运行",
  suspended: "挂起",
  submitted: "提交",
  reviewing: "评审",
  completed: "完成",
  failed: "失败",
  abandoned: "中止",
};

const STEP_LABEL: Record<string, string> = {
  running: "开始执行",
  submitted: "提交待审",
  approved: "通过",
  planned: "切片完成",
  retrying: "自动重试",
  rework: "打回返工",
  rework_exhausted: "返工耗尽",
  escalated: "升级人工",
  rejected: "人工驳回",
  failed: "失败",
};

const SPEEDS = [1, 2, 4, 8, 16];

function stepStatus(status: string): WorkflowStepStatus {
  if (status === "rework" || status === "retrying" || status === "rejected") return "pending";
  if (status === "rework_exhausted") return "failed";
  if (status === "escalated") return "submitted";
  if (status === "planned") return "approved";
  if (status in STATUS_META) return status as WorkflowStepStatus;
  return "pending";
}

type WorkflowStepStatus = "pending" | "running" | "submitted" | "approved" | "rejected" | "failed" | "skipped";

function nameOf(ev: TimelineEvent, roleNames: Map<string, string>): string {
  const slug = ev.role || "system";
  if (slug === "human") return "自然人";
  if (slug === "workflow") return "工作流引擎";
  if (slug === "system") return "系统";
  return roleNames.get(slug) || slug;
}

function eventBody(ev: TimelineEvent): string {
  const p = ev.payload;
  switch (ev.kind) {
    case "comment":
      return String(p.body ?? "");
    case "mention": {
      const target = String(p.target ?? "human");
      const raw = String(p.message ?? "");
      return `@${target} ${raw}`;
    }
    case "step":
      return `${String(p.step ?? "")}「${String(p.title ?? "")}」· ${
        STEP_LABEL[String(p.status ?? "")] || String(p.status ?? "")}`;
    case "run":
      return `运行状态：${RUN_LABEL[String(p.status ?? "")] || String(p.status ?? "")}`;
    case "review":
      return p.approve ? "评审通过，任务完成" : "评审驳回";
    case "board.approve":
      return "人工批准了工作流草案，执行引擎开始推进。";
    case "task.tags_cindy":
      return `Cindy 生成标签：${(p.tags as string[] | undefined)?.join("、") || "—"}`;
    case "task.knowledge_bind":
    case "task.knowledge_bind_cindy": {
      const top = (p.top as { repo?: string }[] | undefined)?.map((x) => x.repo).filter(Boolean);
      const repos = (p.repos as { repo?: string }[] | undefined)?.map((x) => x.repo).filter(Boolean);
      return `知识仓库：${(repos || top || []).join("、") || "无匹配"}`;
    }
    case "heartbeat":
      return "续租心跳";
    case "claim":
      return "认领了任务";
    case "submit":
      return `提交了评审${p.summary ? `：${String(p.summary)}` : ""}`;
    case "wake":
      return "被唤醒处理任务";
    default:
      if (ev.kind.startsWith("audit.")) {
        return ev.kind.slice(6).replaceAll("_", " ");
      }
      return ev.kind;
  }
}

export function TaskReplay({ id }: { id: string }) {
  const [task, setTask] = useState<Task | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [history, setHistory] = useState<WorkflowHistoryItem[]>([]);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [metrics, setMetrics] = useState<WorkflowMetrics | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const streamRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getTask(id),
      api.getTaskTimeline(id),
      api.listRoles(),
      api.listTaskWorkflows(id),
    ]).then(async ([t, tl, rs, hs]) => {
      if (!alive) return;
      const events = [...tl].sort((a, b) => a.ts.localeCompare(b.ts));
      setTask(t);
      setTimeline(events);
      setRoles(rs);
      setHistory(hs);
      const latest = hs[0];
      if (latest) {
        const wf = await api.getWorkflow(latest.id);
        if (!alive) return;
        setWorkflow(wf);
        api.getWorkflowMetrics(latest.id).then(setMetrics).catch(() => setMetrics(null));
      }
    }).catch((e) => alive && setError(String(e)));
    return () => { alive = false; };
  }, [id]);

  const roleNames = useMemo(
    () => new Map(roles.map((r) => [r.slug, r.display_name || r.slug])),
    [roles]);
  const roleBySlug = useMemo(
    () => new Map(roles.map((r) => [r.slug, r])),
    [roles]);

  const events = timeline;
  const current = cursor > 0 ? events[cursor - 1] : null;

  useEffect(() => {
    if (!playing) return;
    if (cursor >= events.length) {
      setPlaying(false);
      return;
    }
    const delay = Math.max(45, 1000 / speed);
    const timer = window.setTimeout(
      () => setCursor((c) => Math.min(c + 1, events.length)), delay);
    return () => window.clearTimeout(timer);
  }, [playing, cursor, speed, events.length]);

  useEffect(() => {
    if ((!playing && cursor < events.length) || !streamRef.current) return;
    const el = streamRef.current;
    requestAnimationFrame(() => {
      el.scrollTo({ top: el.scrollHeight, behavior: playing ? "smooth" : "auto" });
    });
  }, [cursor, playing, events.length]);

  const stepStates = useMemo(() => {
    const map = new Map<string, WorkflowStepStatus>();
    for (const s of workflow?.steps || []) map.set(s.step_id, "pending");
    for (const ev of events.slice(0, cursor)) {
      if (ev.kind !== "step") continue;
      const sid = String(ev.payload.step ?? "");
      if (!sid || !map.has(sid)) continue;
      map.set(sid, stepStatus(String(ev.payload.status ?? "")));
    }
    return map;
  }, [workflow, events, cursor]);

  const visible = events.slice(0, cursor);
  const doneCount = [...stepStates.values()].filter(
    (s) => s === "approved" || s === "skipped").length;
  const progress = workflow?.steps.length
    ? Math.round((doneCount / workflow.steps.length) * 100) : 0;
  const playbackTime = current?.ts || task?.created_at || null;
  const tokenExec = metrics?.tokens.total_tokens
    ?? ((metrics?.tokens.input_tokens ?? 0) + (metrics?.tokens.output_tokens ?? 0));
  const rawTaskTokens = (metrics?.tokens.task_input_tokens ?? 0)
    + (metrics?.tokens.task_output_tokens ?? 0);
  const tokenTask = metrics?.tokens.task_total_tokens ?? (rawTaskTokens || tokenExec);

  const togglePlay = () => {
    if (cursor >= events.length) setCursor(0);
    setPlaying((p) => !p);
  };

  const exportLog = async () => {
    setExporting(true);
    setError(null);
    try {
      const blob = await api.downloadTaskReplayLog(id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `replay-${id.slice(0, 8)}.log`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    } finally {
      setExporting(false);
    }
  };

  if (error) return <p className="error">{error}</p>;
  if (!task) return <p className="muted">加载任务回放…</p>;

  return (
    <div className="session replay-page">
      <p className="session-back">
        <a href={`/tasks/${id}`}><Icon name="chevron-left" /> 返回任务</a>
      </p>
      <PageHeader
        kicker="TASK REPLAY"
        title={task.title}
        subtitle={
          <>
            {task.id} · {task.assignees?.join("、") || "未指派"}
            {" · "}{formatDateTime(task.created_at)}
            {" · "}共 {events.length} 个事件
          </>
        }
        actions={
          <>
            <button className="small" disabled={exporting} onClick={exportLog}>
              <Icon name="file-text" size={12} />
              {exporting ? "导出中…" : "导出日志"}
            </button>
            <span className={`status status-${task.status === "done" ? "done" : task.status === "in_progress" ? "in_progress" : "closed"}`}>
              {task.status === "done" ? "已完成" : task.status === "in_progress" ? "进行中" : task.status}
            </span>
          </>
        }
      />

      <div className="replay-controls card">
        <button className="primary" onClick={togglePlay} disabled={events.length === 0}>
          <Icon name={playing ? "pause" : "play"} size={14} />
          {playing ? "暂停" : cursor >= events.length ? "重新播放" : "播放"}
        </button>
        <button className="small" onClick={() => { setPlaying(false); setCursor(0); }}>
          回到开头
        </button>
        <div className="replay-speed">
          {SPEEDS.map((s) => (
            <button key={s} className={`small ${speed === s ? "primary" : ""}`}
              onClick={() => setSpeed(s)}>{s}×</button>
          ))}
        </div>
        <input className="replay-range" type="range" min={0} max={events.length}
          value={cursor} onChange={(e) => {
            setPlaying(false);
            setCursor(Number(e.target.value));
          }} aria-label="回放进度" />
        <span className="muted replay-count">
          {cursor}/{events.length}
          {playbackTime ? ` · ${formatDateTime(playbackTime)}` : ""}
        </span>
        <button className="small" onClick={() => {
          setPlaying(false);
          setCursor(events.length);
        }}>跳到结束</button>
      </div>

      <div className="replay-layout">
        <section className="replay-stream card" ref={streamRef}>
          {visible.length === 0 && (
            <div className="replay-intro">
              <strong>准备回放</strong>
              <p className="muted">
                点击「播放」按时间顺序重现整个任务：角色发言、步骤切换、运行状态与人工决策。
              </p>
            </div>
          )}
          {visible.map((ev, i) => {
            const role = ev.role || "system";
            const roleMeta = roleBySlug.get(role);
            const isCurrent = i === visible.length - 1;
            const compact = ev.kind === "run" || ev.kind === "heartbeat";
            const body = eventBody(ev);
            const long = body.length > 280;
            return (
              <article key={ev.key || `${ev.ts}-${i}`}
                className={`replay-event ${compact ? "is-compact" : ""} ${isCurrent ? "is-current" : ""}`}>
                <div className="replay-event-head">
                  <RoleAvatar slug={role} name={roleMeta?.display_name || nameOf(ev, roleNames)}
                    avatar={roleMeta?.avatar} color={roleMeta?.avatar_color} size={22} />
                  <strong>{nameOf(ev, roleNames)}</strong>
                  <span className="replay-kind">{KIND_LABEL[ev.kind] || ev.kind}</span>
                  <span className="muted mono">{formatClock(ev.ts)}</span>
                </div>
                {long ? (
                  <details className="replay-event-body">
                    <summary>{body.slice(0, 200)}…</summary>
                    <div>{body}</div>
                  </details>
                ) : (
                  <div className="replay-event-body">{body}</div>
                )}
              </article>
            );
          })}
          {cursor >= events.length && events.length > 0 && (
            <div className="replay-end">
              <Icon name="check" size={14} /> 回放结束 · 共 {events.length} 个事件
            </div>
          )}
        </section>

        <aside className="replay-side">
          <div className="replay-side-block">
            <span className="page-kicker">TASK</span>
            <h3>{task.title}</h3>
            <div className="replay-tags">
              {(task.tags || []).map((tag) => <span key={tag} className="badge">{tag}</span>)}
            </div>
            {task.description && (
              <p className="muted replay-desc">{task.description}</p>
            )}
          </div>

          {workflow && (
            <div className="replay-side-block">
              <div className="replay-side-head">
                <span className="page-kicker">WORKFLOW</span>
                <span className="muted">
                  第 {workflow.execution_no || history[0]?.execution_no || 1} 次 · {workflow.weight || "standard"}
                </span>
              </div>
              <div className="replay-progress">
                <span><i style={{ width: `${progress}%` }} /></span>
                <strong>{doneCount}/{workflow.steps.length}</strong>
              </div>
              <div className="replay-steps">
                {workflow.steps.map((s) => {
                  const status = stepStates.get(s.step_id) || "pending";
                  const meta = STATUS_META[status];
                  return (
                    <div key={s.step_id}
                      className={`replay-step ${current?.step_id === s.step_id || (current?.kind === "step" && current.payload.step === s.step_id) ? "is-current" : ""}`}>
                      <i style={{ background: meta.color }} />
                      <span className="mono">{s.step_id}</span>
                      <span className="replay-step-title" title={s.title}>{s.title}</span>
                      <span className="muted">{s.role_slug}</span>
                      <span style={{ color: meta.color }}>{STATUS_META[status].label}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {metrics && (
            <div className="replay-side-block replay-metrics">
              <span className="page-kicker">FINAL METRICS</span>
              <div><span>步骤</span><strong>{metrics.steps.done}/{metrics.steps.total}</strong></div>
              <div title="任务创建到最后更新的墙钟时间">
                <span>任务总耗时</span>
                <strong>{formatDuration(secondsBetween(task.created_at, task.updated_at))}</strong>
              </div>
              <div title="workflow 创建到完成/终止">
                <span>工作流耗时</span><strong>{formatDuration(metrics.duration_seconds)}</strong>
              </div>
              <div title="工作流耗时扣除纯等待人工介入的时间">
                <span>工作流有效执行</span>
                <strong>{formatDuration(metrics.active_duration_seconds
                  ?? Math.max(0, metrics.duration_seconds
                    - (metrics.waiting.excluded_seconds ?? 0)))}</strong>
              </div>
              <div title="没有角色在推进、纯等待人工审核的时长（区间去重）">
                <span>人工等待</span>
                <strong>{formatDuration(metrics.waiting.excluded_seconds ?? 0)}</strong>
              </div>
              <div title="workflow gate 步骤数；任务最终评审不计入审核门">
                <span>审核门</span><strong>{metrics.gates.total}</strong>
              </div>
              <div title={metrics.decisions.legacy
                ? "历史执行没有不可变决策记录，决策数按决策步骤状态与返工次数推算"
                : "不可变决策记录数"}>
                <span>决策</span><strong>{metrics.decisions.total}</strong>
              </div>
              <div title="workflow 人工决策 + 任务最终人工评审">
                <span>人工干预</span>
                <strong>{metrics.decisions.human_interventions ?? metrics.decisions.human}</strong>
              </div>
              <div><span>返工</span><strong>{metrics.decisions.rejected}</strong></div>
              <div title={`任务所有 run：输入 ${metrics.tokens.task_input_tokens ?? 0} + 输出 ${metrics.tokens.task_output_tokens ?? 0} + 缓存读取 ${metrics.tokens.task_cache_read_tokens ?? 0} + 缓存写入 ${metrics.tokens.task_cache_creation_tokens ?? 0}`}>
                <span>任务 Token</span><strong>{tokenTask.toLocaleString("en-US")}</strong>
              </div>
              <div title={`仅 workflow 步骤关联的 run：输入 ${metrics.tokens.input_tokens ?? 0} + 输出 ${metrics.tokens.output_tokens ?? 0} + 缓存读取 ${metrics.tokens.cache_read_tokens ?? 0} + 缓存写入 ${metrics.tokens.cache_creation_tokens ?? 0}`}>
                <span>执行 Token</span><strong>{tokenExec.toLocaleString("en-US")}</strong>
              </div>
              {metrics.gates.total === 0 && (
                <p className="replay-metric-note">
                  本执行没有 workflow 审核门；最终人工验收单独计入人工干预。
                </p>
              )}
              {(metrics.decisions.legacy ?? 0) > 0 && (
                <p className="replay-metric-note">
                  历史执行无不可变决策记录，决策数由决策步骤状态推算。
                </p>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
