import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, isOnboardingRole, type BoardDraft, type ClarificationState, type Delegation, type FindingSummary, type Role, type Run, type Task, type TaskChild, type TaskDispatch, type TaskLeases, type TaskRoleState, type TimelineEvent, type Workflow, type WorkflowGraphResponse, type WorkflowHistoryItem, type WorkflowMetrics, type WorkflowStep, type WorkflowTemplate } from "../api/client";
import { useLiveRefresh } from "../useLiveRefresh";
import { UNBOUND_CHANNEL_VIEW } from "../channelView";
import { RoleSessions } from "./RoleSessions";
import { WorkflowGraph } from "./WorkflowGraph";
import { roleStatusFromServer, taskRoleState } from "../roleStatus";
import { formatClock, formatDateTime } from "../time";
import { useRoleRuntimes } from "../useRoleRuntimes";
import { usePanelLayout } from "../usePanelLayout";
import { STATUS_META, displayStepMeta, isDynamicStep, stepWaitingWake, summarizeLoops, type LoopSummary } from "../workflowGroups";
import { RoleAvatar } from "../components/RoleAvatar";
import { TaskHeader } from "../components/TaskHeader";
import { SubtasksPanel } from "../components/SubtasksPanel";
import { WorkflowSummary } from "../components/WorkflowSummary";
import { WorkflowWeightDialog } from "../components/WorkflowWeightDialog";
import { IntentBriefCard } from "../components/IntentBriefCard";
import { ClarifyQuestions, parseClarifyQuestions } from "../components/ClarifyQuestions";
import { DraftParticipants } from "../components/DraftParticipants";
import {
  ChannelKnowledgePanel, KNOWLEDGE_MODE_LABEL, KnowledgeRepoPanel,
  TaskKnowledgePanel,
} from "../components/TaskKnowledgePanels";
import {
  TASK_STATUS_META, runStatusLabel,
} from "../statusVocabulary";
import {
  childFromDelegation, childFromDispatch, childState,
} from "../components/ChildTaskCard";
import { WEIGHT_META } from "../components/WorkflowWeightDialog";
import { Icon } from "../components/Icon";
import { confirmDialog, promptDialog } from "../components/dialogs";
import { GateReviewDrawer } from "../components/GateReviewDrawer";
import { TaskFindingBoard } from "./TaskFindingBoard";
import { CheckJobsPanel } from "../components/CheckJobsPanel";

const stepAuthority = (st: WorkflowStep): "ai" | "human" | "joint" =>
  st.decision_authority
  || (st.gate ? "human" : st.kind === "decision" ? "ai" : "human");

