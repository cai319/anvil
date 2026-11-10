import { useEffect, useState } from "react";
import {
  api, subscribeEvents,
  type ContextManifest, type Role, type RoleRuntimeStatus, type Run, type Task,
  type TimelineEvent, type Workflow,
} from "../api/client";
import { roleStatusFromWorkflow } from "../workflowStatus";
import { roleLiveDot, roleLiveState, stateClassForTone, type RoleLiveState } from "../roleLive";
import { formatClock, formatShort } from "../time";
import { RoleAvatar } from "../components/RoleAvatar";
import { Icon } from "../components/Icon";

/** 任务详情「子任务」页签：每个参与角色当前的会话状态 + 执行日志 + 历史上下文。 */

const RUN_LABEL: Record<string, string> = {
  created: "已创建", projecting: "准备中", running: "运行中", submitted: "已提交",
  reviewing: "评审中", completed: "已完成", failed: "失败", abandoned: "已放弃", suspended: "等待唤醒",
};

type Claim = Task["role_claims"][number];

/** 角色当前会话状态：待办 / 进行中 / 待确认 / 完成 / 失败 / 关闭。 */
function roleStatus(claim?: Claim, run?: Run): RoleLiveState {
  if (claim?.status === "approved") return { label: "完成", cls: "done", tone: "done" };
  if (claim?.status === "submitted") return { label: "待确认", cls: "in_review", tone: "in_review" };
  if (claim?.status === "released") return { label: "关闭", cls: "closed", tone: "closed" };
  if (run?.status === "failed") return { label: "失败", cls: "failed", tone: "failed" };
  if (run?.status === "abandoned") return { label: "已放弃", cls: "abandoned", tone: "abandoned" };
  if (claim?.status === "working") return { label: "进行中", cls: "in_progress", tone: "in_progress" };
  return { label: "待办", cls: "todo", tone: "todo" };
}

function describeEvent(ev: TimelineEvent): string {
  const p = ev.payload ?? {};
  switch (ev.kind) {
    case "comment": return String(p.body ?? "");
    case "mention": {
      const target = p.target === "human" ? "你（自然人）" : (p.target ?? "human");
      return `@${target}：${p.message ?? ""}`;
    }
    case "submit": return `提交：${p.summary ?? ""}`;
    case "review": return p.approve ? "评审通过" : "评审驳回";
    case "claim": return "认领任务";
    case "run": return `运行状态：${RUN_LABEL[String(p.status)] ?? p.status}`;
    default: return ev.kind;
  }
}

