import { useState } from "react";
import type { RuntimeProposal } from "../../api/client";
import { formatDateTime } from "../../time";
import { confirmDialog } from "../../components/dialogs";
import {
  PROPOSAL_PRIMARY_TRANSITION,
  PROPOSAL_TERMINAL_STATUSES,
  PROPOSAL_TRANSITION_LABEL,
  allowedTransitions,
  isProposalTerminal,
  proposalComponentLabel,
  proposalSeverityLabel,
  proposalStatusLabel,
} from "./proposalMeta";

export function ProposalDetail({
  proposal, markdown, loading, error, onTransition, onExport,
}: {
  proposal: RuntimeProposal;
  markdown: string;
  loading: boolean;
  error: string | null;
  onTransition: (status: string, note?: string) => Promise<boolean>;
  onExport: () => Promise<string | null>;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [activeError, setActiveError] = useState<string | null>(null);
  const primary = PROPOSAL_PRIMARY_TRANSITION[proposal.status];
  const others = allowedTransitions(proposal.status).filter((status) => status !== primary);

  const submit = async (status: string) => {
    if (PROPOSAL_TERMINAL_STATUSES.has(status)) {
      const ok = await confirmDialog({
        title: `标记为「${proposalStatusLabel(status)}」？`,
        message: "该状态是终态，之后不能继续流转，只能导出。",
        confirmLabel: "确认",
        danger: status === "rejected",
      });
      if (!ok) return;
    }
    setBusy(true);
    setActiveError(null);
    const ok = await onTransition(status, note.trim() || undefined);
    if (ok) setNote("");
    else setActiveError("流转失败，请检查状态是否已被其他人改变。");
    setBusy(false);
  };

  const exportMd = async () => {
    setBusy(true);
    setActiveError(null);
    const path = await onExport();
    if (!path) setActiveError("导出失败。");
    setBusy(false);
  };

  return (
    <div className="proposal-detail">
      <div className="role-detail-head">
        <div className="role-detail-title">
          <h2>{proposal.title}</h2>
          <div className="proposal-headline-meta">
            <span className="mono">{proposal.id}</span>
            <span className={`proposal-status is-${proposal.status}`}>
              {proposalStatusLabel(proposal.status)}
            </span>
            <span className={`proposal-sev is-${proposal.severity}`}>
              {proposalSeverityLabel(proposal.severity)}
            </span>
          </div>
        </div>
      </div>
      <div className="role-detail-body proposal-detail-body">
        {error && <p className="error">{error}</p>}
        <dl className="proposal-meta">
          <div><dt>组件</dt><dd>{proposalComponentLabel(proposal.component_type)}
            {proposal.component_key ? ` · ${proposal.component_key}` : ""}</dd></div>
          {proposal.runtime_id && <div><dt>运行时</dt><dd className="mono">{proposal.runtime_id}</dd></div>}
          <div><dt>来源</dt><dd>
            {proposal.origin || "—"}
            {proposal.origin_role ? ` · ${proposal.origin_role}` : ""}
          </dd></div>
          <div><dt>证据</dt><dd>{proposal.evidence_count} 条</dd></div>
          <div><dt>创建 / 更新</dt><dd>
            {formatDateTime(proposal.created_at)} · {formatDateTime(proposal.updated_at)}
          </dd></div>
          {proposal.decided_by && (
            <div><dt>最近决策</dt><dd>
              {proposal.decided_by}
              {proposal.decided_at ? ` · ${formatDateTime(proposal.decided_at)}` : ""}
              {proposal.decision_note ? ` · ${proposal.decision_note}` : ""}
            </dd></div>
          )}
          {proposal.export_path && (
            <div><dt>最近导出</dt><dd className="mono">{proposal.export_path}</dd></div>
          )}
        </dl>

        <p className="muted proposal-evidence-note">
          当前接口只提供证据计数与最近决策；完整证据和决策历史需要后端持久化。
        </p>

        <div className="proposal-actionbar">
          {!isProposalTerminal(proposal.status) && (
            <>
              <label className="proposal-note-field">
                <span>流转备注（可选）</span>
                <input value={note} disabled={busy}
                  placeholder="写入 decision_note；留空保留上次备注"
                  onChange={(e) => setNote(e.target.value)} />
              </label>
              <div className="proposal-actions">
                {primary && (
                  <button className="primary" disabled={busy}
                    onClick={() => { void submit(primary); }}>
                    {PROPOSAL_TRANSITION_LABEL[primary] ?? primary}
                  </button>
                )}
                {others.length > 0 && (
                  <details className="proposal-other-transitions">
                    <summary>其他流转</summary>
                    <div>
                      {others.map((status) => (
                        <button key={status}
                          className={`small ${status === "rejected" ? "danger" : ""}`}
                          disabled={busy}
                          onClick={() => { void submit(status); }}>
                          {PROPOSAL_TRANSITION_LABEL[status] ?? status}
                        </button>
                      ))}
                    </div>
                  </details>
                )}
                <button className="small" disabled={busy}
                  onClick={() => { void exportMd(); }}>
                  导出 Markdown 到服务器
                </button>
              </div>
            </>
          )}
          {isProposalTerminal(proposal.status) && (
            <div className="proposal-actions">
              <span className="muted">已终结，仅可导出。</span>
              <button className="small" disabled={busy}
                onClick={() => { void exportMd(); }}>
                导出 Markdown 到服务器
              </button>
            </div>
          )}
          {activeError && <p className="error">{activeError}</p>}
          <p className="muted proposal-export-note">导出会写入服务器 docs/runtime-proposals，不会下载到浏览器。</p>
        </div>

        <pre className="proposal-markdown">
          {loading ? "加载中…" : markdown || "（无 Markdown 内容）"}
        </pre>
      </div>
    </div>
  );
}