function draftBlockerText(blockers: BoardDraft["role_suggestion_blockers"]): string {
  if (!blockers?.length) return "";
  return blockers.map((b) => {
    if (b.reason === "pending_role_suggestion") {
      return `先处理角色建议 ${b.draft_id}（${b.requested_assignees.join("、")}）`;
    }
    const missing = b.missing_roles?.length ? `未创建：${b.missing_roles.join("、")}` : "";
    const unbound = b.unbound_roles?.length ? `未绑定：${b.unbound_roles.join("、")}` : "";
    return `角色授权已批准，等待 ${b.planner || "planner"} 创建/绑定`
      + (missing || unbound ? `（${[missing, unbound].filter(Boolean).join("；")}）` : "");
  }).join("；");
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
      return { text: `运行状态：${runStatusLabel(String(p.status))}`, runStatus: String(p.status) };
    case "step": {
      const status = String(p.status ?? "");
      const label: Record<string, string> = {
        running: "开始执行",
        submitted: "已提交（等待人工审核）",
        planned: "切片计划已提交",
        approved: "已通过",
        rework: "决策打回，返工中",
        rework_exhausted: "重做次数耗尽，需人工介入",
        escalated: "升级人工决策",
        rejected: "人工驳回，返工中",
        failed: "执行失败",
      };
      const summary = p.summary ? `：${String(p.summary)}` : "";
      return {
        text: `工作流步骤 ${String(p.step ?? "")}「${String(p.title ?? "")}」`
          + `${label[status] ?? status}${summary}`,
      };
    }
    case "decision": {
      const decision = String(p.decision ?? "");
      const label: Record<string, string> = {
        approve: "通过",
        reject: "打回",
        escalate: "升级人工",
        recommend_approve: "建议通过",
        recommend_reject: "建议打回",
      };
      const targetList = p.rework_targets;
      const targets = Array.isArray(targetList) && targetList.length
        ? ` · 返工：${targetList.join("、")}` : "";
      const feedback = p.feedback ? `：${String(p.feedback)}` : "";
      return { text: `决策${label[decision] ?? decision}${feedback}${targets}` };
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
  { key: "decision", label: "决策" },
  { key: "run", label: "运行" },
  { key: "review", label: "评审" },
] as const;

type TimelineFilter = (typeof TL_FILTERS)[number]["key"];
const DEFAULT_TL_KIND: TimelineFilter = "dialog";


type TaskTab = "chat" | "workflow" | "findings" | "subtasks" | "runtime";

function readInitialTaskRoute(): { tab: TaskTab; finding: string | null } {
  const params = new URLSearchParams(window.location.search);
  const rawTab = params.get("tab");
  const tab: TaskTab = rawTab === "workflow" || rawTab === "findings"
    || rawTab === "subtasks" || rawTab === "runtime" ? rawTab : "chat";
  return { tab, finding: params.get("finding") };
}

export function TaskDetail({ id }: { id: string }) {
  const initialRoute = useRef(readInitialTaskRoute()).current;
  const [task, setTask] = useState<Task | null>(null);
  const [clarification, setClarification] = useState<ClarificationState | null>(null);
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
  const [tab, setTab] = useState<TaskTab>(initialRoute.tab);
  const [runtimeFocus, setRuntimeFocus] = useState<{ role: string; sliceKey: string | null } | null>(null);
  const [findingSummary, setFindingSummary] = useState<FindingSummary | null>(null);
  const [tlKind, setTlKind] = useState<TimelineFilter>(DEFAULT_TL_KIND);
  const [tlLimit, setTlLimit] = useState(40);
  const threadRef = useRef<HTMLElement>(null);
  const pinnedRef = useRef(true);          // 用户是否停留在底部
  const initialScrollRef = useRef(true);   // 刚打开任务/切回会话时强制滚到底
  const autoScrollingRef = useRef(false);  // 程序化滚动期间忽略 onScroll 的“用户滚动”判定
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [workflowHistory, setWorkflowHistory] = useState<WorkflowHistoryItem[]>([]);
  const [viewingWorkflow, setViewingWorkflow] = useState<Workflow | null>(null);
  const [workflowLoaded, setWorkflowLoaded] = useState(false);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [workflowMetrics, setWorkflowMetrics] = useState<WorkflowMetrics | null>(null);
  const [graphDiff, setGraphDiff] = useState<WorkflowGraphResponse | null>(null);
  const [taskLeases, setTaskLeases] = useState<TaskLeases | null>(null);
  const workflowReqRef = useRef(0);
  const [reserveInput, setReserveInput] = useState("");
  const [showWeight, setShowWeight] = useState(false);
  const [dossierBusy, setDossierBusy] = useState(false);
  const [reviewStep, setReviewStep] = useState<WorkflowStep | null>(null);
  const [delegations, setDelegations] = useState<Delegation[]>([]);
  const [dispatches, setDispatches] = useState<TaskDispatch[]>([]);
  const [children, setChildren] = useState<TaskChild[]>([]);
  const [plannerActive, setPlannerActive] = useState(false);
  const [planningActive, setPlanningActive] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [wfView, setWfView] = useState<"graph" | "list">("graph");
  const [seedTemplates, setSeedTemplates] = useState<WorkflowTemplate[]>([]);
  const [seedId, setSeedId] = useState("");
  const [roleStates, setRoleStates] = useState<Record<string, TaskRoleState>>({});
  const [busy, setBusy] = useState(false);
  const { slugs: liveSlugs, runtimes: roleRuntimes } = useRoleRuntimes(task);
  const panel = usePanelLayout({ storageKey: "task" });

  const displayWorkflow = viewingWorkflow ?? workflow;
  const isViewingHistory = Boolean(viewingWorkflow);

  const refreshWorkflow = () => {
    const req = ++workflowReqRef.current;
    const current = api.getTaskWorkflow(id)
      .then((wf) => {
        if (req !== workflowReqRef.current) return;
        setWorkflow(wf);
        setWorkflowLoaded(true);
        setWorkflowError(null);
      })
      .catch((e) => {
        if (req !== workflowReqRef.current) return;
        setWorkflowLoaded(true);
        const message = String(e);
        if (message.includes("404")) {
          setWorkflow(null);
          setWorkflowError(null);
        } else {
          setWorkflowError(message);
        }
      });
    return Promise.all([
      current,
      api.listTaskWorkflows(id).then((items) => {
        if (req === workflowReqRef.current) setWorkflowHistory(items);
      }).catch(() => {
        if (req === workflowReqRef.current) setWorkflowHistory([]);
      }),
      api.listTaskLeases(id).then((leases) => {
        if (req === workflowReqRef.current) setTaskLeases(leases);
      }).catch(() => {
        if (req === workflowReqRef.current) setTaskLeases(null);
      }),
    ]);
  };

  useEffect(() => {
    api.listTaskLeases(id).then(setTaskLeases).catch(() => setTaskLeases(null));
  }, [id]);

  useEffect(() => {
    if (!displayWorkflow) {
      setWorkflowMetrics(null);
      setGraphDiff(null);
      return;
    }
    let alive = true;
    api.getWorkflowMetrics(displayWorkflow.id)
      .then((m) => alive && setWorkflowMetrics(m))
      .catch(() => alive && setWorkflowMetrics(null));
    api.getWorkflowGraph(displayWorkflow.id, "diff")
      .then((d) => alive && setGraphDiff(d))
      .catch(() => alive && setGraphDiff(null));
    return () => { alive = false; };
  }, [displayWorkflow?.id, displayWorkflow?.updated_at]);

  /** 统一子任务投影；旧后端无 /children 时回退到两个旧端点。 */
  const refreshChildren = () => {
    const apply = (items: TaskChild[]) => {
      setChildren(items);
      setDelegations(items
        .filter((c) => c.relation_type === "delegation")
        .map((c) => c.raw as Delegation));
      setDispatches(items
        .filter((c) => c.relation_type === "dispatch")
        .map((c) => c.raw as TaskDispatch));
    };
    return api.listTaskChildrenUnified(id)
      .then((res) => apply(res.items))
      .catch(() => Promise.all([
        api.listTaskDelegations(id).catch(() => []),
        api.listDispatches(id).catch(() => []),
      ]).then(([delegationRows, dispatchRows]) => {
        setDelegations(delegationRows);
        setDispatches(dispatchRows);
        setChildren([
          ...delegationRows.map(childFromDelegation),
          ...dispatchRows.map(childFromDispatch),
        ]);
      }));
  };

  const refreshDrafts = () =>
    api.listDrafts(id).then(setDrafts).catch(() => setDrafts([]));

  const refreshRoleStates = () =>
    api.getTaskRoleStates(id).then((res) => {
      setRoleStates(Object.fromEntries(res.roles.map((r) => [r.role_slug, r])));
    }).catch(() => setRoleStates({}));

  const refresh = () =>
    Promise.all([api.getTask(id), api.getTaskTimeline(id), api.listTaskRuns(id),
      api.getClarification(id).catch(() => null)])
      .then(([t, tl, runs, clar]) => {
        setTask(t);
        setTimeline(tl);
        setRuns(runs);
        setClarification(clar);
        const active = new Set([
          "created", "projecting", "running", "submitted", "reviewing", "suspended",
        ]);
        const onboarding = new Set(roles.filter(isOnboardingRole).map((r) => r.slug));
        setPlannerActive(runs.some((r) => active.has(r.status)
          && (r.role_slug === t.planner || r.role_slug === "cindy"
            || onboarding.has(r.role_slug))));
        // suspended 的 planner run 通常已经产出草案、在等人工审批；
        // 不能继续把简报卡显示成「Cindy 正在规划」。
        const planningNow = new Set(["created", "projecting", "running"]);
        setPlanningActive(runs.some((r) => planningNow.has(r.status)
          && r.origin !== "clarify"
          && (r.role_slug === t.planner || r.role_slug === "cindy"
            || onboarding.has(r.role_slug))));
        setGone(false);
        if (t.issue_board_enabled) {
          api.getFindingSummary(id).then(setFindingSummary)
            .catch(() => setFindingSummary(null));
        } else {
          setFindingSummary(null);
        }
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
    // 切换任务时丢弃上一个任务的 workflow 状态和未完成的旧请求。
    workflowReqRef.current += 1;
    setWorkflow(null);
    setViewingWorkflow(null);
    setWorkflowHistory([]);
    setWorkflowLoaded(false);
    setWorkflowError(null);
    setDescOpen(false);  // 切换任务时收起描述
    setDescOverflow(false);
    refresh();
    refreshWorkflow();
    refreshChildren();
    refreshRoleStates();
    api.listRoles({ includeDeleted: true }).then(setRoles).catch(() => {});
    api.listWorkflowTemplates(undefined, "active").then((list) => {
      setSeedTemplates(list.filter((item) => item.published_version
        && (item.use_mode === "seed" || item.use_mode === "both")));
    }).catch(() => setSeedTemplates([]));
    refreshDrafts();
  }, [id, gone]);

  useLiveRefresh(
    () => Promise.all([
      refresh(), refreshWorkflow(), refreshChildren(),
      refreshDrafts(), refreshRoleStates(),
    ]),
    {
      enabled: !gone,
      initial: false,
      match: (ev) => ev.topic === "tick"
        || (ev.topic === "task" && ev.payload.id === id),
      tickMinGapMs: 10_000,
      pollMs: 45_000,
    },
  );

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

  const selectTab = (next: TaskTab) => {
    setTab(next);
    if (next === "workflow") void refreshWorkflow();
    try {
      const url = new URL(window.location.href);
      if (next === "chat") url.searchParams.delete("tab");
      else url.searchParams.set("tab", next);
      url.searchParams.delete("finding");
      window.history.replaceState({}, "", url);
    } catch {
      // 地址栏同步失败不影响页签切换
    }
  };

  // 问题板被关闭或任务未启用时，深链到 findings 的回退。
  useEffect(() => {
    if (tab === "findings" && task && !task.issue_board_enabled) setTab("chat");
  }, [tab, task?.issue_board_enabled]);

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
  // 依赖 tab/panel.collapsed：侧栏在非会话页签或折叠时不在 DOM/不可见，
  // 仅在 description 变化时测量会漏掉“之后切回会话页签”的场景。
  useEffect(() => {
    if (tab !== "chat" || !task?.description) {
      if (!task?.description) setDescOverflow(false);
      return;
    }
    const el = descRef.current;
    if (!el) return;
    let frame = 0;
    let alive = true;
    const check = () => {
      const node = descRef.current;
      if (!node) return;
      // 侧栏折叠/尚未布局时保持上一次结果，避免把“看不见”误判成“没有溢出”。
      if (node.clientHeight === 0 && node.scrollHeight === 0) return;
      setDescOverflow(node.scrollHeight > node.clientHeight + 1);
    };
    check();
    // 字体加载/首帧布局可能晚于 effect，再补测一次。
    frame = window.requestAnimationFrame(check);
    document.fonts?.ready.then(() => { if (alive) check(); }).catch(() => {});
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => {
      alive = false;
      window.cancelAnimationFrame(frame);
      ro.disconnect();
    };
  }, [task?.description, descOpen, tab, panel.collapsed]);

  const filteredTimeline = timeline.filter((ev) => {
    if (tlKind === "all") return true;
    if (tlKind === "dialog") return ev.kind === "comment" || ev.kind === "mention";
    return ev.kind === tlKind;
  });
  const visibleTimeline = filteredTimeline.slice(-tlLimit);
  // 澄清问题卡是历史消息：刷新/重进后不能仅靠组件内 sent 状态锁定。
  // 一条 Cindy 问题消息之后出现人类回复，视为该问题已回答；之后的新问题不受影响。
  const answeredClarifyKeys = useMemo(() => {
    const answered = new Set<string>();
    const pending: string[] = [];
    const planner = task?.planner || "cindy";
    for (const ev of timeline) {
      const payload = ev.payload as Record<string, unknown>;
      const isCindy = ev.role === "cindy" || ev.role === planner;
      if (ev.kind === "mention" && isCindy) {
        const parsed = parseClarifyQuestions(String(payload.message ?? ""));
        if (parsed.questions.length > 0) pending.push(ev.key);
        continue;
      }
      const target = String(payload.target ?? "");
      const isHumanReply = ev.role === "human" && ev.kind === "mention"
        && (target === planner || target === "cindy");
      if (isHumanReply) {
        for (const key of pending) answered.add(key);
        pending.length = 0;
      }
    }
    return answered;
  }, [timeline, task?.planner]);
  // 后端已把 waiting_for_human 置真（mention 已落库），但问题消息要等下一次刷新
  // 才进入 timeline。这段空窗期继续显示沙漏，避免误以为 Cindy 已说完、轮到自己输入。
  const cindyMentionVisible = useMemo(() => {
    const planner = task?.planner || "cindy";
    let lastHuman = -1;
    timeline.forEach((ev, index) => {
      if (ev.role === "human") lastHuman = index;
    });
    return timeline.slice(lastHuman + 1).some((ev) => ev.kind === "mention"
      && (ev.role === "cindy" || ev.role === planner));
  }, [task?.planner, timeline]);
  const awaitingQuestionMessage = Boolean(
    clarification?.status === "in_progress"
    && clarification.waiting_for_human
    && (clarification.brief?.open_questions?.length ?? 0) > 0
    && !cindyMentionVisible);
  const cindyPending = Boolean(clarification?.thinking) || awaitingQuestionMessage;

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
  const relatedTaskIds = [
    task.id,
    ...delegations.map((d) => d.id),
    ...dispatches.map((d) => d.target_task_id),
  ];
  const roleLiveFor = (slug: string) => {
    const rt = roleRuntimes[slug];
    const ctxList = (rt?.contexts ?? []).filter((c) => c.task_id === task.id);
    const ctxHere = ctxList.find(
      (c) => c.context_key && rt?.context_key && c.context_key === rt.context_key,
    ) ?? ctxList.find(
      (c) => (c.slice_key ?? null) === (rt?.context_slice_key ?? null),
    ) ?? ctxList[0];
    const wfSlice = ctxList.length
      ? (ctxHere?.slice_key ?? rt?.context_slice_key ?? null)
      : undefined;
    // 优先展示角色此刻真正执行的 run；排队/尚未投递的新 run 只有启动头。
    const currentRun = rt?.current_run_id && rt.current_task_id === task.id
      ? runs.find((r) => r.id === rt.current_run_id)
      : undefined;
    const latestRun = currentRun
      ?? runs
        .filter((r) => r.role_slug === slug
          && (ctxList.length === 0
            || (r.slice_key ?? null) === (wfSlice ?? null)))
        .sort((a, b) => String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")))[0]
      ?? runs
        .filter((r) => r.role_slug === slug)
        .sort((a, b) => String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")))[0];
    const returnPending = dispatches.some(
      (d) => d.target_role === slug
        && (d.status === "return_ready" || d.status === "returning"));
    const serverState = roleStates[slug];
    let state = serverState
      ? roleStatusFromServer({
          ...serverState,
          currentStepId: rt?.current_step_id,
        })
      : taskRoleState({
          task,
          role: slug,
          runtime: rt,
          workflow,
          relatedTaskIds,
          sliceKey: wfSlice,
          runStatus: latestRun?.status ?? null,
          returnPending,
        });
    if (!rt) {
      state = {
        ...state, key: "loading", label: "加载中", tone: "gray",
        detail: "状态加载中…", title: "加载中 · 状态加载中…",
      };
    } else if (rt.switching && (state.workingHere || state.queued > 0)) {
      state = {
        ...state, key: "switching", label: "切换上下文", tone: "orange",
        detail: "正在切换到本任务…", title: "切换上下文 · 正在切换到本任务…",
      };
    }
    const display = roles.find((r) => r.slug === slug)?.display_name ?? slug;
    let detail = state.detail;
    if (state.key === "working" && ctxHere?.slice_key) {
      detail += ` · 切片 ${ctxHere.slice_key}`;
    } else if (state.key === "queued" && state.queued > 0) {
      detail += ` · 本任务/子任务待处理 ${state.queued} 条`;
    }
    return {
      slug, rt, claim: (task.role_claims ?? []).find((c) => c.role_slug === slug),
      state, queued: state.queued, detail, title: `${display}：${state.title}`,
    };
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
        tone={live.state.tone}
      />
      <i className={`role-live-dot ${live.state.tone}`} aria-label={live.title} />
      <span className={`status tone-${live.state.tone} role-live-state`}>
        {live.state.label}
      </span>
      {(live.rt?.active_subagents?.length ?? 0) > 0 && (
        <span className="chip" title="运行时原生子代理，不是 Anvil 任务">
          子代理 {live.rt?.active_subagents?.length}
        </span>
      )}
      <span className="role-live-name">{live.slug}</span>
      {live.queued > 0 && (
        <span className="role-live-queue" title={`本任务有 ${live.queued} 条消息待处理`}>
          待处理 {live.queued}
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

  const assignableRoles = roles.filter((r) => !r.deleted && !isOnboardingRole(r));

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
    .filter((r): r is Role => r !== undefined && !r.deleted)
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
    const clarifying = clarification?.status === "in_progress";
    await act(async () => {
      if (targets.length) {
        for (const target of targets) {
          await api.mentionTask(id, target, text);
        }
      } else if (clarifying) {
        // 澄清期间不强制用户手动 @：直接回复就是给 Cindy 的答案，进入下一轮。
        await api.mentionTask(id, task?.planner || "cindy", text);
      } else {
        await api.commentTask(id, text);
      }
      setComment("");
    });
    // 发送后把自己刚发的消息带进视野
    pinnedRef.current = true;
    scrollToBottom("smooth");
  };

  const suspendedStepIds = new Set(
    (isViewingHistory ? [] : (workflow?.steps ?? []))
      .filter((s) => s.status === "running" && s.run_id
        && runs.some((r) => r.id === s.run_id && r.status === "suspended"))
      .map((s) => s.step_id),
  );

  const isReviewable = (st: WorkflowStep) =>
    !isViewingHistory && (
      (st.gate && st.status === "submitted")
      || (st.kind === "decision"
        && (st.status === "submitted" || st.status === "running")));

  const renderStepRow = (st: WorkflowStep) => {
    const meta = displayStepMeta(st, suspendedStepIds);
    const tone = meta.tone;
    const label = meta.label;
    const stepChildren = children.filter((c) => c.parent_step_id === st.step_id);
    // 门禁只看委派：派遣是非阻塞软关联，不参与父 step 收口。
    const stepDelegations = stepChildren.filter((c) => c.relation_type === "delegation");
    const stepDispatches = stepChildren.filter((c) => c.relation_type === "dispatch");
    const openDelegations = stepDelegations.filter(
      (d) => d.status !== "done" && d.status !== "closed");
    const reviewPending = openDelegations.some((d) => d.status === "in_review");
    return (
      <div key={st.step_id} className="wf-step">
        <span className="wf-id mono">{st.step_id}</span>
        <div className="wf-main">
          <div className="wf-title">
            {st.title}
            {st.subagent_hint && (
              <span className="wf-subagent-mark"
                title="该步骤允许使用运行时只读子代理（不独立验收）">
                <Icon name="network" size={12} />
              </span>
            )}
            <span className="wf-role">{st.role_slug}</span>
            {st.reviewer && <span className="wf-role">验证：{st.reviewer}</span>}
            {st.gate && <span className="badge wf-gate">审核门</span>}
            {st.kind === "decision" && (
              <span className={`badge wf-gate ${stepAuthority(st) === "joint" ? "is-joint" : ""}`}>
                {stepAuthority(st) === "human" ? "人工决策"
                  : stepAuthority(st) === "joint" ? "联合决策" : "AI 决策"}
              </span>
            )}
            {st.risk_level && st.risk_level !== "normal" && (
              <span className="badge gate-stale">风险 {st.risk_level}</span>
            )}
            {stepDelegations.length > 0 && (
              <span className="badge wf-gate" title="本步骤的委派工单">
                委派 {stepDelegations.length}
              </span>
            )}
            {stepDispatches.length > 0 && (
              <span className="badge wf-gate" title="本步骤发起的派遣任务（不阻塞）">
                派遣 {stepDispatches.length}
              </span>
            )}
            {st.status === "running" && openDelegations.length > 0 && (
              <span className="badge wf-gate"
                title={reviewPending
                  ? "委派已提交，等待本步骤角色验收"
                  : "父步骤休眠等待委派负责人完成；完成后会自动唤醒"}>
                {reviewPending ? "待验收委派" : "等待委派"} {openDelegations.length}
              </span>
            )}
            <span className={`status tone-${tone}`}>{label}</span>
          </div>
          {st.depends_on?.length > 0 && <div className="muted wf-deps">依赖：{st.depends_on.join("、")}</div>}
          {st.contract && <div className="muted wf-deps">契约：{st.contract}</div>}
          {st.failure_reason && <div className="muted wf-deps">失败归因：{st.failure_reason}</div>}
          {st.lease_holder && (
            <div className="muted wf-deps">
              执行租约：{st.lease_holder}
              {st.lease_expires_at ? ` · 到期 ${formatDateTime(st.lease_expires_at)}` : ""}
            </div>
          )}
          {st.summary && <div className="muted wf-summary">{st.summary}</div>}
          {stepChildren.length > 0 && (
            <div className="wf-delegations">
              {stepChildren.map((c) => {
                const s = childState(c);
                return (
                  <a key={`${c.relation_type}:${c.task_id}`} href={`/tasks/${c.task_id}`}
                    className="muted">
                    {c.relation_type === "dispatch" && <span className="chip">派遣</span>}
                    {c.title} <span className={`status status-${s.cls}`}>{s.label}</span>
                  </a>
                );
              })}
            </div>
          )}
        </div>
        <div className="wf-actions">
          {st.run_id && <a href={`/runs/${st.run_id}`}>会话</a>}
          {isReviewable(st) && (
            <button className="small primary" onClick={() => setReviewStep(st)}>
              {st.kind === "decision" ? "人工决策" : "审查"}
            </button>
          )}
          {!isViewingHistory && (st.status === "failed" || st.status === "rejected") && (
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
    const meta = stepWaitingWake(loop.currentStep, suspendedStepIds)
      ? { label: "等待唤醒", tone: "gray" as const }
      : { label: STATUS_META[loop.status].label, tone: STATUS_META[loop.status].tone };
    return (
      <details key={`loop-${loop.sourceStepId}`} className="wf-loop-list">
        <summary className="wf-loop-summary">
          <span className="wf-loop-mark"><Icon name="refresh" size={12} /> 切片循环</span>
          <span className={`status tone-${meta.tone}`}>{meta.label}</span>
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
                <i className="wf-slice-dot" style={{
                  background: stepWaitingWake(g.active, suspendedStepIds)
                    ? "var(--status-gray)" : STATUS_META[g.status].color,
                }} />
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
          <a className="small button-like replay-link" href={`/tasks/${id}/replay`}
            title="打开任务回放">
            <Icon name="play" size={14} /> 任务回放
          </a>
          {(task.status === "done" || task.status === "closed") && (
            task.archived_at ? (
              <button className="small" disabled={busy}
                onClick={() => act(() => api.unarchiveTask(id))}>取消归档</button>
            ) : (
              <button className="small" disabled={busy}
                onClick={() => act(() => api.archiveTask(id))}>归档任务</button>
            )
          )}
          {["todo", "in_progress", "in_review"].includes(task.status) && (
            <button
              className="small danger"
              disabled={busy}
              onClick={() => {
                void confirmDialog({
                  title: "人工终止任务？",
                  message: "任务会进入「已终止」状态，可重新打开；运行中的进程与会话会保留。",
                  confirmLabel: "终止任务",
                  danger: true,
                }).then((ok) => {
                  if (ok) void act(() => api.cancelTask(id));
                });
              }}
            >人工终止任务</button>
          )}
          <button
            className="small danger"
            disabled={busy}
            onClick={async () => {
              const ok = await confirmDialog({
                title: "删除任务？",
                message: "会同时删除它的 runs、评论和依赖，且不可恢复。",
                confirmLabel: "删除任务",
                danger: true,
              });
              if (!ok) return;
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
      <TaskHeader task={task} />

      {/* 待评审横幅（HITL 醒目提示） */}
      {task.status === "in_review" && (() => {
        const boardPending = (findingSummary?.counts.submitted || 0)
          + (findingSummary?.counts.changes_requested || 0);
        // 摘要加载失败时不锁死按钮：后端 review 仍会硬校验问题板。
        const boardUnknown = Boolean(task.issue_board_enabled && !findingSummary);
        const boardReady = !task.issue_board_enabled || boardUnknown
          || Boolean(findingSummary?.board.status === "completed" && findingSummary?.all_decided);
        return (
          <div className="review-banner">
            <span className="review-banner-text">
              <Icon name="alert" />
              {boardUnknown
                ? "问题板状态加载中 —— 通过时后端仍会校验问题板"
                : task.issue_board_enabled && !boardReady
                  ? (findingSummary?.board.status !== "completed"
                    ? "问题板未完成 —— 审核者需完成问题板后，才能最终评审"
                    : `问题板还有 ${boardPending} 条 finding 待你逐条确认真实/误报`)
                  : "待人工评审 —— 请确认产出后「通过」或「驳回」"}
            </span>
            {task.issue_board_enabled && (
              <button className="small" onClick={() => selectTab("findings")}>
                打开问题板
              </button>
            )}
            <button className="primary"
              disabled={busy || !boardReady}
              title={!boardReady ? "请先完成问题板并逐条确认所有 finding" : undefined}
              onClick={() => act(() => api.reviewTask(id, true))}>通过</button>
            <button className="danger" onClick={() => act(() => api.reviewTask(id, false))} disabled={busy}>驳回</button>
          </div>
        );
      })()}

      <div className="detail-tabs">
        <button className={`tab ${tab === "chat" ? "active" : ""}`} onClick={() => selectTab("chat")}>
          会话 {timeline.length > 0 && <span className="tab-count">{timeline.length}</span>}
        </button>
        <button className={`tab ${tab === "workflow" ? "active" : ""}`} onClick={() => selectTab("workflow")}>
          工作流 {workflow && <span className="tab-count">{workflow.steps.filter((s) => s.status === "approved").length}/{workflow.steps.length}</span>}
        </button>
        {task.issue_board_enabled && (
          <button className={`tab ${tab === "findings" ? "active" : ""}`} onClick={() => selectTab("findings")}>
            问题板
            {findingSummary && findingSummary.pending > 0 && (
              <span className="tab-count tab-count-alert">{findingSummary.pending}</span>
            )}
          </button>
        )}
        <button className={`tab ${tab === "subtasks" ? "active" : ""}`} onClick={() => selectTab("subtasks")}>
          委派/派遣任务 {(children.length) > 0 && (
            <span className="tab-count">{children.length}</span>
          )}
        </button>
        <button className={`tab ${tab === "runtime" ? "active" : ""}`} onClick={() => selectTab("runtime")}>
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

      <div className={`task-tab-body ${tab === "chat" ? "is-chat" : ""} ${tab === "workflow" ? "is-workflow" : ""} ${tab === "findings" ? "is-findings" : ""}`}>
      {tab === "findings" && task.issue_board_enabled && (
        <TaskFindingBoard
          taskId={id}
          onChanged={() => { void refresh(); }}
          initialFindingId={initialRoute.finding}
        />
      )}
      {tab === "chat" && (
      <>
      <IntentBriefCard taskId={id} state={clarification}
        planningActive={planningActive}
        draftPending={drafts.some((d) => d.status === "pending")}
        hasWorkflow={Boolean(workflow)}
        pendingQuestions={awaitingQuestionMessage}
        onChanged={() => { void refresh(); }} />
      <div
        className={`task-layout ${panel.collapsed ? "panel-collapsed" : ""}`}
        style={panel.style}
      >
        {/* 左：会话流 */}
        <div className="task-main">
          <div className="session-actions">
            {task.status === "todo" && !task.plan_first && !workflow
              && !drafts.some((d) => d.status === "pending") && (task.assignees?.length ?? 0) === 0
              && !plannerActive && (
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
            {task.status === "todo" && clarification?.status !== "in_progress"
              && clarification?.status !== "stale" && planningActive && (
              <span className="muted">Cindy 正在规划工作流，请到收件箱处理草案。</span>
            )}
            {(task.status === "todo" || task.status === "in_progress")
              && !(workflow && workflow.status === "running")
              && clarification?.status !== "in_progress"
              && clarification?.status !== "stale"
              && !planningActive && !drafts.some((d) => d.status === "pending") && (
              <button
                className="primary"
                disabled={busy}
                title="0 角色时 Cindy 会先分析并建议角色；plan_first 或多角色任务先由 planner/Cindy 澄清需求并规划工作流；单角色任务若无标签或知识仓库匹配较弱，也会先由 Cindy 识别知识仓库。草案经你批准后执行。这是启动/规划入口，不是派遣子任务。"
                onClick={() => act(async () => {
                  const res = await api.dispatchTask(id);
                  setDispatchNote(
                    res.mode === "clarifying"
                      ? null
                      : res.mode === "planning"
                        ? (task.assignees?.length
                            ? `规划助理 ${res.planner} 正在分析任务并设计工作流…草案将出现在收件箱，批准后自动启动步骤执行。`
                            : `规划助理 ${res.planner} 正在分析任务与角色缺口；若角色不足，会先提交角色建议等待你审批。`)
                        : res.mode === "pending_review"
                          ? (res.draft_kind === "role_suggestion"
                              ? "已有待审角色建议，请到收件箱审阅批准。"
                              : "已有待审工作流草案，请到收件箱审阅批准。")
                          : "已直接启动参与角色执行。");
                })}
              >{clarification?.status === "none" && clarification?.can_start
                  ? "启动（Cindy 先澄清）"
                  : clarification?.status === "confirmed"
                    ? "启动（Cindy 规划）"
                    : clarification?.status === "skipped"
                      ? "启动（直接规划）"
                      : (task.assignees?.length ?? 0) === 0
                        ? "启动（Cindy 分析并建议角色）"
                        : (task.assignees?.length ?? 0) >= 2
                          ? "启动（先由 Cindy 规划）"
                          : task.plan_first
                            ? "启动（先规划工作流）"
                            : (!task.knowledge_binding_locked
                                && ["needs_cindy", "suggested", "fallback"].includes(
                                  task.knowledge_binding_state ?? ""))
                              ? "启动（Cindy 识别知识仓库）"
                              : "启动并执行"}</button>
            )}
            {task.status === "in_progress" && (
              <>
                <button onClick={() => act(() => api.unclaimTask(id))} disabled={busy}>放弃</button>
                <button className="primary" disabled={busy} onClick={() => act(async () => {
                  if (workflow?.status === "running") {
                    const ok = await confirmDialog({
                      title: "终止当前 workflow 并提交评审？",
                      message: "当前工作流仍在执行。提交评审会先终止它，"
                        + "正在跑的步骤和 run 会被放弃。",
                      confirmLabel: "终止并提交",
                      danger: true,
                    });
                    if (!ok) return;
                    await api.submitTask(id, { force_terminate_workflow: true });
                    return;
                  }
                  await api.submitTask(id);
                })}>
                  {workflow?.status === "running"
                    ? "终止 workflow 并提交评审"
                    : task.assignees?.length > 0 ? "人工收口并提交评审" : "提交评审"}
                </button>
              </>
            )}
            {task.status === "done" && <span className="muted">已完成。</span>}
            {task.status === "closed" && (
              <button onClick={() => act(() => api.reopenTask(id))} disabled={busy}>重新打开</button>
            )}
          </div>

          {dispatchNote && <p className="muted">{dispatchNote}</p>}

          {drafts.filter((d) => d.status === "pending" && d.kind === "role_suggestion").map((d) => {
            const mode = d.tree?.apply_mode === "authorize" ? "authorize" : "create_bind";
            const roles = d.tree?.requested_assignees?.length
              ? d.tree.requested_assignees
              : (d.tree?.suggested_roles || []).map((r) => r.slug);
            return (
              <p className="muted" key={d.id}>
                {mode === "authorize" ? "角色追加授权待审批" : "角色建议待审批"}
                （{roles.join("、") || "无角色"}）。
                {mode === "authorize"
                  ? "批准后由规划者创建/绑定；角色未全部创建并绑定前，工作流不能批准。"
                  : "批准会创建/绑定角色；完成后才能批准工作流。"}
                {" "}<a href="/inbox">去收件箱处理</a>。
              </p>
            );
          })}
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
                  {d.kind === "workflow" && <DraftParticipants draftId={d.id} />}
                  {d.kind === "workflow_revision" && d.tree?.impact && (
                    <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                      <strong>{d.tree.impact.classification}</strong>
                      {" · "}{d.tree.impact.summary}
                      {(d.tree.impact.blocked_by?.length ?? 0) > 0 && (
                        <> · 阻塞：{d.tree.impact.blocked_by!
                          .map((b) => `${b.step_id || "?"}(${b.status || "?"})`).join("、")}</>
                      )}
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
                  {(d.role_suggestion_blockers?.length ?? 0) > 0 && (
                    <div className="inbox-blocker">
                      {draftBlockerText(d.role_suggestion_blockers)}
                    </div>
                  )}
                  <div className="card-actions">
                    {d.kind === "workflow" && (
                      <a className="small button-like" href={`/templates?draft=${encodeURIComponent(d.id)}`}>
                        在模板工作台编辑
                      </a>
                    )}
                    <button
                      className="primary small"
                      disabled={busy || (d.role_suggestion_blockers?.length ?? 0) > 0}
                      title={(d.role_suggestion_blockers?.length ?? 0) > 0
                        ? draftBlockerText(d.role_suggestion_blockers) : undefined}
                      onClick={() => act(async () => {
                        if (d.kind === "workflow_revision") {
                          const rerunNote = d.tree?.impact?.classification === "safe_external"
                            ? "\n旁路节点完成后，目标步骤会等待其结果再继续或重跑。"
                            : "";
                          const ok = await confirmDialog({
                            title: "应用工作流增量修订？",
                            message: `${d.tree?.impact?.summary || ""}\n`
                              + "当前执行会继续，不会终止或从头重跑。"
                              + rerunNote,
                            confirmLabel: "应用修订",
                          });
                          if (!ok) return;
                          await api.approveDraft([d.id]);
                        } else if (d.kind === "workflow" && workflow?.status === "running") {
                          const ok = await confirmDialog({
                            title: "终止当前执行并重新开始？",
                            message: "任务当前有运行中的执行。批准后会终止当前执行并从头"
                              + "重新开始，旧执行保留为历史。",
                            confirmLabel: "终止并重跑",
                            danger: true,
                          });
                          if (!ok) return;
                          const reason = await promptDialog({
                            title: "重新执行原因",
                            message: "可留空；原因会写入任务会话。",
                            confirmLabel: "继续",
                            input: { placeholder: "为什么需要重新执行？" },
                          });
                          if (reason === null) return;
                          await api.approveDraft([d.id], {
                            restart_running: true,
                            restart_reason: reason.trim() || undefined,
                          });
                        } else {
                          await api.approveDraft([d.id]);
                        }
                        await Promise.all([
                          refreshWorkflow(),
                          api.listDrafts(id).then(setDrafts).catch(() => {}),
                        ]);
                      })}
                    >{(d.role_suggestion_blockers?.length ?? 0) > 0 ? "先处理角色建议"
                      : d.kind === "workflow_revision" ? "应用修订（不重启）"
                        : d.kind === "workflow" ? "批准并启动工作流"
                          : "批准并落板（步骤按依赖自动启动）"}</button>
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
                      title={f.key === "dialog" ? "默认只显示评论与 @提及" : undefined}
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
              <div className="muted empty">
                <div>
                  当前筛选（{TL_FILTERS.find((f) => f.key === tlKind)?.label ?? tlKind}）
                  下没有交互。
                </div>
                {tlKind === "dialog" && <div>默认只显示评论与 @提及。</div>}
                {tlKind !== "all" && timeline.length > 0 && (
                  <button className="small" style={{ marginTop: 8 }}
                    onClick={() => setTlKind("all")}>
                    查看全部 {timeline.length} 条
                  </button>
                )}
              </div>
            )}
            {visibleTimeline.map((ev) => {
              const d = describe(ev, displayNameFor);
              const clarifyParsed = d.mention ? parseClarifyQuestions(d.text) : null;
              const p = ev.payload as Record<string, unknown>;
              const noteBody = ev.kind === "comment" ? String(p.body ?? "") : "";
              const noteAuthor = typeof p.author === "string" && p.author ? p.author : null;
              // 系统备注（context note）以 role_slug=NULL 存储，不能 fallback 成自然人。
              const isSystemNote = !ev.role && ev.kind === "comment"
                && (noteAuthor !== null || noteBody.startsWith("系统备注"));
              const noteStep = typeof p.for_step === "string" ? p.for_step : null;
              const role = ev.role ?? (isSystemNote ? "system" : "human");
              const showClarify = Boolean(
                clarifyParsed && clarifyParsed.questions.length > 0
                && (role === "cindy" || role === (task?.planner || "cindy")));
              const isHuman = role === "human";
              const systemLabel = SYSTEM_SENDERS[role];
              // 聊天框用户名显示昵称；后台仍保留 slug 作为身份。
              const displayRole = isSystemNote
                ? "系统"
                : isHuman
                  ? "你（自然人）"
                  : (systemLabel ?? displayNameFor(role));
              const roleInfo = roles.find((r) => r.slug === role);
              const liveRole = !isHuman && !systemLabel && !isSystemNote
                ? roleLiveFor(role) : null;
              const noteScope = noteStep ?? "全任务";
              const metaTitle = isSystemNote
                ? `系统备注 · 作者 ${noteAuthor ?? "系统"} · 作用范围 ${noteScope}`
                : isHuman ? "human" : `${displayRole}（${role}）`;
              const noteText = isSystemNote
                ? d.text.replace(/^系统备注(?:（给 [^）]+）)?：\s*/, "")
                : d.text;
              return (
                <div
                  className={`thread-item ${ev.kind === "comment" ? "is-message" : ""} ${d.mention ? "is-mention" : ""} ${isHuman ? "is-human" : ""} ${isSystemNote ? "is-system-note" : ""} ${systemLabel && !isSystemNote ? "is-system-event" : ""}`}
                  key={ev.key}
                >
                  {isSystemNote ? (
                    <span className="thread-avatar system-avatar" title="系统备注">
                      <Icon name="cpu" size={16} />
                    </span>
                  ) : d.mention ? (
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
                      tone={liveRole?.state.tone}
                    />
                  )}
                  <div className="thread-body">
                    <div className="thread-meta">
                      <strong title={metaTitle}>
                        {displayRole}
                      </strong>
                      {isSystemNote && (
                        <span className="thread-note-badge">系统备注</span>
                      )}
                      <span className="muted mono">{formatClock(ev.ts)}</span>
                      {ev.step_id && (
                        <span className="muted mono" title="执行号 / 步骤">
                          {ev.execution_no ? `E${ev.execution_no} ` : ""}{ev.step_id}
                        </span>
                      )}
                      {isSystemNote && (
                        <>
                          {noteAuthor && (
                            <span className="muted">作者 {displayNameFor(noteAuthor)}</span>
                          )}
                          <span className="thread-scope-tag" title="系统备注作用范围">
                            作用范围 · {noteScope}
                          </span>
                        </>
                      )}
                      {d.runStatus && <span className={`status status-${d.runStatus}`}>{runStatusLabel(d.runStatus)}</span>}
                    </div>
                    <div className="thread-text">
                      {d.mention ? (
                        <>
                          <span className="mention-tag">
                            @{displayNameFor(d.mention)}
                          </span>{" "}
                          {renderMentionText(
                            clarifyParsed ? clarifyParsed.text : d.text, roleTokens)}
                        </>
                      ) : (
                        renderMentionText(noteText, roleTokens)
                      )}
                    </div>
                    {showClarify && clarifyParsed && (
                      <ClarifyQuestions
                        taskId={id}
                        planner={task?.planner || "cindy"}
                        questions={clarifyParsed.questions}
                        answered={answeredClarifyKeys.has(ev.key)}
                        clarifyStatus={clarification?.status ?? "none"}
                        onAnswered={() => { void refresh(); }}
                      />
                    )}
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
                placeholder={clarification?.status === "in_progress"
                  ? cindyPending
                    ? "Cindy 正在整理澄清问题，问题出现后再回答；也可直接补充说明"
                    : "输入你的回答，发送后进入下一轮澄清（自动 @Cindy）"
                  : "输入消息；输入 @ 选择角色（界面显示昵称，后台用 slug 激活）"}
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
            <button className="primary" onClick={send} disabled={busy || !comment.trim()}>
              {clarification?.status === "in_progress" ? "发送给 Cindy（下一轮）" : "发送"}
            </button>
          </div>
          <p className="muted hint" style={{ margin: "4px 2px 0" }}>
            {clarification?.status === "in_progress" ? (
              cindyPending ? (
                <>Cindy 仍在思考并整理问题；问题卡出现后再选择回答。直接发送会让 Cindy 提前进入下一轮。</>
              ) : (
                <>澄清中：直接发送会唤醒 Cindy 并进入下一轮；只想评论不唤醒时，请先确认或跳过澄清。</>
              )
            ) : (
              <>
                提示：输入 <strong>@</strong> 会弹出角色选择（上下键选择，Enter 确认）；选中后按 Enter 发送。
                @角色 消息会按「@提及」发送——时间线标黄并唤醒该角色。
              </>
            )}
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
            <dd><span className={`status status-${task.status}`}>{TASK_STATUS_META[task.status].label}</span></dd>
            <dt>领队</dt>
            <dd>
              {task.assignee
                ? renderRoleLive(roleLiveFor(task.assignee))
                : (task.assignees?.length ? "待认领" : "未指派")}
            </dd>
            <dt>频道</dt>
            <dd>
              {task.workspace ? (
                <a href={`/channels?channel=${encodeURIComponent(task.workspace.id)}&tab=knowledge`}>
                  {task.workspace.name}
                </a>
              ) : task.workspace_id ? (
                <span className="mono">{task.workspace_id}</span>
              ) : (
                <a href={`/channels?channel=${UNBOUND_CHANNEL_VIEW}`}># 无主题</a>
              )}
            </dd>
            <dt>类型</dt><dd>{task.type ?? "—"}</dd>
            <dt>标签</dt><dd>{task.tags?.length ? task.tags.join(", ") : "—"}</dd>
            <dt>知识主题</dt>
            <dd>
              <span className="mono">{task.repo || "临时知识空间"}</span>
              {task.knowledge_mode && (
                <span className="muted">
                  {" "}· {KNOWLEDGE_MODE_LABEL[task.knowledge_mode] ?? task.knowledge_mode}
                </span>
              )}
            </dd>
            <dt>任务工作空间</dt>
            <dd>
              {task.task_workspace?.path ? (
                <span className="mono" title={task.task_workspace.path}>
                  {task.task_workspace.path.split("/task-workspaces/").pop()}
                </span>
              ) : "—"}
              {task.source_repo && (
                <span className="muted"> · Git 来源 {task.source_repo}</span>
              )}
            </dd>
            {task.archived_at && (
              <>
                <dt>归档</dt>
                <dd><a href="/archive">{task.archived_at.slice(0, 10)} · 查看归档</a></dd>
              </>
            )}
            <dt>ID</dt><dd className="mono">{task.id}</dd>
          </dl>

          <KnowledgeRepoPanel task={task} onChanged={refresh} />
          <ChannelKnowledgePanel task={task} onChanged={refresh} />
          <TaskKnowledgePanel task={task} />

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
      </>
      )}

      {tab === "workflow" && (
        <div className={`workflow-view ${wfView === "graph" ? "is-graph" : "is-list"}`}>
          <div className="wf-template-actions">
            {displayWorkflow && (
              <button className="small" disabled={busy}
                onClick={async () => {
                  setBusy(true); setError(null);
                  try {
                    await api.importWorkflowTemplate("workflow", displayWorkflow.id, task.title);
                    window.location.href = "/templates";
                  } catch (e) { setError(String(e)); setBusy(false); }
                }}>保存为模板</button>
            )}
            {displayWorkflow && (
              <>
                <button className="small" disabled={busy}
                  title="主决策在澄清阶段的简报卡；此处只影响下一次执行"
                  onClick={() => setShowWeight(true)}>高级：调整流程强度</button>
                <button className="small" disabled={dossierBusy}
                  onClick={async () => {
                    setDossierBusy(true); setError(null);
                    try {
                      const blob = await api.downloadWorkflowDossier(displayWorkflow.id, "md");
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = `dossier-${displayWorkflow.id.slice(0, 8)}.md`;
                      a.click();
                      URL.revokeObjectURL(url);
                    } catch (e) { setError(String(e)); }
                    finally { setDossierBusy(false); }
                  }}>{dossierBusy ? "导出中…" : "导出执行档案"}</button>
                <a className="small button-like"
                  href={`/tasks/${id}/dossier?workflow_id=${displayWorkflow.id}`}>
                  查看执行档案
                </a>
              </>
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
          {!displayWorkflow && !workflowLoaded && !workflowError && (
            <p className="muted">正在加载工作流…</p>
          )}
          {!displayWorkflow && workflowError && (
            <div className="wf-refresh-error">
              <p className="error">工作流加载失败：{workflowError}</p>
              <button className="small" onClick={() => void refreshWorkflow()}>重试</button>
            </div>
          )}
          {!displayWorkflow && workflowLoaded && !workflowError && (
            <p className="muted">还没有工作流。多角色任务启动后，Cindy 会生成步骤状态机并在此展示。</p>
          )}
          {displayWorkflow && workflowError && (
            <div className="wf-refresh-error">
              <p className="error">工作流刷新失败：{workflowError}（当前显示上次成功加载的数据）</p>
              <button className="small" onClick={() => void refreshWorkflow()}>重试</button>
            </div>
          )}
          {displayWorkflow && (
            <>
              <WorkflowSummary workflow={displayWorkflow} taskStatus={task.status}
                metrics={workflowMetrics} diff={graphDiff} />
              {!isViewingHistory && taskLeases && (
                <details className="wf-leases">
                  <summary>
                    路径预留 · {(taskLeases.paths || []).filter((p) => p.status === "active").length}
                    <span className="muted">执行租约 {taskLeases.steps.filter((s) => s.lease_status === "active").length}</span>
                  </summary>
                  <div className="wf-lease-list">
                    {(taskLeases.paths || []).filter((p) => p.status === "active").map((p) => (
                      <div key={p.id} className="wf-lease-row">
                        <span className="mono">{p.resource_key.split(":").slice(1).join(":")}</span>
                        <span className="muted">{p.holder || "—"}</span>
                        <span className="muted">
                          {p.expires_at ? `到期 ${formatDateTime(p.expires_at)}` : ""}
                        </span>
                        <button className="small" onClick={() => act(async () => {
                          await api.releaseTaskPaths(id,
                            [p.resource_key.split(":").slice(1).join(":")], p.holder || "human");
                          refreshWorkflow();
                        })}>释放</button>
                      </div>
                    ))}
                    {taskLeases.steps.filter((s) => s.lease_status === "active").map((s) => (
                      <div key={`${s.workflow_id}:${s.step_id}`} className="wf-lease-row">
                        <span className="mono">{s.step_id}</span>
                        <span className="muted">执行中 · {s.holder || "—"} · fence {s.fence_token}</span>
                        <span className="muted">
                          {s.expires_at ? `到期 ${formatDateTime(s.expires_at)}` : ""}
                        </span>
                      </div>
                    ))}
                    {taskLeases.paths.filter((p) => p.status === "active").length === 0
                      && taskLeases.steps.filter((s) => s.lease_status === "active").length === 0 && (
                      <span className="muted">当前没有活跃租约。</span>
                    )}
                    <div className="wf-lease-form">
                      <input value={reserveInput} onChange={(e) => setReserveInput(e.target.value)}
                        placeholder="预留路径，逗号分隔：src/a.ts, src/b.ts" />
                      <button className="small" disabled={!reserveInput.trim()} onClick={() => act(async () => {
                        const paths = reserveInput.split(",").map((x) => x.trim()).filter(Boolean);
                        if (!paths.length) return;
                        await api.reserveTaskPaths(id, paths, "human");
                        setReserveInput("");
                        refreshWorkflow();
                      })}>预留</button>
                    </div>
                  </div>
                </details>
              )}
              {isViewingHistory && (
                <div className="wf-history-banner">
                  <span>
                    正在查看第 {displayWorkflow.execution_no || 1} 次执行（只读）
                  </span>
                  <button className="small primary" onClick={() => setViewingWorkflow(null)}>
                    回到当前执行
                  </button>
                </div>
              )}
              {workflowHistory.length > 1 && (
                <details className="wf-history">
                  <summary>
                    执行历史 · {workflowHistory.length} 次
                    <span className="muted">旧执行保留，不会被新执行覆盖</span>
                  </summary>
                  <div className="wf-history-list">
                    {workflowHistory.map((item) => (
                      <button key={item.id} type="button"
                        className={`wf-history-row is-${item.status} ${viewingWorkflow?.id === item.id ? "is-selected" : ""}`}
                        onClick={() => act(async () => {
                          setViewingWorkflow(await api.getWorkflow(item.id));
                        })}>
                        <span className="wf-history-no">第 {item.execution_no} 次</span>
                        <span className={`status status-${item.status === "done" ? "done" : item.status === "running" ? "in_progress" : "closed"}`}>
                          {item.status === "done" ? "已完成" : item.status === "running" ? "执行中"
                            : item.status === "terminated" ? "已终止" : "失败"}
                        </span>
                        {item.weight && (
                          <span className={`wf-weight is-${item.weight}`}>
                            {WEIGHT_META[item.weight].label}
                          </span>
                        )}
                        <span className="muted">{formatDateTime(item.created_at)}</span>
                        <span className="muted">
                          {item.approved_count || 0}/{item.step_count} 步完成
                        </span>
                        {item.metrics && (
                          <span className="muted">
                            决策 {item.metrics.decisions?.total ?? 0} · 打回 {item.metrics.decisions?.rejected ?? 0}
                          </span>
                        )}
                        {item.termination_reason && (
                          <span className="wf-history-reason">原因：{item.termination_reason}</span>
                        )}
                      </button>
                    ))}
                  </div>
                </details>
              )}
              <div className="role-picker" style={{ margin: "4px 0 10px" }}>
                <button className={`small ${wfView === "graph" ? "primary" : ""}`}
                  onClick={() => setWfView("graph")}>图谱</button>
                <button className={`small ${wfView === "list" ? "primary" : ""}`}
                  onClick={() => setWfView("list")}>列表</button>
              </div>
              {wfView === "graph" ? (
                <WorkflowGraph
                  workflow={displayWorkflow}
                  roles={roles}
                  suspendedStepIds={suspendedStepIds}
                  busy={busy}
                  readOnly={isViewingHistory}
                  onOpenReview={(step) => setReviewStep(step)}
                  onRetry={(step) => act(async () => {
                    setWorkflow(await api.retryWorkflowStep(workflow!.id, step.step_id));
                  })}
                  onOpenRoleContext={(role, sliceKey) => {
                    setRuntimeFocus({ role, sliceKey });
                    selectTab("runtime");
                  }}
                />
              ) : (
                (() => {
                  const loops = summarizeLoops(displayWorkflow.steps);
                  const loopBySource = new Map(loops.map((l) => [l.sourceStepId, l]));
                  const rendered = new Set<string>();
                  const rows = displayWorkflow.steps.flatMap((st) => {
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
        <SubtasksPanel items={children} onChanged={() => { void refreshChildren(); }} />
      )}

      {tab === "runtime" && (
        <div className="task-runtime-tab">
          <RoleSessions task={task} roles={roles} workflow={workflow} dispatches={dispatches} relatedTaskIds={relatedTaskIds} roleStates={roleStates} focus={runtimeFocus} />
          <CheckJobsPanel taskId={task.id} />
        </div>
      )}
      </div>
      {reviewStep && workflow && (
        <GateReviewDrawer
          wid={workflow.id}
          sid={reviewStep.step_id}
          onClose={() => setReviewStep(null)}
          onDone={(wf) => { setWorkflow(wf); refreshWorkflow(); }}
        />
      )}
      {showWeight && (
        <WorkflowWeightDialog
          taskId={id}
          current={workflow}
          onClose={() => setShowWeight(false)}
          onApplied={(wf) => {
            if (wf) setWorkflow(wf);
            setViewingWorkflow(null);
            refreshWorkflow();
          }}
        />
      )}
    </div>
  );
}
