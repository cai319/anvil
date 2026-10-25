import type { KnowledgeStatus, TaskKnowledge, WorkspaceKnowledge } from "../../api/client";
import {
  AUTHORITY_LABEL,
  KNOWLEDGE_KIND_LABEL,
  KNOWLEDGE_STATUS_LABEL,
} from "../../knowledgeCard";

const STATUS_CLASS: Record<KnowledgeStatus, string> = {
  draft: "todo",
  proposed: "in_review",
  approved: "done",
  rejected: "closed",
  deprecated: "closed",
  expired: "closed",
};

function fmtDate(value?: string | null): string {
  return value ? value.slice(0, 10) : "";
}

export function KnowledgeBadges({ kind, authority, status, expired, reviewAt, conflicts }: {
  kind: WorkspaceKnowledge["kind"];
  authority?: WorkspaceKnowledge["authority"];
  status?: KnowledgeStatus;
  expired?: boolean;
  reviewAt?: string | null;
  conflicts?: string[];
}) {
  return (
    <span className="knowledge-record-badges">
      {authority && (
        <span className={`authority-badge authority-${authority}`}>
          {AUTHORITY_LABEL[authority]}
        </span>
      )}
      {status && (
        <span className={`status status-${STATUS_CLASS[status] ?? "closed"}`}>
          {KNOWLEDGE_STATUS_LABEL[status] ?? status}
        </span>
      )}
      <span className="chip">{KNOWLEDGE_KIND_LABEL[kind] ?? kind}</span>
      {expired && <span className="review-reason">已过期，不注入</span>}
      {reviewAt && <span className="review-reason">复核 {fmtDate(reviewAt)}</span>}
      {conflicts && conflicts.length > 0 && (
        <span className="review-reason">冲突 {conflicts.length}</span>
      )}
    </span>
  );
}

export function ChannelKnowledgeRow({ card, busy, onApprove, onReject }: {
  card: WorkspaceKnowledge;
  busy: boolean;
  onApprove: (card: WorkspaceKnowledge) => void;
  onReject: (card: WorkspaceKnowledge) => void;
}) {
  const channelName = card.workspace_name || "已删除频道";
  return (
    <li className="knowledge-record">
      <div className="knowledge-record-head">
        <div className="knowledge-record-main">
          <strong className="knowledge-record-title">{card.title}</strong>
          <span className="knowledge-record-meta">
            <KnowledgeBadges
              kind={card.kind}
              authority={card.authority}
              status={card.status}
              expired={card.is_expired}
              reviewAt={card.review_at}
              conflicts={card.conflicts}
            />
            <a href={`/channels?channel=${encodeURIComponent(card.workspace_id)}&tab=knowledge`}>
              {channelName}
            </a>
            {card.completeness < 1 && (
              <span className="muted">完整度 {Math.round(card.completeness * 100)}%</span>
            )}
          </span>
          <span className="knowledge-record-tldr">{card.tldr}</span>
          <details className="knowledge-record-details">
            <summary>查看知识卡</summary>
            <pre>{card.body_md}</pre>
          </details>
        </div>
        <span className="knowledge-record-actions">
          {card.status === "proposed" && (
            <>
              <button type="button" className="small primary" disabled={busy}
                onClick={() => onApprove(card)}>批准</button>
              <button type="button" className="small danger" disabled={busy}
                onClick={() => onReject(card)}>拒绝</button>
            </>
          )}
          <a className="small button-like"
            href={`/channels?channel=${encodeURIComponent(card.workspace_id)}&tab=knowledge`}>
            频道页
          </a>
        </span>
      </div>
    </li>
  );
}

export function TaskKnowledgeRow({ card }: { card: TaskKnowledge }) {
  const archived = Boolean(card.task_archived_at) || card.status === "archived";
  return (
    <li className="knowledge-record">
      <div className="knowledge-record-main">
        <div className="knowledge-record-head">
          <a className="knowledge-record-title" href={`/tasks/${card.task_id}`}>
            {card.task_title || card.task_id}
          </a>
          <span className="knowledge-record-actions">
            <a className="small button-like" href={`/tasks/${card.task_id}`}>任务页</a>
          </span>
        </div>
        <strong className="knowledge-record-title">{card.title}</strong>
        <span className="knowledge-record-meta">
          <KnowledgeBadges kind={card.kind} />
          {card.workspace_name && <span>{card.workspace_name}</span>}
          {card.source_subtask_id && (
            <span className="review-reason">来自子任务 {card.source_subtask_id}</span>
          )}
          {archived && <span className="chip">任务已归档</span>}
          {card.source_role && <span className="muted">来源：{card.source_role}</span>}
        </span>
        <span className="knowledge-record-tldr">{card.tldr}</span>
        <details className="knowledge-record-details">
          <summary>查看知识卡</summary>
          <pre>{card.body_md}</pre>
        </details>
      </div>
    </li>
  );
}
