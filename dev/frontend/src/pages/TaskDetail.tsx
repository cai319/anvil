import { useEffect, useRef, useState } from "react";
import { api, subscribeEvents, type BoardDraft, type Role, type Task, type TimelineEvent, type Workflow } from "../api/client";
import { RoleSessions } from "./RoleSessions";
import { WorkflowGraph } from "./WorkflowGraph";

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
    case "mention":
      return { text: String(p.message ?? ""), mention: String(p.target ?? "human") };
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
    default:
      return { text: ev.kind };
  }
}

export function TaskDetail({ id }: { id: string }) {
  const [task, setTask] = useState<Task | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [assignee, setAssignee] = useState("");
  const [comment, setComment] = useState("");
  const [asRole, setAsRole] = useState("human");
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const [drafts, setDrafts] = useState<BoardDraft[]>([]);
  const [dispatchNote, setDispatchNote] = useState<string | null>(null);
  const [tab, setTab] = useState<"chat" | "workflow" | "runtime">("chat");
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [wfView, setWfView] = useState<"graph" | "list">("graph");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  const refreshWorkflow = () =>
    api.getTaskWorkflow(id).then(setWorkflow).catch(() => setWorkflow(null));

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

  useEffect(() => {
    if (gone) return; // 任务已删除：不再刷新、不再订阅事件
    refresh();
    refreshWorkflow();
    api.listRoles().then(setRoles).catch(() => {});
    const refreshDrafts = () => api.listDrafts(id).then(setDrafts).catch(() => {});
    refreshDrafts();
    return subscribeEvents((ev) => {
      if (ev.topic === "tick" || (ev.topic === "task" && ev.payload.id === id)) {
        refresh();
        refreshWorkflow();
        refreshDrafts();
      }
    });
  }, [id, gone]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [timeline.length]);

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

  const send = async () => {
    const text = comment.trim();
    if (!text) return;
    // 自动解析消息中的 @角色（slug 或显示名），命中参与角色即走 mention（唤醒 + 时间线标黄）
    const byToken = new Map<string, string>();
    roles.forEach((r) => {
      byToken.set(r.slug.toLowerCase(), r.slug);
      if (r.display_name) byToken.set(r.display_name.toLowerCase(), r.slug);
    });
    const targets = [...new Set(
      [...text.matchAll(/@([\w\-\u4e00-\u9fa5]+)/g)]
        .map((m) => byToken.get(m[1].toLowerCase()))
        .filter((v): v is string => Boolean(v)))];
    const senderRole = asRole === "human" ? undefined : asRole;
    await act(async () => {
      if (targets.length) {
        for (const target of targets) {
          await api.mentionTask(id, target, text, senderRole);
        }
      } else {
        await api.commentTask(id, text, senderRole);
      }
      setComment("");
    });
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
          <span>👤 {task.assignee || "未指派"}</span>
          {task.assignees?.length > 0 && <span>👥 {task.assignees.join("、")}</span>}
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
                  {roles.map((r) => (
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
            {task.assignees?.length > 0 && (task.status === "todo" || task.status === "in_progress") && (
              <button
                className="primary"
                disabled={busy}
                title="多角色任务先由规划助理 Cindy 分析任务并设计工作流，草案经你批准后按依赖执行"
                onClick={() => act(async () => {
                  const res = await api.dispatchTask(id);
                  setDispatchNote(
                    res.mode === "planning"
                      ? `规划助理 ${res.planner} 正在分析任务并设计工作流…草案将出现在收件箱，批准后自动派发子任务。`
                      : res.mode === "pending_review"
                        ? "已有待审工作流草案，请到收件箱审阅批准。"
                        : "已直接派发参与角色。");
                })}
              >{task.assignees.length >= 2 ? "派发（先由 Cindy 规划）" : "派发并执行"}</button>
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

          {drafts.filter((d) => d.status === "pending").length > 0 && (
            <section className="session-thread">
              <h2 className="pane-title">工作流草案 · 待审批</h2>
              {drafts.filter((d) => d.status === "pending").map((d) => (
                <div key={d.id} className="card" style={{ marginBottom: 8 }}>
                  <strong>{d.goal}</strong>
                  <div className="muted">
                    by {d.created_by}
                    {d.tree?.leader && <> · leader：{d.tree.leader}</>}
                    {d.tree?.children && <> · {d.tree.children.length} 个子任务</>}
                  </div>
                  {d.tree?.children && (
                    <ul className="muted" style={{ margin: "6px 0", paddingLeft: 18 }}>
                      {d.tree.children.map((c, i) => (
                        <li key={c.id ?? i}>
                          {c.title} → {c.role ?? "未指派"}
                          {c.depends_on?.length ? `（依赖 ${c.depends_on.join("、")}）` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
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

          <section className="session-thread">
            <h2 className="pane-title">会话 · {timeline.length} 条交互</h2>
            {timeline.length === 0 && (
              <p className="muted empty">暂无交互 —— 认领、评论、提交、评审、运行都会出现在这里。</p>
            )}
            {timeline.map((ev) => {
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
            <div ref={endRef} />
          </section>

          {/* 评论 / @提及 框 */}
          <div className="session-compose">
            <select value={asRole} onChange={(e) => setAsRole(e.target.value)} title="发送身份（默认 human）">
              <option value="human">human（你）</option>
              {roles.map((r) => (
                <option key={r.slug} value={r.slug}>{r.display_name}</option>
              ))}
            </select>
            <input
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder="输入消息；@角色 会唤醒对方，例如：@pm 请启动程序，让我做网页版人工验证"
            />
            <button className="primary" onClick={send} disabled={busy || !comment.trim()}>发送</button>
          </div>
          <p className="muted" style={{ fontSize: 11, margin: "4px 2px 0" }}>
            提示：消息中出现 @角色（slug 或显示名）时自动按「@提及」发送——时间线标黄并唤醒该角色。
          </p>
        </div>

        {/* 右：任务信息面板 */}
        <aside className="task-side">
          <h2 className="pane-title">任务信息</h2>
          <dl className="task-meta">
            <dt>状态</dt>
            <dd><span className={`status status-${task.status}`}>{STATUS_LABEL[task.status]}</span></dd>
            <dt>指派</dt><dd>{task.assignee ?? "未指派"}</dd>
            <dt>类型</dt><dd>{task.type ?? "—"}</dd>
            <dt>标签</dt><dd>{task.tags?.length ? task.tags.join(", ") : "—"}</dd>
            <dt>ID</dt><dd className="mono">{task.id}</dd>
          </dl>

          {task.description && (
            <>
              <h3 className="side-h">描述</h3>
              <p className="side-p">{task.description}</p>
            </>
          )}

          <h3 className="side-h">参与角色</h3>
          <ul className="side-list">
            {(task.role_claims ?? []).length === 0 && <li className="muted">（未绑定多角色）</li>}
            {(task.role_claims ?? []).map((c) => (
              <li key={c.role_slug}>
                <strong>{c.role_slug}</strong>{" "}
                <span className={`status status-${c.status === "submitted" ? "in_review" : c.status === "approved" ? "done" : c.status === "working" ? "in_progress" : "todo"}`}>
                  {ROLE_CLAIM_LABEL[c.status] ?? c.status}
                </span>
                {c.summary && <div className="muted">{c.summary}</div>}
              </li>
            ))}
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
              <div className="wf-steps">
                {workflow.steps.map((st) => {
                  const cls = st.status === "approved" ? "done" : st.status === "running" ? "in_progress"
                    : st.status === "submitted" ? "in_review" : st.status === "pending" ? "todo" : "closed";
                  const label = st.status === "approved" ? "完成" : st.status === "running" ? "进行中"
                    : st.status === "submitted" ? "待审核" : st.status === "pending" ? "待办"
                    : st.status === "rejected" ? "已驳回" : st.status === "failed" ? "失败" : "跳过";
                  return (
                    <div key={st.step_id} className="wf-step">
                      <span className="wf-id mono">{st.step_id}</span>
                      <div className="wf-main">
                        <div className="wf-title">
                          {st.title}
                          <span className="wf-role">{st.role_slug}</span>
                          {st.gate && <span className="badge wf-gate">审核门</span>}
                          <span className={`status status-${cls}`}>{label}</span>
                        </div>
                        {st.depends_on?.length > 0 && <div className="muted wf-deps">依赖：{st.depends_on.join("、")}</div>}
                        {st.summary && <div className="muted wf-summary">{st.summary}</div>}
                      </div>
                      <div className="wf-actions">
                        {st.run_id && <a href={`/runs/${st.run_id}`}>会话</a>}
                        {st.gate && st.status === "submitted" && (
                          <>
                            <button className="small primary" onClick={() => act(async () => {
                              const w = await api.reviewWorkflowStep(workflow.id, st.step_id, true);
                              setWorkflow(w);
                            })}>通过</button>
                            <button className="small danger" onClick={() => act(async () => {
                              const w = await api.reviewWorkflowStep(workflow.id, st.step_id, false, "请按评审意见修改");
                              setWorkflow(w);
                            })}>驳回</button>
                          </>
                        )}
                        {(st.status === "failed" || st.status === "rejected") && (
                          <button className="small" onClick={() => act(async () => {
                            const w = await api.retryWorkflowStep(workflow.id, st.step_id);
                            setWorkflow(w);
                          })}>重试</button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              )}
            </>
          )}
        </div>
      )}

      {tab === "runtime" && <RoleSessions task={task} roles={roles} />}
    </div>
  );
}
