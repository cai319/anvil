import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, isOnboardingRole, subscribeEvents, type BoardDraft, type KnowledgeRepoBinding, type KnowledgeRepoProfile, type Role, type Task, type TimelineEvent, type Workflow, type WorkflowStep, type WorkflowTemplate } from "../api/client";
import { RoleSessions } from "./RoleSessions";
import { ApplyTemplateModal } from "./ApplyTemplateModal";
import { WorkflowGraph } from "./WorkflowGraph";
import { roleStatusFromWorkflow } from "../workflowStatus";
import { formatClock } from "../time";
import { roleLiveDot, roleLiveState, stateClassForDot } from "../roleLive";
import { useRoleRuntimes } from "../useRoleRuntimes";
import { usePanelLayout } from "../usePanelLayout";
import { STATUS_META, isDynamicStep, summarizeLoops, type LoopSummary } from "../workflowGroups";
import { RoleAvatar } from "../components/RoleAvatar";
import { Icon } from "../components/Icon";

const STATUS_LABEL: Record<Task["status"], string> = {
  todo: "待办",
  in_progress: "进行中",
  in_review: "待评审",
  done: "完成",
  closed: "已终止",
};

const RUN_LABEL: Record<string, string> = {
  created: "已创建",
  projecting: "准备中",
  running: "运行中",
  submitted: "已提交",
  reviewing: "评审中",
  completed: "已完成",
  failed: "失败",
  abandoned: "已放弃",
  suspended: "已挂起",
};

