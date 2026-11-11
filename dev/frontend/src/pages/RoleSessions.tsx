import { useEffect, useRef, useState } from "react";
import {
  api, subscribeEvents,
  type ContextManifest, type Role, type RoleRuntimeStatus, type Run, type Task,
  type TaskDispatch, type TimelineEvent, type Workflow,
} from "../api/client";
import { taskRoleState } from "../roleStatus";
import { formatClock, formatShort } from "../time";
import { HEADER_ONLY_RUN_LOG_HINT, isHeaderOnlyRunLog } from "../runLog";
import { RoleAvatar } from "../components/RoleAvatar";
import { Icon } from "../components/Icon";

/** 任务详情「子任务」页签：每个参与角色当前的会话状态 + 执行日志 + 历史上下文。 */

const RUN_LABEL: Record<string, string> = {
  created: "已创建", projecting: "准备中", running: "运行中", submitted: "已提交",
  reviewing: "评审中", completed: "已完成", failed: "失败", abandoned: "已放弃", suspended: "等待唤醒",
};

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

export function RoleSessions({ task, roles, workflow, dispatches = [], relatedTaskIds = [], focus = null }: {
  task: Task;
  roles: Role[];
  workflow?: Workflow | null;
  dispatches?: TaskDispatch[];
  relatedTaskIds?: string[];
  focus?: { role: string; sliceKey: string | null } | null;
}) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  // 没有本任务 run 时仍允许展开运行时上下文（角色可能正在别的 task/slice 工作）。
  const [expandedCtx, setExpandedCtx] = useState<string | null>(null);
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
    ...((workflow?.steps ?? []).map((s) => s.role_slug)),
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

  const loadRunContext = (run: Run) => {
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

  const open = (run: Run | undefined, slug: string) => {
    if (!run) {
      setExpanded(null);
      setExpandedCtx((cur) => (cur === slug ? null : slug));
      return;
    }
    setExpandedCtx(null);
    setExpanded((cur) => (cur === run.id ? null : run.id));
    loadRunContext(run);
  };

  const focusAppliedRef = useRef<{ role: string; sliceKey: string | null } | null>(null);
  useEffect(() => {
    if (!focus || focusAppliedRef.current === focus || !runsLoaded) return;
    const candidate = runs
      .filter((r) => r.role_slug === focus.role
        && (r.slice_key ?? null) === (focus.sliceKey ?? null))
      .sort((a, b) => String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")))[0];
    if (candidate) {
      setExpanded(candidate.id);
      setExpandedCtx(null);
      loadRunContext(candidate);
    } else {
      setExpanded(null);
      setExpandedCtx(focus.role);
    }
    focusAppliedRef.current = focus;
  }, [focus, runsLoaded, runs]);

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
            <span><i className="role-live-dot green" />空闲 / 完成</span>
            <span><i className="role-live-dot orange" />进行中 / 切换上下文</span>
            <span><i className="role-live-dot yellow" />等待人工</span>
            <span><i className="role-live-dot red" />排队 / 失败 / 返工</span>
            <span><i className="role-live-dot gray" />未参与 / 待办 / 已结束</span>
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
        // 优先展示角色此刻真正执行的 run；排队/尚未投递的新 run 只有启动头。
        const currentRun = rt?.current_run_id && rt.current_task_id === task.id
          ? runs.find((r) => r.id === rt.current_run_id)
          : undefined;
        const run = currentRun
          || sliceRun
          || (stepRunId && runs.find((r) => r.id === stepRunId))
          || latestByRole.get(slug);
        const ctxList = (rt?.contexts ?? []).filter((c) => c.task_id === task.id);
        const focusCtx = focus?.role === slug
          ? ctxList.find((c) => (c.slice_key ?? null) === (focus.sliceKey ?? null))
          : undefined;
        const ctxHere = focusCtx ?? ctxList.find(
          (c) => c.context_key && rt?.context_key && c.context_key === rt.context_key,
        ) ?? ctxList.find(
          (c) => (c.slice_key ?? null) === (rt?.context_slice_key ?? null),
        ) ?? ctxList[0];
        const wfSlice = ctxList.length
          ? (ctxHere?.slice_key ?? rt?.context_slice_key ?? null)
          : undefined;
        const queueCtx = rt?.queue_contexts?.find(
          (q) => q.task_id === task.id && (q.slice_key ?? null) === (wfSlice ?? null));
        const queueInfo = rt?.queue_tasks?.find((q) => q.id === task.id);
        const queuedHere = queueCtx?.count ?? queueInfo?.count ?? 0;
        const returnPending = dispatches.some(
          (d) => d.target_role === slug
            && (d.status === "return_ready" || d.status === "returning"));
        let state = taskRoleState({
          task,
          role: slug,
          runtime: rt,
          workflow,
          relatedTaskIds,
          sliceKey: wfSlice,
          runStatus: run?.status ?? null,
          returnPending,
        });
        if (!rt) {
          state = {
            ...state, key: "loading", label: "加载中", tone: "gray",
            detail: "状态加载中…", title: "加载中 · 状态加载中…",
          };
        }
        const subagents = rt?.active_subagents ?? [];
        const runOpen = expanded === run?.id && !!run;
        const ctxOpen = expandedCtx === slug;
        const isOpen = runOpen || ctxOpen;
        let detail = state.detail;
        if (state.key === "working" && ctxHere?.slice_key) {
          detail += ` · 切片 ${ctxHere.slice_key}`;
        } else if (state.key === "idle" && ctxHere?.last_active_at) {
          detail += ` · ${formatShort(ctxHere.last_active_at)}`;
        } else if (state.key === "queued" && queuedHere > 0) {
          detail += ` · 当前切片 ${wfSlice ?? "task 级"} ${queuedHere} 条`;
        }
        return (
          <div key={slug} className={`rs-row-wrap ${focus?.role === slug ? "is-focused" : ""}`}>
            <button className="rs-row" onClick={() => open(run, slug)}>
              <span
                className={`role-live-dot ${state.tone}`}
                title={state.title}
                aria-label={state.title}
                role="img"
              />
              <span className={`status tone-${state.tone}`}>{state.label}</span>
              {subagents.length > 0 && (
                <span className="chip" title="运行时原生子代理，不是 Anvil 任务">
                  子代理 {subagents.length}
                </span>
              )}
              <RoleAvatar
                slug={slug}
                name={roleName(slug)}
                avatar={roleFor(slug)?.avatar}
                color={roleFor(slug)?.avatar_color}
                size={28}
                tone={state.tone}
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

            {ctxOpen && (
              <div className="rs-detail">
                <div className="rs-detail-meta">
                  <strong>运行时上下文</strong>
                  <span className="muted">本任务暂无可用 run；角色可能正在其他 task/slice 工作</span>
                  {rt?.current_run_id && (
                    <a href={`/runs/${rt.current_run_id}`}>打开当前 run <Icon name="chevron-right" size={12} /></a>
                  )}
                </div>
                {ctxList.length === 0 ? (
                  <p className="muted">本任务还没有上下文槽；首次进入时会新建。</p>
                ) : (
                  <ul className="rs-timeline">
                    {ctxList.map((c) => (
                      <li key={c.context_key || c.slice_key || "task"}>
                        <span className="mono">{c.slice_key || "task 级"}</span>
                        <span className="muted">
                          {" · "}{c.has_session ? "已保留会话" : "无 session"}
                          {c.cwd ? ` · ${c.cwd}` : ""}
                        </span>
                        {c.context_key && c.context_key === rt?.context_key && (
                          <span className="chip" style={{ marginLeft: 6 }}>当前</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="muted">
                  队列：{queuedHere} 条；最近活跃：{ctxHere?.last_active_at ? formatShort(ctxHere.last_active_at) : "—"}
                </p>
                {subagents.length > 0 && (
                  <p className="muted">运行时子代理：{subagents.length} 个运行中</p>
                )}
              </div>
            )}
            {runOpen && run && (
              <div className="rs-detail">
                <div className="rs-detail-meta">
                  <span className="mono">run {run.id.slice(0, 8)}</span>
                  <span className={`status status-${run.status}`}>{RUN_LABEL[run.status] ?? run.status}</span>
                  {run.failure_class && <span className="badge badge-in_progress">{run.failure_class}</span>}
                  <a href={`/runs/${run.id}`}>打开完整会话页 <Icon name="chevron-right" size={12} /></a>
                </div>
                {subagents.length > 0 && (
                  <div className="rs-subagents">
                    <strong>运行时子代理</strong>
                    <ul>
                      {subagents.map((sub) => (
                        <li key={sub.tool_use_id}>
                          <span className="mono">{sub.subagent_type || sub.tool_name || "subagent"}</span>
                          {sub.description ? ` · ${sub.description}` : ""}
                          <span className="muted"> · {sub.status || "running"}</span>
                          {sub.result_summary ? (
                            <div className="muted">{sub.result_summary}</div>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                    <p className="muted">
                      子代理由运行时管理；Anvil 只展示可观测到的状态和结果，不保证完整子上下文。
                    </p>
                  </div>
                )}
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
                  logs[run.id] === undefined ? (
                    <p className="muted">加载中…</p>
                  ) : isHeaderOnlyRunLog(logs[run.id]) ? (
                    <>
                      <p className="muted">{HEADER_ONLY_RUN_LOG_HINT}</p>
                      <pre className="run-log">{logs[run.id]}</pre>
                    </>
                  ) : (
                    <pre className="run-log">{logs[run.id]}</pre>
                  )
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
