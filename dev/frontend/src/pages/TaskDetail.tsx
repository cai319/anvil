import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, isOnboardingRole, subscribeEvents, type BoardDraft, type Role, type Task, type TimelineEvent, type Workflow, type WorkflowStep } from "../api/client";
import { RoleSessions } from "./RoleSessions";
import { WorkflowGraph } from "./WorkflowGraph";
import { roleStatusFromWorkflow } from "../workflowStatus";
import { STATUS_META, isDynamicStep, summarizeLoops, type LoopSummary } from "../workflowGroups";

const STATUS_LABEL: Record<Task["status"], string> = {
  todo: "待办",
  in_progress: "进行中",
  in_review: "待评审",
  done: "完成",
  closed: "已关闭",
};

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

const ROLE_CLAIM_LABEL: Record<string, string> = {
  pending: "未认领",
  working: "工作中",
  submitted: "已提交",
  released: "已放弃",
  approved: "已通过",
};

const AVATAR_COLORS = ["#6d7cff", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa"];
function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function describe(ev: TimelineEvent): { text: string; runStatus?: string; mention?: string } {
  const p = ev.payload;
  switch (ev.kind) {
    case "comment":
      return { text: String(p.body ?? "") };
    case "mention": {
      const target = String(p.target ?? "human");
      const raw = String(p.message ?? "");
      const escaped = target.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return {
        text: raw.replace(new RegExp(`^@\\s*${escaped}\\s*[，,:：]?\\s*`, "i"), ""),
        mention: target,
      };
    }
    case "submit":
      return { text: "提交了评审" + (p.summary ? `：${p.summary}` : "") };
    case "review":
      return { text: p.approve ? "评审通过 ✅" : "评审驳回 ↩️" };
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
      return { text: `运行状态 → ${RUN_LABEL[String(p.status)] ?? p.status}`, runStatus: String(p.status) };
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
  const [busy, setBusy] = useState(false);

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
        <p><a href="/">← 返回任务大厅</a></p>
      </div>
    );
  if (error) return <p className="error">{error}</p>;
  if (!task) return <p className="muted">加载中…</p>;

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

  // 优先只联想任务参与角色（@ 才会唤醒）；任务尚未绑定角色时退回全部可指派角色。
  const assignedToTask = new Set(task.assignees ?? []);
  const mentionRoles = assignedToTask.size
    ? assignableRoles.filter((r) => assignedToTask.has(r.slug))
    : assignableRoles;

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
    if (prev && !/[\s([{,，。；;:：]/.test(prev)) {
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

  const chooseMention = (candidate: { slug: string; name: string }) => {
    const input = inputRef.current;
    if (!input || !mention) return;
    const caret = input.selectionStart ?? comment.length;
    let after = comment.slice(caret);
    if (after.startsWith(" ")) after = after.slice(1);
    const next = comment.slice(0, mention.start)
      + "@" + candidate.slug + " " + after;
    setComment(next);
    setMention(null);
    const pos = mention.start + candidate.slug.length + 2; // @ + slug + space
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
    // 自动解析消息中的 @角色（slug 或显示名），命中参与角色即走 mention（唤醒 + 时间线标黄）
    const byToken = new Map<string, string>();
    assignableRoles.forEach((r) => {
      byToken.set(r.slug.toLowerCase(), r.slug);
      if (r.display_name) byToken.set(r.display_name.toLowerCase(), r.slug);
    });
    const targets = [...new Set(
      [...text.matchAll(/@([\w\-\u4e00-\u9fa5]+)/g)]
        .map((m) => byToken.get(m[1].toLowerCase()))
        .filter((v): v is string => Boolean(v)))];
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
          <span className="wf-loop-mark">↻ 切片循环</span>
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
    <div className="session">
      <p className="session-back"><a href="/">← 任务大厅</a></p>

      {/* 头部 */}
      <div className="session-head">
        <div className="session-title">
          <h1>{task.title}</h1>
          <span className={`status status-${task.status}`}>{STATUS_LABEL[task.status]}</span>
        </div>
        <div className="session-meta">
          {task.assignee && <span>👤 {task.assignee}</span>}
          {task.assignees?.length > 0 ? (
            <span>👥 {task.assignees.join("、")}</span>
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
          <span className="review-banner-text">🔔 待人工评审 —— 请确认产出后「通过」或「驳回」</span>
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
      </div>

      {tab === "chat" && (
      <div className="task-layout">
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
                title="0 角色时 Cindy 会先分析并建议角色；多角色任务先由 Cindy 规划工作流，草案经你批准后按依赖执行"
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
            {task.status !== "closed" && (
              <button className="danger" onClick={() => act(() => api.cancelTask(id))} disabled={busy}>取消任务</button>
            )}
            <button
              className="danger"
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

          {dispatchNote && <p className="muted">{dispatchNote}</p>}

          {drafts.some((d) => d.status === "pending" && d.kind === "role_suggestion") && (
            <p className="muted">
              有角色建议待审批，请到收件箱处理；批准后会自动创建角色、绑定任务并重新规划工作流。
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
                      服务端按 loop 模板为每片生成 execute → verify → fix → verify2 → decide 小闭环。
                    </div>
                  )}
                  {d.tree?.steps?.length ? (
                    <ul className="muted" style={{ margin: "6px 0", paddingLeft: 18 }}>
                      {d.tree.steps.map((s, i) => (
                        <li key={s.id ?? i}>
                          {s.id ? `${s.id} ` : ""}{s.title} → {s.role ?? "未指派"}
                          {s.kind === "plan_slices" ? "（切片计划）" : ""}
                          {s.depends_on?.length ? `（依赖 ${s.depends_on.join("、")}）` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : d.tree?.children ? (
                    <ul className="muted" style={{ margin: "6px 0", paddingLeft: 18 }}>
                      {d.tree.children.map((c, i) => (
                        <li key={c.id ?? i}>
                          {c.title} → {c.role ?? "未指派"}
                          {c.depends_on?.length ? `（依赖 ${c.depends_on.join("、")}）` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {d.tree?.feedback_rules?.length ? (
                    <div className="muted">反馈路径：{d.tree.feedback_rules.join("；")}</div>
                  ) : null}
                  <div className="card-actions">
                    <button
                      className="primary small"
                      disabled={busy}
                      onClick={() => act(async () => {
                        await api.approveDraft([d.id]);
                        await api.listDrafts(id).then(setDrafts).catch(() => {});
                      })}
                    >批准并落板（子任务按依赖自动派发）</button>
                  </div>
                </div>
              ))}
            </section>
          )}

          <section className="session-thread" ref={threadRef} onScroll={onThreadScroll}>
            <div className="thread-head">
              <h2 className="pane-title">会话 · {filteredTimeline.length} 条</h2>
              <div className="thread-head-right">
                <button className="small thread-jump" onClick={scrollToTop}
                  title="跳到最早一条消息">↑ 最早</button>
                <button className="small thread-jump"
                  onClick={() => scrollToBottom("smooth")}
                  title="跳到最新一条消息">↓ 最新</button>
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
              const d = describe(ev);
              const role = ev.role ?? "human";
              return (
                <div
                  className={`thread-item ${ev.kind === "comment" ? "is-message" : ""} ${d.mention ? "is-mention" : ""}`}
                  key={ev.key}
                >
                  <span className="thread-avatar" style={{ background: d.mention ? "var(--amber)" : avatarColor(role) }}>
                    {d.mention ? "@" : role.slice(0, 1).toUpperCase()}
                  </span>
                  <div className="thread-body">
                    <div className="thread-meta">
                      <strong>{role}</strong>
                      <span className="muted mono">{ev.ts.slice(11, 19)}</span>
                      {d.runStatus && <span className={`status status-${d.runStatus}`}>{RUN_LABEL[d.runStatus]}</span>}
                    </div>
                    <div className="thread-text">
                      {d.mention ? (
                        <>
                          <span className="mention-tag">@{d.mention}</span> {d.text}
                        </>
                      ) : (
                        d.text
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </section>

          {/* 评论 / @提及 框 */}
          <div className="session-compose">
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
                onBlur={() => window.setTimeout(() => setMention(null), 150)}
                placeholder="输入消息；输入 @ 弹出角色选择，例如：@pm 请启动程序…"
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
                      <span className="mention-item-name">{c.name}</span>
                      <span className="mention-item-slug">@{c.slug}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button className="primary" onClick={send} disabled={busy || !comment.trim()}>发送</button>
          </div>
          <p className="muted" style={{ fontSize: 11, margin: "4px 2px 0" }}>
            提示：输入 <strong>@</strong> 会弹出角色选择（↑/↓ 选择，Enter 确认）；选中后按 Enter 发送。
            @角色 消息会按「@提及」发送——时间线标黄并唤醒该角色。
          </p>
        </div>

        {/* 右：任务信息面板 */}
        <aside className="task-side">
          <h2 className="pane-title">任务信息</h2>
          <dl className="task-meta">
            <dt>状态</dt>
            <dd><span className={`status status-${task.status}`}>{STATUS_LABEL[task.status]}</span></dd>
            <dt>领队</dt>
            <dd>{task.assignee ?? (task.assignees?.length ? "待认领" : "未指派")}</dd>
            <dt>类型</dt><dd>{task.type ?? "—"}</dd>
            <dt>标签</dt><dd>{task.tags?.length ? task.tags.join(", ") : "—"}</dd>
            <dt>ID</dt><dd className="mono">{task.id}</dd>
          </dl>

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
          <ul className="side-list">
            {(task.role_claims ?? []).length === 0 && <li className="muted">（未绑定多角色）</li>}
            {(task.role_claims ?? []).map((c) => {
              const wfSt = roleStatusFromWorkflow(workflow, c.role_slug);
              const cls = wfSt?.cls ?? (c.status === "submitted" ? "in_review" : c.status === "approved" ? "done" : c.status === "working" ? "in_progress" : "todo");
              const label = wfSt?.label ?? ROLE_CLAIM_LABEL[c.status] ?? c.status;
              return (
                <li key={c.role_slug}>
                  <strong>{c.role_slug}</strong>{" "}
                  <span className={`status status-${cls}`}>{label}</span>
                  {c.summary && <div className="muted">{c.summary}</div>}
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
        <div className="workflow-view">
          {!workflow && (
            <p className="muted">还没有工作流。多角色任务派发后，Cindy 会生成步骤状态机并在此展示。</p>
          )}
          {workflow && (
            <>
              <p className="muted">
                工作流 {workflow.status === "done" ? "已完成（等待最终评审）"
                  : workflow.status === "running" ? "推进中" : "已失败"}
                {" · "}{workflow.steps.filter((s) => s.status === "approved").length}/{workflow.steps.length} 步完成
              </p>
              {workflow.definition?.feedback_rules?.length ? (
                <p className="muted">反馈路径：{workflow.definition.feedback_rules.join("；")}</p>
              ) : null}
              {workflow.definition?.verification?.length ? (
                <div className="wf-matrix">
                  <span className="muted">验证矩阵：</span>
                  {workflow.definition.verification.map((v, i) => (
                    <span key={i} className="wf-matrix-cell">
                      <strong>{v.cell}</strong> · {v.method}
                      {v.verifier_role ? ` · ${v.verifier_role}` : ""}
                    </span>
                  ))}
                </div>
              ) : null}
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
  );
}
