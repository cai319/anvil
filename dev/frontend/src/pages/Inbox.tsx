import { useEffect, useState } from "react";
import { api, subscribeEvents, type InboxItem } from "../api/client";
import { formatDateTime } from "../time";
import { PageHeader } from "../components/ui";

/** Inbox（信息架构重构）：四类人机决策点统一入口，action card 式就地操作。 */

const KIND_LABEL: Record<InboxItem["kind"], string> = {
  review: "评审",
  mention: "@我",
  draft: "草案",
  lesson: "经验",
  proposal: "提案",
  gate: "审核门",
  action: "操作申请",
};

const KIND_COLOR: Record<InboxItem["kind"], string> = {
  review: "#b91c1c",
  mention: "#1d4ed8",
  draft: "#6b21a8",
  lesson: "#166534",
  proposal: "#854d0e",
  gate: "#92400e",
  action: "#be185d",
};

type Tab = "all" | InboxItem["kind"];

const TABS: { key: Tab; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "review", label: "评审" },
  { key: "mention", label: "@我" },
  { key: "draft", label: "草案" },
  { key: "lesson", label: "经验" },
  { key: "proposal", label: "提案" },
  { key: "gate", label: "审核门" },
  { key: "action", label: "操作申请" },
];

export function Inbox() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [tab, setTab] = useState<Tab>("all");
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = () =>
    api.getInbox().then((b) => setItems(b.items)).catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
    return subscribeEvents(() => refresh()); // 任何事件后重取（轻量只读端点）
  }, []);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const shown = tab === "all" ? items : items.filter((i) => i.kind === tab);
  const countOf = (k: Tab) => (k === "all" ? items.length : items.filter((i) => i.kind === k).length);

  const renderActions = (it: InboxItem) => {
    const disabled = busyId !== null;
    const raw = it.id.split(":", 2)[1]; // review:T -> T；draft:D -> D；lesson:L -> L；proposal:P -> P
    switch (it.kind) {
      case "review":
        return (
          <>
            <button className="primary small" disabled={disabled}
              onClick={() => act(it.id, () => api.reviewTask(it.task_id!, true))}>通过</button>
            <button className="small danger" disabled={disabled}
              onClick={() => act(it.id, () => api.reviewTask(it.task_id!, false))}>驳回</button>
            <a className="small" href={`/tasks/${it.task_id}`}>看详情</a>
          </>
        );
      case "mention":
        return (
          <a className="small button-like" href={`/tasks/${it.task_id}`}>去回复（会话上下文在任务详情）</a>
        );
      case "draft": {
        const isRoleSuggestion = it.meta?.draft_kind === "role_suggestion";
        return (
          <>
            <button className="primary small" disabled={disabled}
              onClick={() => act(it.id, () => api.approveDraft([raw]))}>
              {isRoleSuggestion ? "批准并创建角色" : "批准落板"}
            </button>
            <button className="small danger" disabled={disabled}
              onClick={() => act(it.id, () => api.rejectDraftById(raw))}>驳回</button>
            {it.meta?.draft_kind === "workflow" && (
              <a className="small" href={`/templates?draft=${encodeURIComponent(raw)}`}>
                在模板工作台编辑
              </a>
            )}
            {it.task_id && <a className="small" href={`/tasks/${it.task_id}`}>看任务</a>}
          </>
        );
      }
      case "lesson":
        return (
          <>
            <button className="primary small" disabled={disabled}
              onClick={() => act(it.id, () => api.approveLesson(raw))}>批准</button>
            <button className="small danger" disabled={disabled}
              onClick={() => act(it.id, () => api.rejectLesson(raw))}>拒绝</button>
          </>
        );
      case "proposal":
        return (
          <>
            <button className="primary small" disabled={disabled}
              onClick={() => act(it.id, () => api.approveProposal(raw))}>批准</button>
            <button className="small danger" disabled={disabled}
              onClick={() => act(it.id, () => api.rejectProposal(raw))}>拒绝</button>
          </>
        );
      case "action": {
        const actionId = it.id.split(":", 2)[1];
        return (
          <>
            <button className="primary small" disabled={disabled}
              onClick={() => {
                if (!window.confirm(`批准后将立即执行「${it.title}」，确定？`)) return;
                act(it.id, () => api.approveActionCard(actionId));
              }}>批准并执行</button>
            <button className="small danger" disabled={disabled}
              onClick={() => act(it.id, () => api.rejectActionCard(actionId))}>拒绝</button>
            {it.task_id && <a className="small" href={`/tasks/${it.task_id}`}>看任务</a>}
          </>
        );
      }
      case "gate": {
        const [, wid, sid] = it.id.split(":");
        return (
          <>
            <button className="primary small" disabled={disabled}
              onClick={() => act(it.id, () => api.reviewWorkflowStep(wid, sid, true))}>通过</button>
            <button className="small danger" disabled={disabled}
              onClick={() => act(it.id, () => api.reviewWorkflowStep(wid, sid, false, "请按评审意见修改"))}>驳回</button>
            {it.task_id && <a className="small" href={`/tasks/${it.task_id}`}>看工作流</a>}
          </>
        );
      }
    }
  };

  return (
    <div className="session">
      <PageHeader
        kicker="INBOX"
        title={<>收件箱 · 待我处理 {items.length > 0 && <span className="badge badge-in_progress">{items.length}</span>}</>}
      />

      {error && <p className="error">{error}</p>}

      <div className="role-picker" style={{ marginBottom: 12 }}>
        {TABS.map((t) => (
          <button key={t.key} className={`small ${tab === t.key ? "primary" : ""}`}
            onClick={() => setTab(t.key)}>
            {t.label}{countOf(t.key) > 0 ? ` ${countOf(t.key)}` : ""}
          </button>
        ))}
      </div>

      {shown.length === 0 && <p className="muted">没有待处理的事项 —— 所有 agent 各自运转中。</p>}

      {shown.map((it) => (
        <div key={it.id} className="card inbox-item" style={{ borderLeft: `3px solid ${KIND_COLOR[it.kind]}` }}>
          <div className="inbox-head">
            <span className="badge" style={{ background: `${KIND_COLOR[it.kind]}22`, color: KIND_COLOR[it.kind] }}>
              {KIND_LABEL[it.kind]}
            </span>
            <strong>{it.title}</strong>
            <span className="muted mono">{formatDateTime(it.created_at)}</span>
          </div>
          <div className="muted" style={{ margin: "4px 0 8px" }}>{it.detail}</div>
          {it.meta?.draft_kind === "role_suggestion" && (it.meta.suggested_roles?.length ?? 0) > 0 && (
            <div className="muted" style={{ marginBottom: 8 }}>
              建议角色：
              {it.meta.suggested_roles!.map((r) => `${r.display_name || r.slug}（${r.slug}）`).join("、")}
              {it.meta.assessment ? ` · ${it.meta.assessment}` : ""}
            </div>
          )}
          {it.kind === "draft" && it.meta?.draft_kind !== "role_suggestion" && (
            <div className="repo-draft-meta">
              {(it.meta?.tags?.length ?? 0) > 0 && (
                <span className="muted">标签：{it.meta!.tags!.join("、")}</span>
              )}
              {(it.meta?.knowledge_repos?.length ?? 0) > 0 && (
                <span className="repo-chips">
                  {it.meta!.knowledge_repos!.map((r) => (
                    <span key={r.repo} className="repo-chip repo-applied"
                      title={r.reason ?? undefined}>
                      <span className="repo-chip-name">{r.repo}</span>
                      <span className="repo-chip-meta">Cindy</span>
                    </span>
                  ))}
                </span>
              )}
              {it.meta?.binding_locked && <span className="repo-lock">已人工固定</span>}
            </div>
          )}
          <div className="card-actions" style={{ gap: 6 }}>{renderActions(it)}</div>
        </div>
      ))}
    </div>
  );
}