export function RoleSessions({ task, roles, workflow }: { task: Task; roles: Role[]; workflow?: Workflow | null }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<"log" | "timeline" | "manifest">("log");
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [timelines, setTimelines] = useState<Record<string, TimelineEvent[]>>({});
  const [manifests, setManifests] = useState<Record<string, ContextManifest[]>>({});
  const [runtimes, setRuntimes] = useState<Record<string, RoleRuntimeStatus>>({});
  // 参与角色 + planner（通常是 Cindy）：planner 也可能同时服务多个任务。
  const slugs = Array.from(new Set([
    ...(task.assignees?.length
      ? task.assignees
      : (task.role_claims ?? []).map((c) => c.role_slug)),
    ...(task.planner ? [task.planner] : []),
  ])).filter(Boolean);
  const slugKey = slugs.join(",");

  useEffect(() => {
    let lastLogAt = 0;
    const loadRuntimes = (roles: string[]) => {
      for (const slug of roles) {
        api.getRoleRuntime(slug)
          .then((rt) => setRuntimes((prev) => ({ ...prev, [slug]: rt })))
          .catch(() => {});
      }
    };
    const load = (ev?: { topic?: string }) => {
      if (expanded && ev?.topic === "tick" && Date.now() - lastLogAt > 3000) {
        lastLogAt = Date.now();
        api.getRunLog(expanded)
          .then((r) => setLogs((prev) => ({ ...prev, [expanded]: r.exists ? r.content : "（该会话暂无日志）" })))
          .catch(() => {});
      }
      api.listTaskRuns(task.id)
        .then((r) => {
          setRuns(r);
          setRunsLoaded(true);
          // 被 @ 但不在 assignees 里的角色也会出现在本任务 run 列表中。
          loadRuntimes(Array.from(new Set([...slugs, ...r.map((x) => x.role_slug)])));
        })
        .catch(() => setRunsLoaded(true));
      if (slugs.length) loadRuntimes(slugs);
    };
    load();
    return subscribeEvents((ev) => {
      if (ev.topic === "task" || ev.topic === "tick") load(ev);
    });
  }, [task.id, task.status, slugKey, expanded]);

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
  const roleName = (slug: string) => roles.find((r) => r.slug === slug)?.display_name ?? slug;
  const roleFor = (slug: string) => roles.find((r) => r.slug === slug);

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
    if (!(run.id in manifests)) {
      api.getRunContextManifest(run.id)
        .then((m) => setManifests((prev) => ({ ...prev, [run.id]: m })))
        .catch(() => setManifests((prev) => ({ ...prev, [run.id]: [] })));
    }
  };

  // 本任务出现过的所有角色（含被 @ 拉入、但不在 assignees 里的角色）。
  const visibleSlugs = Array.from(new Set([
    ...slugs,
    ...runs.map((r) => r.role_slug),
  ]));

  return (
    <div className="role-sessions">
      {visibleSlugs.length === 0 && <p className="muted">该任务还没有参与角色。可在新建任务时选择成员。</p>}
      {visibleSlugs.length > 0 && (
        <>
          <p className="muted">每个角色的当前会话状态；点击行展开执行日志与历史上下文。</p>
          <div className="role-live-legend" aria-label="角色状态图例">
            <span><i className="role-live-dot green" />进行中（本任务）</span>
            <span><i className="role-live-dot red" />繁忙（其他任务）/ 失败</span>
            <span><i className="role-live-dot amber" />切换上下文 / 排队 / 等待唤醒 / 等待重试</span>
            <span><i className="role-live-dot blue" />空闲 · 上下文已保留</span>
            <span><i className="role-live-dot gray" />待办 / 冷启动 / 已终止</span>
          </div>
        </>
      )}
      {visibleSlugs.map((slug) => {
        const claim = claims.get(slug);
        const stepRunId = stepRunByRole.get(slug);
        const rt = runtimes[slug];
        // 同一 task 多切片时，优先展示角色当前切片对应的 run，而不是最后一个 step run。
        const sliceRun = runs
          .filter((r) => r.role_slug === slug
            && (r.slice_key ?? null) === (rt?.context_slice_key ?? null))
          .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""))[0];
        const run = sliceRun
          || (stepRunId && runs.find((r) => r.id === stepRunId))
          || latestByRole.get(slug);
        const busyElsewhere = Boolean(
          rt?.alive && rt.status === "busy"
          && rt.current_task_id && rt.current_task_id !== task.id,
        );
        const queueInfo = rt?.queue_tasks?.find((q) => q.id === task.id);
        const queuedHere = queueInfo?.count ?? 0;
        const queueAhead = queueInfo?.ahead_count ?? 0;
        const ctxList = (rt?.contexts ?? []).filter((c) => c.task_id === task.id);
        const ctxHere = ctxList.find(
          (c) => c.context_key && rt?.context_key && c.context_key === rt.context_key,
        ) ?? ctxList.find(
          (c) => (c.slice_key ?? null) === (rt?.context_slice_key ?? null),
        ) ?? ctxList[0];
        const wfSlice = ctxList.length
          ? (ctxHere?.slice_key ?? rt?.context_slice_key ?? null)
          : undefined;
        const wfState = roleStatusFromWorkflow(workflow, slug, wfSlice);
        const taskClosed = task.status === "closed" || workflow?.status === "terminated";
        const retryWait = wfState?.label === "等待重试";
        const live = roleLiveState(rt, task.id, {
          hasOpenStep: wfState?.cls === "in_progress",
          allDone: wfState?.cls === "done",
          queuedHere,
          queueAhead,
          runStatus: run?.status ?? null,
          retryWait,
          taskClosed,
          taskDone: task.status === "done",
        });
        // 点灯表达实时运行态；徽标优先表达终止/完成/失败。
        let st = live;
        if (wfState?.cls === "closed" && task.status !== "done" && !taskClosed) {
          st = { label: wfState.label, cls: "failed", tone: "failed" };
        } else if (live.tone === "todo" && !taskClosed) {
          st = wfState?.cls === "done"
            ? { label: "完成", cls: "done", tone: "done" }
            : roleStatus(claim, run);
        }
        const dot = roleLiveDot(
          rt, task.id, Boolean(ctxHere?.has_session),
          wfState?.cls === "in_progress", wfState?.cls === "closed",
          run?.status ?? null, taskClosed, queueAhead, retryWait,
        );
        const isOpen = expanded === run?.id && !!run;
        let detail = "";
        if (rt?.switching) {
          detail = "正在切换任务上下文…";
        } else if (busyElsewhere) {
          detail = `繁忙 · 正在处理「${rt?.current_task?.title ?? rt?.current_task_id}」`;
          if (queuedHere) detail += `，本任务有 ${queuedHere} 条消息排队`;
        } else if (live.tone === "in_progress") {
          detail = rt?.current_step_id
            ? `正在处理本任务 · 步骤 ${rt.current_step_id}`
            : "正在处理本任务";
          if (rt?.current_slice_key) detail += ` · 切片 ${rt.current_slice_key}`;
          if (queuedHere > 0) detail += ` · 本任务待处理 ${queuedHere} 条`;
        } else if (live.tone === "queued") {
          detail = live.label === "等待唤醒"
            ? `等待唤醒后处理本任务 ${queuedHere} 条消息`
            : live.label === "等待重试"
              ? "等待自动恢复重试"
              : (queuedHere > 0
                ? `本任务有 ${queuedHere} 条消息待处理`
                : "等待上下文切换");
        } else if (live.tone === "failed") {
          detail = "最近一次 run 失败，等待重试或人工处理";
        } else if (live.tone === "closed") {
          detail = "任务已终止";
        } else if (!rt) {
          detail = "状态加载中…";
        } else if (ctxHere?.has_session) {
          detail = `空闲 · ${ctxHere.slice_key ? `切片 ${ctxHere.slice_key} ` : ""}上下文已保留 · ${formatShort(ctxHere.last_active_at)}`;
        } else {
          detail = ctxList.length > 1
            ? `空闲 · 本任务有 ${ctxList.length} 个切片上下文（均无会话）`
            : "空闲 · 首次进入将新建上下文";
        }
        return (
          <div key={slug} className="rs-row-wrap">
            <button className="rs-row" onClick={() => open(run)} disabled={!run}>
              <span
                className={`role-live-dot ${dot.cls}`}
                title={dot.title}
                aria-label={dot.title}
                role="img"
              />
              <span className={`status status-${stateClassForTone(st.tone)}`}>{st.label}</span>
              <RoleAvatar
                slug={slug}
                name={roleName(slug)}
                avatar={roleFor(slug)?.avatar}
                color={roleFor(slug)?.avatar_color}
                size={28}
                status={dot.cls}
              />
              <span className="rs-name">
                {roleName(slug)}
                <small className="muted"> {slug}</small>
              </span>
              {detail ? (
                <span className="rs-run muted">{detail}</span>
              ) : run ? (
                <span className="rs-run muted">
                  {RUN_LABEL[run.status] ?? run.status} · {formatClock(run.started_at)}
                </span>
              ) : (
                <span className="rs-run muted">{runsLoaded ? "尚无会话" : "加载中…"}</span>
              )}
              <span className="rs-chevron">{isOpen ? <Icon name="chevron-down" size={14} /> : <Icon name="chevron-right" size={14} />}</span>
            </button>
            {claim?.summary && <div className="rs-summary muted">提交摘要：{claim.summary}</div>}
            {ctxList.length > 1 && (
              <div className="rs-summary muted">
                切片上下文：
                {ctxList.map((c) => (
                  <span key={c.context_key || c.slice_key || "task"} className="chip"
                    style={{ marginLeft: 6 }}>
                    {c.slice_key || "task 级"}
                    {c.has_session ? " · 已保留" : " · 无会话"}
                    {c.context_key && c.context_key === rt?.context_key ? " · 当前" : ""}
                  </span>
                ))}
              </div>
            )}

            {isOpen && run && (
              <div className="rs-detail">
                <div className="rs-detail-meta">
                  <span className="mono">run {run.id.slice(0, 8)}</span>
                  <span className={`status status-${run.status}`}>{RUN_LABEL[run.status] ?? run.status}</span>
                  {run.failure_class && <span className="badge badge-in_progress">{run.failure_class}</span>}
                  <a href={`/runs/${run.id}`}>打开完整会话页 <Icon name="chevron-right" size={12} /></a>
                </div>
                <div className="rs-tabs">
                  <button className={`small ${subTab === "log" ? "primary" : ""}`} onClick={() => setSubTab("log")}>完整交互（上下文 / 模型 / 工具）</button>
                  <button className={`small ${subTab === "timeline" ? "primary" : ""}`} onClick={() => setSubTab("timeline")}>历史上下文（时间线）</button>
                  <button className={`small ${subTab === "manifest" ? "primary" : ""}`} onClick={() => setSubTab("manifest")}>上下文来源</button>
                  <button className="small" onClick={() => {
                    api.getRunLog(run.id)
                      .then((r) => setLogs((prev) => ({ ...prev, [run.id]: r.exists ? r.content : "（该会话暂无日志）" })))
                      .catch(() => {});
                  }}>刷新日志</button>
                </div>
                {subTab === "log" && (
                  <pre className="run-log">{logs[run.id] ?? "加载中…"}</pre>
                )}
                {subTab === "timeline" && (
                  <ul className="rs-timeline">
                    {(timelines[run.id] ?? []).length === 0 && <li className="muted">暂无上下文事件。</li>}
                    {(timelines[run.id] ?? []).map((ev) => (
                      <li key={ev.key}>
                        <span className="muted mono">{formatClock(ev.ts)}</span>{" "}
                        <strong>{ev.role ?? "system"}</strong>{" "}
                        <span>{describeEvent(ev)}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {subTab === "manifest" && (
                  <div className="rs-manifest">
                    {(manifests[run.id] ?? []).length === 0 && (
                      <p className="muted">暂无上下文清单（历史 run 可能未记录）。</p>
                    )}
                    {(manifests[run.id] ?? []).map((m) => (
                      <div key={m.id} className="rs-manifest-group">
                        <div className="muted">
                          <strong>{m.kind}</strong> · {m.total_chars} chars · {formatClock(m.created_at)}
                        </div>
                        <ul className="rs-timeline">
                          {m.blocks.map((b, i) => (
                            <li key={`${m.id}-${i}`}>
                              <span className="muted mono">[{b.layer ?? "?"}]</span>{" "}
                              <strong>{b.name ?? b.kind ?? "block"}</strong>{" "}
                              <span className="muted">
                                {b.chars ?? 0} chars · {b.injected ? "注入" : (b.reason ?? "省略")}
                              </span>
                              {b.source_ids?.length ? (
                                <span className="muted mono"> · {b.source_ids.slice(0, 3).join(", ")}</span>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