/** 工作流概览：状态、完成度、步骤状态计数，规则/验证矩阵默认收起。 */
function WorkflowSummary({ workflow, taskStatus }: {
  workflow: Workflow;
  taskStatus: Task["status"];
}) {
  const total = workflow.steps.length;
  const approved = workflow.steps.filter((s) => s.status === "approved").length;
  const percent = total ? Math.round((approved / total) * 100) : 0;
  const stateLabel = workflow.status === "done"
    ? (taskStatus === "done" ? "已完成" : "已完成 · 等待最终评审")
    : workflow.status === "running" ? "推进中" : "已失败";
  const counts = (Object.keys(STATUS_META) as WorkflowStep["status"][])
    .map((status) => ({
      status,
      count: workflow.steps.filter((s) => s.status === status).length,
    }))
    .filter(({ status, count }) => count > 0 && status !== "approved");
  const feedbackRules = workflow.definition?.feedback_rules ?? [];
  const verification = workflow.definition?.verification ?? [];

  return (
    <div className={`wf-summary is-${workflow.status}`}>
      <div className="wf-summary-main">
        <span className="wf-summary-state">{stateLabel}</span>
        <div className="wf-summary-progress">
          <span className="wf-summary-bar"><i style={{ width: `${percent}%` }} /></span>
          <span className="wf-summary-count"><strong>{approved}</strong> / {total} 步完成</span>
        </div>
        <div className="wf-summary-pills">
          {counts.map(({ status, count }) => (
            <span key={status} className="wf-summary-pill" style={{ color: STATUS_META[status].color }}>
              <i style={{ background: STATUS_META[status].color }} />
              {STATUS_META[status].label} {count}
            </span>
          ))}
        </div>
      </div>
      {(feedbackRules.length > 0 || verification.length > 0) && (
        <details className="wf-summary-rules">
          <summary>
            <Icon className="wf-summary-chevron" name="chevron-right" size={12} />
            规则与验证
            <span className="muted">反馈 {feedbackRules.length} · 验证 {verification.length}</span>
          </summary>
          <div className="wf-summary-rules-body">
            {feedbackRules.length > 0 && (
              <p className="muted">反馈路径：{feedbackRules.join("；")}</p>
            )}
            {verification.length > 0 && (
              <div className="wf-matrix">
                <span className="muted">验证矩阵：</span>
                {verification.map((v, i) => (
                  <span key={i} className="wf-matrix-cell">
                    <strong>{v.cell}</strong> · {v.method}
                    {v.verifier_role ? ` · ${v.verifier_role}` : ""}
                  </span>
                ))}
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

// 系统伪发送者：workflow 引擎/系统通知不是真实 Agent 角色。
const SYSTEM_SENDERS: Record<string, string> = {
  workflow: "工作流引擎",
  system: "系统",
};

// @ 提及的边界字符：空白、开括号、常见中英文标点都可结束上一个词。
const MENTION_BOUNDARY = /[\s([{,，。；;:：.!?！？、）)]/;

/** 把文本里命中角色的 @slug/@显示名（昵称）标黄；未命中角色的 @ 保持原样。 */
function renderMentionText(text: string, tokens: Map<string, string>): ReactNode {
  if (!text || tokens.size === 0) return text;
  const keys = [...tokens.keys()].sort((a, b) => b.length - a.length);
  const boundary = (ch: string | undefined) => !ch || /[\s，。；;:：,.!?！？、）)]/.test(ch);
  const out: ReactNode[] = [];
  let i = 0;
  let last = 0;
  let idx = 0;
  while (i < text.length) {
    if (text[i] !== "@") { i += 1; continue; }
    const rest = text.slice(i + 1).toLowerCase();
    const hit = keys.find((k) => {
      if (!rest.startsWith(k)) return false;
      return boundary(text[i + 1 + k.length]);
    });
    if (!hit) { i += 1; continue; }
    const original = text.slice(i + 1, i + 1 + hit.length);
    out.push(text.slice(last, i));
    out.push(<span className="mention-tag" key={`mention-${idx++}`}>@{original}</span>);
    i += 1 + hit.length;
    last = i;
  }
  out.push(text.slice(last));
  return out;
}

function describe(
  ev: TimelineEvent,
  nameFor: (slug: string) => string = (slug) => slug,
): { text: string; runStatus?: string; mention?: string } {
  const p = ev.payload;
  switch (ev.kind) {
    case "comment":
      return { text: String(p.body ?? "") };
    case "mention": {
      const target = String(p.target ?? "human");
      const raw = String(p.message ?? "");
      // 正文可能以昵称或 slug 开头；两者都尝试去掉，避免标签与正文重复。
      let body = raw;
      for (const token of [nameFor(target), target]) {
        if (!token) continue;
        const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const re = new RegExp(`^@\\s*${escaped}\\s*[，,:：]?\\s*`, "i");
        if (re.test(body)) {
          body = body.replace(re, "");
          break;
        }
      }
      return { text: body, mention: target };
    }
    case "submit":
      return { text: "提交了评审" + (p.summary ? `：${p.summary}` : "") };
    case "review":
      return { text: p.approve ? "评审通过" : "评审驳回" };
    case "claim":
      return { text: "认领了任务" };
    case "unclaim":
      return { text: "放弃了任务" };
    case "heartbeat":
      return { text: "续租心跳" };
    case "wake":
      return { text: "被唤醒处理任务" };
    case "starvation":
      return { text: "饥饿升级（无角色可认领）" };
    case "run":
      return { text: `运行状态：${RUN_LABEL[String(p.status)] ?? p.status}`, runStatus: String(p.status) };
    case "step": {
      const status = String(p.status ?? "");
      const label: Record<string, string> = {
        running: "开始执行",
        submitted: "已提交（等待人工审核）",
        planned: "切片计划已提交",
        approved: "已通过",
        rework: "决策打回，返工中",
        rework_exhausted: "重做次数耗尽，需人工介入",
        rejected: "人工驳回，返工中",
        failed: "执行失败",
      };
      const summary = p.summary ? `：${String(p.summary)}` : "";
      return {
        text: `工作流步骤 ${String(p.step ?? "")}「${String(p.title ?? "")}」`
          + `${label[status] ?? status}${summary}`,
      };
    }
    default:
      return { text: ev.kind };
  }
}

const TL_FILTERS = [
  { key: "all", label: "全部" },
  { key: "dialog", label: "对话" },
  { key: "mention", label: "@提及" },
  { key: "step", label: "工作流" },
  { key: "run", label: "运行" },
  { key: "review", label: "评审" },
] as const;

const REPO_SOURCE_LABEL: Record<string, string> = {
  auto: "自动",
  cindy: "Cindy",
  human: "人工",
  inherited: "继承",
  agent: "Agent",
};

const REPO_STATE_HINT: Record<string, string> = {
  applied: "已按任务标签/文本自动关联。",
  suggested: "低置信度建议，确认后才会参与检索。",
  needs_cindy: "任务还没有标签，派发后由 Cindy 识别。",
  fallback: "未匹配到知识仓库，回退到执行 repo + shared。",
  locked: "已人工固定，自动识别和 Cindy 不会覆盖。",
  off: "知识仓库功能已关闭。",
};

function KnowledgeRepoPanel({ task, onChanged }: { task: Task; onChanged: () => void }) {
  const [repos, setRepos] = useState<KnowledgeRepoBinding[]>(task.knowledge_repos ?? []);
  const [locked, setLocked] = useState(Boolean(task.knowledge_binding_locked));
  const [state, setState] = useState<string | null>(task.knowledge_binding_state ?? null);
  const [scope, setScope] = useState<string[] | null>(null);
  const [catalog, setCatalog] = useState<KnowledgeRepoProfile[]>([]);
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((payload: {
    locked?: boolean; state?: string | null; repos?: KnowledgeRepoBinding[];
    scope?: { repos: string[] | null };
  }) => {
    if (payload.repos) setRepos(payload.repos);
    if (payload.locked !== undefined) setLocked(Boolean(payload.locked));
    if (payload.state !== undefined) setState(payload.state);
    if (payload.scope) setScope(payload.scope.repos);
  }, []);

  const repoKey = JSON.stringify(task.knowledge_repos ?? []);
  useEffect(() => {
    setRepos(task.knowledge_repos ?? []);
    setLocked(Boolean(task.knowledge_binding_locked));
    setState(task.knowledge_binding_state ?? null);
  }, [repoKey, task.knowledge_binding_locked, task.knowledge_binding_state]);

  useEffect(() => {
    setRepos(task.knowledge_repos ?? []);
    setLocked(Boolean(task.knowledge_binding_locked));
    setState(task.knowledge_binding_state ?? null);
    api.getTaskKnowledgeRepos(task.id)
      .then((d) => apply(d))
      .catch(() => {});
    api.listKnowledgeRepos()
      .then((d) => setCatalog(d.repos ?? []))
      .catch(() => setCatalog([]));
  }, [task.id, apply]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const visible = repos.filter((r) => r.status !== "dismissed");
  const applied = visible.filter((r) => r.status === "applied");
  const suggested = visible.filter((r) => r.status === "suggested");

  return (
    <>
      <h3 className="side-h">知识仓库</h3>
      <div className="repo-panel">
        {visible.length === 0 && (
          <p className="muted hint">{REPO_STATE_HINT[state ?? ""] ?? "尚未关联知识仓库。"}</p>
        )}
        <div className="repo-chips">
          {[...applied, ...suggested].map((r) => (
            <span
              key={`${r.repo}-${r.status}`}
              className={`repo-chip repo-${r.status}`}
              title={r.reason ?? undefined}
            >
              <span className="repo-chip-name">{r.repo}</span>
              <span className="repo-chip-meta">
                {REPO_SOURCE_LABEL[r.source] ?? r.source}
                {r.status === "suggested" ? " · 建议" : ""}
              </span>
              {r.status === "suggested" && (
                <span className="repo-chip-actions">
                  <button type="button" disabled={busy}
                    onClick={() => run(async () => {
                      const d = await api.acceptTaskKnowledgeRepo(task.id, r.repo) as {
                        locked: boolean; state: string; repos: KnowledgeRepoBinding[]; scope: { repos: string[] | null };
                      };
                      apply(d);
                    })}>采纳</button>
                  <button type="button" disabled={busy}
                    onClick={() => run(async () => {
                      const d = await api.dismissTaskKnowledgeRepo(task.id, r.repo) as {
                        locked: boolean; state: string; repos: KnowledgeRepoBinding[]; scope: { repos: string[] | null };
                      };
                      apply(d);
                    })}>忽略</button>
                </span>
              )}
            </span>
          ))}
        </div>
        {state && (
          <p className="muted hint">
            {REPO_STATE_HINT[state] ?? state}
            {scope?.length ? ` 检索范围：${scope.join("、")}。` : ""}
          </p>
        )}
        {locked && <p className="repo-lock">已人工固定</p>}
        {!editing ? (
          <div className="repo-actions">
            <button type="button" className="small" disabled={busy}
              onClick={() => {
                setSelected(applied.map((r) => r.repo));
                setEditing(true);
              }}>编辑</button>
            <button type="button" className="small" disabled={busy}
              onClick={() => run(async () => {
                const d = await api.reautoTaskKnowledgeRepos(task.id) as {
                  locked: boolean; state: string | null; repos: KnowledgeRepoBinding[]; scope: { repos: string[] | null };
                };
                apply(d);
              })}>重新识别</button>
          </div>
        ) : (
          <div className="repo-editor">
            <select
              multiple
              value={selected}
              onChange={(e) => setSelected(
                Array.from(e.currentTarget.selectedOptions).map((o) => o.value))}
            >
              {catalog.map((p) => (
                <option key={p.key} value={p.repo}>{p.repo}</option>
              ))}
            </select>
            <div className="repo-actions">
              <button type="button" className="small" disabled={busy}
                onClick={() => run(async () => {
                  const next = await api.patchTask(task.id, {
                    knowledge_repos: selected.map((repo) => ({ repo })),
                  });
                  setRepos(next.knowledge_repos ?? []);
                  setLocked(Boolean(next.knowledge_binding_locked));
                  setState(next.knowledge_binding_state ?? null);
                  setEditing(false);
                })}>保存并固定</button>
              <button type="button" className="small" disabled={busy}
                onClick={() => setEditing(false)}>取消</button>
            </div>
          </div>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    </>
  );
}

export function TaskDetail({ id }: { id: string }) {
  const [task, setTask] = useState<Task | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [assignee, setAssignee] = useState("");
  const [comment, setComment] = useState("");
  const [descOpen, setDescOpen] = useState(false);
  const [descOverflow, setDescOverflow] = useState(false);
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const blurTimerRef = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (blurTimerRef.current != null) window.clearTimeout(blurTimerRef.current);
    };
  }, []);
  const descRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const [drafts, setDrafts] = useState<BoardDraft[]>([]);
  const [dispatchNote, setDispatchNote] = useState<string | null>(null);
  const [tab, setTab] = useState<"chat" | "workflow" | "subtasks" | "runtime">("chat");
  const [tlKind, setTlKind] = useState<"all" | "dialog" | "mention" | "step" | "run" | "review">("all");
  const [tlLimit, setTlLimit] = useState(40);
  const threadRef = useRef<HTMLElement>(null);
  const pinnedRef = useRef(true);          // 用户是否停留在底部
  const initialScrollRef = useRef(true);   // 刚打开任务/切回会话时强制滚到底
  const autoScrollingRef = useRef(false);  // 程序化滚动期间忽略 onScroll 的“用户滚动”判定
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [children, setChildren] = useState<Task[]>([]);
  const [wfView, setWfView] = useState<"graph" | "list">("graph");
  const [showApplyTemplate, setShowApplyTemplate] = useState(false);
  const [seedTemplates, setSeedTemplates] = useState<WorkflowTemplate[]>([]);
  const [seedId, setSeedId] = useState("");
  const [busy, setBusy] = useState(false);
  const { slugs: liveSlugs, runtimes: roleRuntimes } = useRoleRuntimes(task);
  const panel = usePanelLayout({ storageKey: "task" });

  const refreshWorkflow = () =>
    api.getTaskWorkflow(id).then(setWorkflow).catch(() => setWorkflow(null));

  const refreshChildren = () =>
    api.listTaskChildren(id).then(setChildren).catch(() => []);

  const refresh = () =>
    Promise.all([api.getTask(id), api.getTaskTimeline(id)])
      .then(([t, tl]) => {
        setTask(t);
        setTimeline(tl);
        setGone(false);
      })
      .catch((e) => {
        // 任务被删除：停止轮询（不再被 SSE tick 唤醒），给出可返回大厅的提示
        if (String(e).includes("404")) setGone(true);
        else setError(String(e));
      });

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const el = threadRef.current;
    if (!el) return;
    pinnedRef.current = true;
    autoScrollingRef.current = true;
    // 等一帧确保新消息已经完成布局，再滚到容器底部（不滚动整个页面）
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior });
        window.setTimeout(() => { autoScrollingRef.current = false; },
                          behavior === "smooth" ? 500 : 80);
      });
    });
  }, []);

  const scrollToTop = useCallback(() => {
    const el = threadRef.current;
    if (!el) return;
    pinnedRef.current = false;  // 人为回到最早：暂停自动跟随，直到用户再次滚到底
    autoScrollingRef.current = true;
    el.scrollTo({ top: 0, behavior: "smooth" });
    window.setTimeout(() => { autoScrollingRef.current = false; }, 500);
  }, []);

  useEffect(() => {
    if (gone) return; // 任务已删除：不再刷新、不再订阅事件
    setDescOpen(false);  // 切换任务时收起描述
    setDescOverflow(false);
    refresh();
    refreshWorkflow();
    refreshChildren();
    api.listRoles().then(setRoles).catch(() => {});
    api.listWorkflowTemplates(undefined, "active").then((list) => {
      setSeedTemplates(list.filter((item) => item.published_version
        && (item.use_mode === "seed" || item.use_mode === "both")));
    }).catch(() => setSeedTemplates([]));
    const refreshDrafts = () => api.listDrafts(id).then(setDrafts).catch(() => {});
    refreshDrafts();
    return subscribeEvents((ev) => {
      if (ev.topic === "tick" || (ev.topic === "task" && ev.payload.id === id)) {
        refresh();
        refreshWorkflow();
        refreshChildren();
        refreshDrafts();
      }
    });
  }, [id, gone]);

  useEffect(() => {
    setSeedId(task?.plan_seed?.template_id || seedTemplates[0]?.id || "");
  }, [task?.plan_seed?.template_id, seedTemplates]);

  // 打开任务、切回「会话」或切换筛选时，默认显示最后一条消息。
  useEffect(() => {
    pinnedRef.current = true;
    initialScrollRef.current = true;
    if (tab === "chat") {
      scrollToBottom("auto");
      // 线程已挂载时这次就是首次定位；清掉 initial，避免用户随后向上
      // 翻看历史时被下一条新消息强行拉回底部。
      if (threadRef.current) initialScrollRef.current = false;
    } else {
      setMention(null);  // 离开会话页签时关闭联想
    }
  }, [id, tab, tlKind, scrollToBottom]);

  // 新消息到达：只有用户本来就在底部（或首次加载）时才跟随滚动；
  // 用户主动向上翻看历史时不打断。
  useEffect(() => {
    if (tab !== "chat" || !task || task.id !== id) return;
    if (timeline.length === 0) return;
    if (initialScrollRef.current) {
      initialScrollRef.current = false;
      scrollToBottom("auto");
    } else if (pinnedRef.current) {
      scrollToBottom("smooth");
    }
  }, [timeline.length, tab, task?.id, id, scrollToBottom]);

  const onThreadScroll = () => {
    if (autoScrollingRef.current) return;  // 程序化滚动不改写“用户是否在底部”
    const el = threadRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  // 线程高度由 flex/视口计算，首帧时可能还没有最终高度；ResizeObserver 在拿到
  // 真实 clientHeight 后再滚到底，避免打开任务时停在最旧消息。
  useEffect(() => {
    if (tab !== "chat") return;
    const el = threadRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (initialScrollRef.current || pinnedRef.current) {
        el.scrollTop = el.scrollHeight;
        initialScrollRef.current = false;
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [tab, task?.id]);


  // 描述被 6 行截断时显示“展开全文”；用实际布局高度判断，避免按字符数误判。
  useEffect(() => {
    const el = descRef.current;
    if (!el) return;
    const check = () => setDescOverflow(el.scrollHeight > el.clientHeight + 1);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [task?.description, descOpen]);

  const filteredTimeline = timeline.filter((ev) => {
    if (tlKind === "all") return true;
    if (tlKind === "dialog") return ev.kind === "comment" || ev.kind === "mention";
    return ev.kind === tlKind;
  });
  const visibleTimeline = filteredTimeline.slice(-tlLimit);

  if (gone)
    return (
      <div className="session">
        <p className="error">任务不存在或已被删除。</p>
        <p><a href="/"><Icon name="chevron-left" /> 返回任务大厅</a></p>
      </div>
    );
  if (error) return <p className="error">{error}</p>;
  if (!task) return <p className="muted">加载中…</p>;

  /** 任务信息面板统一取数：点灯、状态徽标、排队数、详情文案。 */
  const roleLiveFor = (slug: string) => {
    const rt = roleRuntimes[slug];
    const ctxHere = rt?.contexts?.find((c) => c.task_id === task.id);
    const wfState = roleStatusFromWorkflow(workflow, slug);
    const queued = rt?.queue_tasks?.find((q) => q.id === task.id)?.count ?? 0;
    const live = roleLiveState(rt, task.id, {
      hasOpenStep: wfState?.cls === "in_progress",
      allDone: wfState?.cls === "done",
      queuedHere: queued,
    });
    let state: { label: string; cls: string } = live;
    if (wfState?.cls === "closed") state = wfState;
    else if (live.cls === "todo" && wfState?.cls === "done") state = wfState;
    const dot = roleLiveDot(
      rt, task.id, Boolean(ctxHere?.has_session),
      wfState?.cls === "in_progress", wfState?.cls === "closed",
    );
    const display = roles.find((r) => r.slug === slug)?.display_name ?? slug;
    let detail = "";
    if (rt?.switching) detail = "正在切换任务上下文…";
    else if (live.cls === "busy") {
      detail = `正在处理「${rt?.current_task?.title ?? rt?.current_task_id}」`;
      if (queued) detail += ` · 本任务排队 ${queued} 条`;
    } else if (live.cls === "in_progress") {
      detail = rt?.current_step_id ? `当前步骤 ${rt.current_step_id}` : "正在本任务工作";
    } else if (live.cls === "queued") {
      detail = queued > 0 ? `本任务排队 ${queued} 条` : "等待上下文切换";
    } else if (!rt) {
      detail = "状态加载中…";
    } else if (ctxHere?.has_session) {
      detail = "空闲 · 本任务上下文已保留";
    } else {
      detail = "空闲 · 可安排任务";
    }
    const title = `${display}：${dot.title}`
      + (queued ? ` · 本任务有 ${queued} 条消息排队，等当前轮结束` : "");
    return { slug, rt, claim: (task.role_claims ?? []).find((c) => c.role_slug === slug),
             state, dot, queued, detail, title };
  };

  /** 点灯 -> 状态徽标 -> slug 的统一呈现。 */
  const renderRoleLive = (live: ReturnType<typeof roleLiveFor>) => (
    <span className="role-live-chip" title={live.title}>
      <RoleAvatar
        slug={live.slug}
        name={roles.find((r) => r.slug === live.slug)?.display_name ?? live.slug}
        avatar={roles.find((r) => r.slug === live.slug)?.avatar}
        color={roles.find((r) => r.slug === live.slug)?.avatar_color}
        size={18}
        status={live.dot.cls}
      />
      <i className={`role-live-dot ${live.dot.cls}`} aria-label={live.title} />
      <span className={`status status-${stateClassForDot(live.dot.cls)} role-live-state`}>
        {live.state.label}
      </span>
      <span className="role-live-name">{live.slug}</span>
      {live.queued > 0 && (
        <span className="role-live-queue" title={`本任务有 ${live.queued} 条消息排队，等当前轮结束`}>
          排队 {live.queued}
        </span>
      )}
    </span>
  );

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const assignableRoles = roles.filter((r) => !isOnboardingRole(r));

  // 优先联想任务参与角色；Cindy（onboarding 助理）和 task.planner 指向的真实角色始终加入：
  // 系统 @human 通知由它们发出，human 需要能直接 @ 回去答复。
  const assignedToTask = new Set(task.assignees ?? []);
  const baseMentionRoles = assignedToTask.size
    ? assignableRoles.filter((r) => assignedToTask.has(r.slug))
    : assignableRoles;
  const onboardingAssistant = roles.find((r) => isOnboardingRole(r));
  const plannerRole = task.planner
    ? roles.find((r) => r.slug === task.planner)
    : undefined;
  const extraSeen = new Set<string>();
  const extraMentionRoles = [onboardingAssistant, plannerRole]
    .filter((r): r is Role => Boolean(r))
    .filter((r) => {
      if (baseMentionRoles.some((x) => x.slug === r.slug)
          || extraSeen.has(r.slug)) return false;
      extraSeen.add(r.slug);
      return true;
    });
  // Cindy/planner 放前面：候选最多只显示 10 条，不能让它们被大量任务成员挤出。
  const mentionRoles = [...extraMentionRoles, ...baseMentionRoles];

  // slug 是后台身份；display_name 是界面昵称。两者都注册为 token，
  // 发送时统一解析回 slug，保证消息正文可显示昵称、后台仍按 slug 激活角色。
  const roleTokens = new Map<string, string>();
  mentionRoles.forEach((r) => {
    roleTokens.set(r.slug.toLowerCase(), r.slug);
    if (r.display_name) roleTokens.set(r.display_name.toLowerCase(), r.slug);
  });

  // human 是自然人身份，后台 slug 固定为 human，界面统一显示「你（自然人）」。
  roleTokens.set("human", "human");
  roleTokens.set("你（自然人）", "human");
  const displayNameFor = (slug: string) =>
    slug === "human"
      ? "你（自然人）"
      : (roles.find((r) => r.slug === slug)?.display_name || slug);

  const mentionQuery = (mention?.query || "").toLowerCase();
  const seenMentionSlugs = new Set<string>();
  const mentionCandidates = mentionRoles.filter((r) => {
    if (seenMentionSlugs.has(r.slug)) return false;
    seenMentionSlugs.add(r.slug);
    const name = r.display_name || r.slug;
    return !mentionQuery
      || r.slug.toLowerCase().includes(mentionQuery)
      || name.toLowerCase().includes(mentionQuery);
  }).map((r) => ({ slug: r.slug, name: r.display_name || r.slug })).slice(0, 10);

  // 从光标位置向前找 @token；只有 @ 位于开头/空白/标点后才触发联想。
  const syncMention = (value: string, caret: number | null) => {
    const pos = caret ?? value.length;
    const before = value.slice(0, pos);
    const at = before.lastIndexOf("@");
    if (at < 0) {
      setMention(null);
      return;
    }
    const prev = at > 0 ? before[at - 1] : "";
    if (prev && !MENTION_BOUNDARY.test(prev)) {
      setMention(null);
      return;
    }
    const query = before.slice(at + 1);
    if (/\s/.test(query)) {
      setMention(null);
      return;
    }
    setMention({ start: at, query });
    setMentionIndex(0);
  };

  const insertMention = () => {
    if (blurTimerRef.current != null) {
      window.clearTimeout(blurTimerRef.current);
      blurTimerRef.current = null;
    }
    const el = inputRef.current;
    const focused = el != null && document.activeElement === el;
    const start = focused && el.selectionStart != null ? el.selectionStart : comment.length;
    const end = focused && el.selectionEnd != null ? el.selectionEnd : start;
    const before = comment.slice(0, start);
    const token = before && !MENTION_BOUNDARY.test(before.slice(-1)) ? " @" : "@";
    const next = comment.slice(0, start) + token + comment.slice(end);
    setComment(next);
    const caret = start + token.length;
    syncMention(next, caret);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(caret, caret);
    });
  };

  const chooseMention = (candidate: { slug: string; name: string }) => {
    const input = inputRef.current;
    if (!input || !mention) return;
    const caret = input.selectionStart ?? comment.length;
    let after = comment.slice(caret);
    if (after.startsWith(" ")) after = after.slice(1);
    // 输入框显示昵称，真正发送时再解析回 slug；后台数据和激活都只用 slug。
    const next = comment.slice(0, mention.start)
      + "@" + candidate.name + " " + after;
    setComment(next);
    setMention(null);
    const pos = mention.start + candidate.name.length + 2; // @ + 昵称 + space
    requestAnimationFrame(() => {
      input.focus();
      input.setSelectionRange(pos, pos);
    });
  };

  const onComposerKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // 中文输入法组合期间的 Enter 用于上屏，不触发联想选择/发送
    if (e.nativeEvent.isComposing) return;
    if (mention && mentionCandidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionIndex((i) => (i + 1) % mentionCandidates.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionIndex((i) =>
          (i - 1 + mentionCandidates.length) % mentionCandidates.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        chooseMention(mentionCandidates[mentionIndex] ?? mentionCandidates[0]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    if (e.key === "Enter") send();
  };

  const send = async () => {
    const text = comment.trim();
    if (!text) return;
    setMention(null);
    // 自动解析消息中的 @角色（slug 或显示名），命中参与角色/Cindy/planner
    // 即走 mention（唤醒 + 时间线标黄）
    // 消息正文可以写昵称或 slug；这里统一解析回 slug 作为 mention target。
    // 例：输入框显示「@产品经理」，发送时 target=「pm」，正文保留昵称。
    const lower = text.toLowerCase();
    const hasToken = (token: string) => {
      const needle = "@" + token.toLowerCase();
      let at = lower.indexOf(needle);
      while (at >= 0) {
        const next = text[at + needle.length];
        if (!next || /[\s，。；;:：,.!?！？、）)]/.test(next)) return true;
        at = lower.indexOf(needle, at + 1);
      }
      return false;
    };
    const targets = [...new Set(mentionRoles
      .filter((r) => hasToken(r.slug) || (r.display_name ? hasToken(r.display_name) : false))
      .map((r) => r.slug))];
    await act(async () => {
      if (targets.length) {
        for (const target of targets) {
          await api.mentionTask(id, target, text);
        }
      } else {
        await api.commentTask(id, text);
      }
      setComment("");
    });
    // 发送后把自己刚发的消息带进视野
    pinnedRef.current = true;
    scrollToBottom("smooth");
  };

  const statusClass = (status: WorkflowStep["status"]) =>
    status === "approved" ? "done"
      : status === "running" ? "in_progress"
      : status === "submitted" ? "in_review"
      : status === "pending" ? "todo"
      : status === "rejected" ? "in_review"
      : "closed";

  const renderStepRow = (st: WorkflowStep) => {
    const cls = statusClass(st.status);
    const label = STATUS_META[st.status].label;
    return (
      <div key={st.step_id} className="wf-step">
        <span className="wf-id mono">{st.step_id}</span>
        <div className="wf-main">
          <div className="wf-title">
            {st.title}
            <span className="wf-role">{st.role_slug}</span>
            {st.reviewer && <span className="wf-role">验证：{st.reviewer}</span>}
            {st.gate && <span className="badge wf-gate">审核门</span>}
            <span className={`status status-${cls}`}>{label}</span>
          </div>
          {st.depends_on?.length > 0 && <div className="muted wf-deps">依赖：{st.depends_on.join("、")}</div>}
          {st.contract && <div className="muted wf-deps">契约：{st.contract}</div>}
          {st.failure_reason && <div className="muted wf-deps">失败归因：{st.failure_reason}</div>}
          {st.summary && <div className="muted wf-summary">{st.summary}</div>}
        </div>
        <div className="wf-actions">
          {st.run_id && <a href={`/runs/${st.run_id}`}>会话</a>}
          {st.gate && st.status === "submitted" && (
            <>
              <button className="small primary" onClick={() => act(async () => {
                const w = await api.reviewWorkflowStep(workflow!.id, st.step_id, true);
                setWorkflow(w);
              })}>通过</button>
              <button className="small danger" onClick={() => act(async () => {
                const w = await api.reviewWorkflowStep(workflow!.id, st.step_id, false, "请按评审意见修改");
                setWorkflow(w);
              })}>驳回</button>
            </>
          )}
          {(st.status === "failed" || st.status === "rejected") && (
            <button className="small" onClick={() => act(async () => {
              const w = await api.retryWorkflowStep(workflow!.id, st.step_id);
              setWorkflow(w);
            })}>重试</button>
          )}
        </div>
      </div>
    );
  };

  const renderLoopList = (loop: LoopSummary) => {
    const meta = STATUS_META[loop.status];
    return (
      <details key={`loop-${loop.sourceStepId}`} className="wf-loop-list">
        <summary className="wf-loop-summary">
          <span className="wf-loop-mark"><Icon name="refresh" size={12} /> 切片循环</span>
          <span className={`status status-${statusClass(loop.status)}`}>{meta.label}</span>
          <span className="muted">
            {loop.totalSlices} 片 · {loop.totalSteps} 步 · 已完成 {loop.doneSlices}/{loop.totalSlices} 片
          </span>
          {loop.currentGroup && (
            <span className="muted">
              当前 {loop.currentGroup.title} · {loop.currentPhase}（{loop.currentProgress}）
            </span>
          )}
        </summary>
        <div className="wf-slice-list">
          {loop.groups.map((g) => (
            <details key={g.key} className="wf-slice-item">
              <summary className="wf-slice-item-summary">
                <i className="wf-slice-dot" style={{ background: STATUS_META[g.status].color }} />
                <span className="wf-slice-name" title={g.title}>{g.title}</span>
                <span className="muted">{g.done}/{g.total} · {g.phase}</span>
              </summary>
              <div className="wf-steps">{g.steps.map(renderStepRow)}</div>
            </details>
          ))}
        </div>
      </details>
    );
  };

  return (
    <div className="session task-page">
      <div className="task-sheet">
      <div className="task-sheet-top">
        <p className="session-back">
          <a href="/"><Icon name="chevron-left" /> 任务大厅</a>
        </p>
        <div className="task-sheet-actions">
          {["todo", "in_progress", "in_review"].includes(task.status) && (
            <button
              className="small danger"
              disabled={busy}
              onClick={() => {
                if (!window.confirm(
                  "人工终止后任务会进入「已终止」状态，可重新打开；确定终止？")) return;
                act(() => api.cancelTask(id));
              }}
            >人工终止任务</button>
          )}
          <button
            className="small danger"
            disabled={busy}
            onClick={async () => {
              if (!window.confirm("删除任务会同时删除它的 runs、评论和依赖，且不可恢复。确定？")) return;
              setBusy(true);
              try {
                await api.deleteTask(id, true);
                window.location.href = "/";
              } catch (e) {
                setError(String(e));
                setBusy(false);
              }
            }}
          >删除任务</button>
        </div>
      </div>

      {/* 头部 */}
      <div className="session-head">
        <div className="session-title">
          <h1>{task.title}</h1>
          <span className={`status status-${task.status}`}>{STATUS_LABEL[task.status]}</span>
        </div>
        <div className="session-meta">
          {task.assignee && (
            <span className="session-meta-item"><Icon name="user" /> {task.assignee}</span>
          )}
          {task.assignees?.length > 0 ? (
            <span className="session-meta-item"><Icon name="users" /> {task.assignees.join("、")}</span>
          ) : !task.assignee ? (
            <span className="muted">未指派</span>
          ) : null}
          {task.tags?.length > 0 && <span className="muted">#{task.tags.join(" #")}</span>}
          <span className="muted mono">{task.id}</span>
        </div>
      </div>

      {/* 待评审横幅（HITL 醒目提示） */}
      {task.status === "in_review" && (
        <div className="review-banner">
          <span className="review-banner-text"><Icon name="alert" /> 待人工评审 —— 请确认产出后「通过」或「驳回」</span>
          <button className="primary" onClick={() => act(() => api.reviewTask(id, true))} disabled={busy}>通过</button>
          <button className="danger" onClick={() => act(() => api.reviewTask(id, false))} disabled={busy}>驳回</button>
        </div>
      )}

      <div className="detail-tabs">
        <button className={`tab ${tab === "chat" ? "active" : ""}`} onClick={() => setTab("chat")}>
          会话 {timeline.length > 0 && <span className="tab-count">{timeline.length}</span>}
        </button>
        <button className={`tab ${tab === "workflow" ? "active" : ""}`} onClick={() => setTab("workflow")}>
          工作流 {workflow && <span className="tab-count">{workflow.steps.filter((s) => s.status === "approved").length}/{workflow.steps.length}</span>}
        </button>
        <button className={`tab ${tab === "subtasks" ? "active" : ""}`} onClick={() => setTab("subtasks")}>
          子任务 {children.length > 0 && <span className="tab-count">{children.length}</span>}
        </button>
        <button className={`tab ${tab === "runtime" ? "active" : ""}`} onClick={() => setTab("runtime")}>
          角色运行时 {(task.assignees ?? []).length > 0 && <span className="tab-count">{task.assignees.length}</span>}
        </button>
        {panel.collapsed && (
          <button
            type="button"
            className="small"
            style={{ marginLeft: "auto", alignSelf: "center" }}
            onClick={panel.toggle}
          >
            显示任务信息
          </button>
        )}
      </div>
      </div>

      <div className={`task-tab-body ${tab === "chat" ? "is-chat" : ""} ${tab === "workflow" ? "is-workflow" : ""}`}>
      {tab === "chat" && (
      <div
        className={`task-layout ${panel.collapsed ? "panel-collapsed" : ""}`}
        style={panel.style}
      >
        {/* 左：会话流 */}
        <div className="task-main">
          <div className="session-actions">
            {task.status === "todo" && (
              <>
                <select value={assignee} onChange={(e) => setAssignee(e.target.value)} title="选择要指派的角色">
                  <option value="">选择角色…</option>
                  {assignableRoles.map((r) => (
                    <option key={r.slug} value={r.slug}>{r.display_name}</option>
                  ))}
                </select>
                <button
                  className="primary"
                  onClick={() => act(async () => {
                    // 先写入参与角色（让 @拉入 / 下游交接生效），再拉起 run
                    await api.assignRoles(id, [assignee]);
                    await api.startRun(assignee, id);
                  })}
                  disabled={busy || !assignee}
                >指派并执行</button>
              </>
            )}
            {(task.status === "todo" || task.status === "in_progress") && (
              <button
                className="primary"
                disabled={busy}
                title="0 角色时 Cindy 会先分析并建议角色；多角色任务先由 Cindy 规划工作流；单角色任务若无标签或知识仓库匹配较弱，也会先由 Cindy 识别知识仓库。草案经你批准后执行"
                onClick={() => act(async () => {
                  const res = await api.dispatchTask(id);
                  setDispatchNote(
                    res.mode === "planning"
                      ? (task.assignees?.length
                          ? `规划助理 ${res.planner} 正在分析任务并设计工作流…草案将出现在收件箱，批准后自动派发子任务。`
                          : `规划助理 ${res.planner} 正在分析任务与角色缺口；若角色不足，会先提交角色建议等待你审批。`)
                      : res.mode === "pending_review"
                        ? (res.draft_kind === "role_suggestion"
                            ? "已有待审角色建议，请到收件箱审阅批准。"
                            : "已有待审工作流草案，请到收件箱审阅批准。")
                        : "已直接派发参与角色。");
                })}
              >{(task.assignees?.length ?? 0) === 0
                  ? "派发（Cindy 分析并建议角色）"
                  : (task.assignees?.length ?? 0) >= 2
                    ? "派发（先由 Cindy 规划）"
                    : (!task.knowledge_binding_locked
                        && ["needs_cindy", "suggested", "fallback"].includes(
                          task.knowledge_binding_state ?? ""))
                      ? "派发（Cindy 识别知识仓库）"
                      : "派发并执行"}</button>
            )}
            {task.plan_first && task.status === "todo" && (
              <button
                className="primary"
                disabled={busy || !task.planner}
                title={task.planner ? `唤醒 ${task.planner} 生成工作流草案` : "任务未设置 planner"}
                onClick={() => act(() => api.planTask(id))}
              >生成工作流</button>
            )}
            {task.status === "in_progress" && (
              <>
                <button onClick={() => act(() => api.unclaimTask(id))} disabled={busy}>放弃</button>
                <button className="primary" onClick={() => act(() => api.submitTask(id))} disabled={busy}>
                  {task.assignees?.length > 0 ? "人工收口并提交评审" : "提交评审"}
                </button>
              </>
            )}
            {task.status === "done" && <span className="muted">已完成。</span>}
            {task.status === "closed" && (
              <button onClick={() => act(() => api.reopenTask(id))} disabled={busy}>重新打开</button>
            )}
          </div>

          {dispatchNote && <p className="muted">{dispatchNote}</p>}

          {drafts.some((d) => d.status === "pending" && d.kind === "role_suggestion") && (
            <p className="muted">
              有角色建议待审批，请到收件箱处理；批准只创建/绑定角色，需要继续时在任务会话 @Cindy 或对应角色。
            </p>
          )}
          {drafts.filter((d) => d.status === "pending" && d.kind !== "role_suggestion").length > 0 && (
            <section className="session-thread">
              <h2 className="pane-title">工作流草案 · 待审批</h2>
              {drafts.filter((d) => d.status === "pending" && d.kind !== "role_suggestion").map((d) => (
                <div key={d.id} className="card" style={{ marginBottom: 8 }}>
                  <strong>{d.goal}</strong>
                  <div className="muted">
                    by {d.created_by}
                    {d.tree?.leader && <> · leader：{d.tree.leader}</>}
                    {d.tree?.steps?.length
                      ? <> · {d.tree.steps.length} 个步骤</>
                      : d.tree?.children
                        ? <> · {d.tree.children.length} 个子任务</>
                        : null}
                  </div>
                  {d.tree?.loop && (
                    <div className="muted">
                      动态切片：切片计划步骤由执行角色运行时盘点项目后产出；
                      {"服务端按 loop 模板为每片生成 execute -> verify -> fix -> verify2 -> decide 小闭环。"}
                    </div>
                  )}
                  {d.tree?.steps?.length ? (
                    <ul className="muted" style={{ margin: "6px 0", paddingLeft: 18 }}>
                      {d.tree.steps.map((s, i) => (
                        <li key={s.id ?? i}>
                          {s.id ? `${s.id} ` : ""}{s.title}{" -> "}{s.role ?? "未指派"}
                          {s.kind === "plan_slices" ? "（切片计划）" : ""}
                          {s.depends_on?.length ? `（依赖 ${s.depends_on.join("、")}）` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : d.tree?.children ? (
                    <ul className="muted" style={{ margin: "6px 0", paddingLeft: 18 }}>
                      {d.tree.children.map((c, i) => (
                        <li key={c.id ?? i}>
                          {c.title}{" -> "}{c.role ?? "未指派"}
                          {c.depends_on?.length ? `（依赖 ${c.depends_on.join("、")}）` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {d.tree?.feedback_rules?.length ? (
                    <div className="muted">反馈路径：{d.tree.feedback_rules.join("；")}</div>
                  ) : null}
                  <div className="card-actions">
                    {d.kind === "workflow" && (
                      <a className="small button-like" href={`/templates?draft=${encodeURIComponent(d.id)}`}>
                        在模板工作台编辑
                      </a>
                    )}
                    <button
                      className="primary small"
                      disabled={busy}
                      onClick={() => act(async () => {
                        await api.approveDraft([d.id]);
                        await api.listDrafts(id).then(setDrafts).catch(() => {});
                      })}
                    >{d.kind === "workflow" ? "批准并启动工作流" : "批准并落板（子任务按依赖自动派发）"}</button>
                  </div>
                </div>
              ))}
            </section>
          )}

          <section className="session-thread session-thread-main" ref={threadRef} onScroll={onThreadScroll}>
            <div className="thread-head">
              <h2 className="pane-title">会话 · {filteredTimeline.length} 条</h2>
              <div className="thread-head-right">
                <button className="small thread-jump" onClick={scrollToTop}
                  title="跳到最早一条消息">最早</button>
                <button className="small thread-jump"
                  onClick={() => scrollToBottom("smooth")}
                  title="跳到最新一条消息">最新</button>
                <div className="thread-filters">
                  {TL_FILTERS.map((f) => (
                    <button key={f.key} className={`small ${tlKind === f.key ? "primary" : ""}`}
                      onClick={() => setTlKind(f.key)}>{f.label}</button>
                  ))}
                </div>
              </div>
            </div>
            {filteredTimeline.length > tlLimit && (
              <button className="small load-earlier" onClick={() => setTlLimit((n) => n + 40)}>
                显示更早的 {filteredTimeline.length - tlLimit} 条
              </button>
            )}
            {filteredTimeline.length === 0 && (
              <p className="muted empty">当前筛选下没有交互。</p>
            )}
            {visibleTimeline.map((ev) => {
              const d = describe(ev, displayNameFor);
              const role = ev.role ?? "human";
              const isHuman = role === "human";
              const systemLabel = SYSTEM_SENDERS[role];
              // 聊天框用户名显示昵称；后台仍保留 slug 作为身份。
              const displayRole = isHuman
                ? "你（自然人）"
                : (systemLabel ?? displayNameFor(role));
              const roleInfo = roles.find((r) => r.slug === role);
              const liveRole = !isHuman && !systemLabel ? roleLiveFor(role) : null;
              return (
                <div
                  className={`thread-item ${ev.kind === "comment" ? "is-message" : ""} ${d.mention ? "is-mention" : ""} ${isHuman ? "is-human" : ""} ${systemLabel ? "is-system-event" : ""}`}
                  key={ev.key}
                >
                  {d.mention ? (
                    <span className="thread-avatar mention-avatar">@</span>
                  ) : isHuman ? (
                    <span className="thread-avatar human-avatar">人</span>
                  ) : systemLabel ? null : (
                    <RoleAvatar
                      slug={role}
                      name={displayRole}
                      avatar={roleInfo?.avatar}
                      color={roleInfo?.avatar_color}
                      size={32}
                      className="thread-role-avatar"
                      status={liveRole?.dot.cls}
                    />
                  )}
                  <div className="thread-body">
                    <div className="thread-meta">
                      <strong title={isHuman ? "human" : `${displayRole}（${role}）`}>
                        {displayRole}
                      </strong>
                      <span className="muted mono">{formatClock(ev.ts)}</span>
                      {d.runStatus && <span className={`status status-${d.runStatus}`}>{RUN_LABEL[d.runStatus]}</span>}
                    </div>
                    <div className="thread-text">
                      {d.mention ? (
                        <>
                          <span className="mention-tag">
                            @{displayNameFor(d.mention)}
                          </span>{" "}
                          {renderMentionText(d.text, roleTokens)}
                        </>
                      ) : (
                        renderMentionText(d.text, roleTokens)
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </section>

          {/* 评论 / @提及 框 */}
          <div className="session-compose">
            <button
              type="button"
              className="compose-at"
              onMouseDown={(event) => event.preventDefault()}
              onClick={insertMention}
              aria-label="提及角色"
              title="提及角色"
            >
              <Icon name="at-sign" />
            </button>
            <div className="mention-input-wrap">
              <input
                ref={inputRef}
                value={comment}
                onChange={(e) => {
                  const value = e.target.value;
                  setComment(value);
                  syncMention(value, e.target.selectionStart);
                }}
                onKeyDown={onComposerKeyDown}
                onKeyUp={(e) => {
                  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
                    syncMention(comment, e.currentTarget.selectionStart);
                  }
                }}
                onClick={(e) => syncMention(comment, e.currentTarget.selectionStart)}
                onBlur={() => {
                  if (blurTimerRef.current != null) {
                    window.clearTimeout(blurTimerRef.current);
                  }
                  blurTimerRef.current = window.setTimeout(() => setMention(null), 150);
                }}
                placeholder="输入消息；输入 @ 选择角色（界面显示昵称，后台用 slug 激活）"
              />
              {mention && mentionCandidates.length > 0 && (
                <div className="mention-menu" role="listbox" aria-label="选择要 @ 的角色">
                  {mentionCandidates.map((c, i) => (
                    <button
                      key={c.slug}
                      type="button"
                      role="option"
                      aria-selected={i === mentionIndex}
                      className={`mention-item ${i === mentionIndex ? "active" : ""}`}
                      onMouseDown={(e) => e.preventDefault()}
                      onMouseEnter={() => setMentionIndex(i)}
                      onClick={() => chooseMention(c)}
                    >
                      <span className="mention-item-name">
                        {c.name !== c.slug ? `${c.name}（${c.slug}）` : c.slug}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button className="primary" onClick={send} disabled={busy || !comment.trim()}>发送</button>
          </div>
          <p className="muted hint" style={{ margin: "4px 2px 0" }}>
            提示：输入 <strong>@</strong> 会弹出角色选择（上下键选择，Enter 确认）；选中后按 Enter 发送。
            @角色 消息会按「@提及」发送——时间线标黄并唤醒该角色。
          </p>
        </div>

        {/* 右：任务信息面板 */}
        <div
          className={`splitter ${panel.dragging ? "dragging" : ""}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整任务信息面板宽度"
          aria-valuemin={300}
          aria-valuemax={560}
          aria-valuenow={panel.width}
          tabIndex={0}
          onPointerDown={panel.onPointerDown}
          onKeyDown={panel.onKeyDown}
          onDoubleClick={panel.reset}
        />
        <aside className="task-side">
          <div className="pane-head">
            <h2 className="pane-title">任务档案</h2>
            <button
              type="button"
              className="icon-btn"
              onClick={panel.toggle}
              aria-label="折叠任务信息"
              title="折叠任务信息"
            >
              <Icon name="panel-right" />
            </button>
          </div>
          <dl className="task-meta">
            <dt>状态</dt>
            <dd><span className={`status status-${task.status}`}>{STATUS_LABEL[task.status]}</span></dd>
            <dt>领队</dt>
            <dd>
              {task.assignee
                ? renderRoleLive(roleLiveFor(task.assignee))
                : (task.assignees?.length ? "待认领" : "未指派")}
            </dd>
            <dt>类型</dt><dd>{task.type ?? "—"}</dd>
            <dt>标签</dt><dd>{task.tags?.length ? task.tags.join(", ") : "—"}</dd>
            <dt>ID</dt><dd className="mono">{task.id}</dd>
          </dl>

          <KnowledgeRepoPanel task={task} onChanged={refresh} />

          {task.description && (
            <>
              <h3 className="side-h">描述</h3>
              <div ref={descRef} className={`side-desc ${descOpen ? "expanded" : ""}`}>
                {task.description}
              </div>
              {(descOpen || descOverflow) && (
                <button type="button" className="small side-desc-toggle"
                  onClick={() => setDescOpen((v) => !v)}>
                  {descOpen ? "收起描述" : "展开全文"}
                </button>
              )}
            </>
          )}

          <h3 className="side-h">参与角色</h3>
          <ul className="side-list side-roles">
            {liveSlugs.length === 0 && <li className="muted">（未指派角色）</li>}
            {liveSlugs.map((slug) => {
              const live = roleLiveFor(slug);
              return (
                <li key={slug} className="side-role">
                  {renderRoleLive(live)}
                  <div className="side-role-detail muted">{live.detail}</div>
                  {live.claim?.summary && (
                    <div className="side-role-summary muted">{live.claim.summary}</div>
                  )}
                </li>
              );
            })}
          </ul>

          <h3 className="side-h">验收标准</h3>
          <ul className="side-list">
            {(task.acceptance ?? []).length === 0 && <li className="muted">（无）</li>}
            {(task.acceptance ?? []).map((a, i) => <li key={i}>{a}</li>)}
          </ul>

          <h3 className="side-h">作用域 scope</h3>
          <ul className="side-list">
            {(task.scope_paths ?? []).length === 0 && <li className="muted">（未限制）</li>}
            {(task.scope_paths ?? []).map((p, i) => <li key={i} className="mono">{p}</li>)}
          </ul>
        </aside>
      </div>
      )}

      {tab === "workflow" && (
        <div className={`workflow-view ${wfView === "graph" ? "is-graph" : "is-list"}`}>
          <div className="wf-template-actions">
            <button className="small" disabled={busy || task.status === "done" || task.status === "closed"}
              onClick={() => setShowApplyTemplate(true)}>套用模板</button>
            {workflow && (
              <button className="small" disabled={busy}
                onClick={async () => {
                  setBusy(true); setError(null);
                  try {
                    await api.importWorkflowTemplate("workflow", workflow.id, task.title);
                    window.location.href = "/templates";
                  } catch (e) { setError(String(e)); setBusy(false); }
                }}>保存为模板</button>
            )}
            <span className="wf-template-actions-divider" />
            <span className="muted">规划种子：{task.plan_seed?.name || "无"}</span>
            <select value={seedId} onChange={(e) => setSeedId(e.target.value)} disabled={!seedTemplates.length}>
              {seedTemplates.length === 0 && <option value="">暂无可用模板</option>}
              {seedTemplates.map((item) => (
                <option key={item.id} value={item.id}>{item.name} · v{item.published_version}</option>
              ))}
            </select>
            <button className="small" disabled={busy || !seedId}
              onClick={() => act(async () => {
                const item = seedTemplates.find((t) => t.id === seedId);
                if (!item?.published_version) return;
                await api.setPlanSeed(id, item.id, item.published_version);
              })}>设为种子</button>
            {task.plan_seed && (
              <button className="small" disabled={busy}
                onClick={() => act(() => api.clearPlanSeed(id))}>清除种子</button>
            )}
          </div>
          {!workflow && (
            <p className="muted">还没有工作流。多角色任务派发后，Cindy 会生成步骤状态机并在此展示；也可以直接套用已发布模板。</p>
          )}
          {workflow && (
            <>
              <WorkflowSummary workflow={workflow} taskStatus={task.status} />
              <div className="role-picker" style={{ margin: "4px 0 10px" }}>
                <button className={`small ${wfView === "graph" ? "primary" : ""}`}
                  onClick={() => setWfView("graph")}>图谱</button>
                <button className={`small ${wfView === "list" ? "primary" : ""}`}
                  onClick={() => setWfView("list")}>列表</button>
              </div>
              {wfView === "graph" ? (
                <WorkflowGraph
                  workflow={workflow}
                  roles={roles}
                  busy={busy}
                  onReview={(step, approve) => act(async () => {
                    const w = await api.reviewWorkflowStep(
                      workflow.id, step.step_id, approve,
                      approve ? undefined : "请按评审意见修改");
                    setWorkflow(w);
                  })}
                  onRetry={(step) => act(async () => {
                    setWorkflow(await api.retryWorkflowStep(workflow.id, step.step_id));
                  })}
                />
              ) : (
                (() => {
                  const loops = summarizeLoops(workflow.steps);
                  const loopBySource = new Map(loops.map((l) => [l.sourceStepId, l]));
                  const rendered = new Set<string>();
                  const rows = workflow.steps.flatMap((st) => {
                    if (isDynamicStep(st)) return [];
                    const row = renderStepRow(st);
                    const loop = loopBySource.get(st.step_id);
                    if (loop && !rendered.has(loop.sourceStepId)) {
                      rendered.add(loop.sourceStepId);
                      return [row, renderLoopList(loop)];
                    }
                    return [row];
                  });
                  for (const loop of loops) {
                    if (!rendered.has(loop.sourceStepId)) rows.push(renderLoopList(loop));
                  }
                  return <div className="wf-steps">{rows}</div>;
                })()
              )}
            </>
          )}
        </div>
      )}

      {tab === "subtasks" && (
        <div className="child-tasks">
          {children.length === 0 && (
            <p className="muted">暂无临时子任务。Agent 可用 `anvil task create --parent …` 创建，供其他角色认领。</p>
          )}
          {children.map((c) => (
            <a key={c.id} className="child-task" href={`/tasks/${c.id}`}>
              <span className="child-title">{c.title}</span>
              <span className={`status status-${c.status === "in_progress" ? "in_progress" : c.status === "in_review" ? "in_review" : c.status === "done" ? "done" : c.status === "closed" ? "closed" : "todo"}`}>
                {STATUS_LABEL[c.status]}
              </span>
              <span className="muted">{c.assignees?.length ? c.assignees.join("、") : "无人认领"}</span>
              <span className="muted mono">{c.id.slice(0, 8)}</span>
            </a>
          ))}
        </div>
      )}

      {tab === "runtime" && <RoleSessions task={task} roles={roles} workflow={workflow} />}
      </div>
      {showApplyTemplate && (
        <ApplyTemplateModal
          task={task}
          roles={roles}
          onClose={() => setShowApplyTemplate(false)}
          onApplied={async (applied) => {
            setShowApplyTemplate(false);
            setWorkflow(applied as unknown as Workflow);
            await refreshWorkflow();
          }}
        />
      )}
    </div>
  );
}
