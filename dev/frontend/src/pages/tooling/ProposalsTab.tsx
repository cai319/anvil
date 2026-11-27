import { useEffect, useMemo, useRef, useState } from "react";
import { useLiveRefresh } from "../../useLiveRefresh";
import { EmptyState } from "../../components/ui";
import { formatDateTime } from "../../time";
import { ProposalDetail } from "./ProposalDetail";
import {
  PROPOSAL_ACTIVE_STATUSES,
  PROPOSAL_SEVERITY_LABEL,
  PROPOSAL_STATUSES,
  PROPOSAL_STATUS_LABEL,
  proposalComponentLabel,
  proposalSeverityLabel,
  proposalStatusLabel,
  severityRank,
} from "./proposalMeta";
import { toolingHref } from "./toolingRoute";
import { useRuntimeProposals } from "./useRuntimeProposals";

export function ProposalsTab({
  ripId, onSelectProposal, onActiveCount,
}: {
  ripId: string | null;
  onSelectProposal: (id: string | null) => void;
  onActiveCount: (count: number) => void;
}) {
  const {
    items, listLoading, listError, selected, markdown, detailLoading, detailError,
    load, selectById, clearSelection, transition, exportMarkdown,
  } = useRuntimeProposals();
  const [statusFilter, setStatusFilter] = useState("active");
  const [severityFilter, setSeverityFilter] = useState("");
  const [componentFilter, setComponentFilter] = useState("");
  const deepLinkRef = useRef<string | null>(null);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveRefresh(load, {
    topics: [],
    pollMs: 30_000,
    initial: false,
  });

  const activeCount = useMemo(
    () => items.filter((item) => PROPOSAL_ACTIVE_STATUSES.has(item.status)).length,
    [items],
  );
  useEffect(() => { onActiveCount(activeCount); }, [activeCount, onActiveCount]);

  useEffect(() => {
    if (!ripId) {
      deepLinkRef.current = null;
      clearSelection();
      return;
    }
    if (deepLinkRef.current === ripId || selected?.id === ripId) return;
    deepLinkRef.current = ripId;
    void selectById(ripId).then((proposal) => {
      if (proposal && !PROPOSAL_ACTIVE_STATUSES.has(proposal.status)) {
        setStatusFilter("all");
      }
    });
  }, [ripId, selected?.id, selectById, clearSelection]);

  useEffect(() => {
    if (!selected) return;
    const latest = items.find((item) => item.id === selected.id);
    if (latest && latest.updated_at !== selected.updated_at) {
      void selectById(selected.id);
    }
  }, [items, selected, selectById]);

  const severities = useMemo(() => {
    const values = [...new Set(items.map((item) => item.severity))];
    return values.sort((a, b) => severityRank(a) - severityRank(b));
  }, [items]);

  const components = useMemo(
    () => [...new Set(items.map((item) => item.component_type))].sort(),
    [items],
  );

  const filtered = useMemo(() => items.filter((item) => {
    if (statusFilter === "active" && !PROPOSAL_ACTIVE_STATUSES.has(item.status)) return false;
    if (statusFilter !== "active" && statusFilter !== "all" && item.status !== statusFilter) {
      return false;
    }
    if (severityFilter && item.severity !== severityFilter) return false;
    if (componentFilter && item.component_type !== componentFilter) return false;
    return true;
  }), [items, statusFilter, severityFilter, componentFilter]);

  const clearFilters = () => {
    setStatusFilter("active");
    setSeverityFilter("");
    setComponentFilter("");
  };

  const openProposal = (id: string) => {
    onSelectProposal(id);
    deepLinkRef.current = id;
    void selectById(id).then((proposal) => {
      if (proposal && !PROPOSAL_ACTIVE_STATUSES.has(proposal.status)) {
        setStatusFilter("all");
      }
    });
  };

  return (
    <div className="tooling-proposals">
      <div className="toolbar proposal-toolbar">
        <label className="field-inline">
          <span>状态</span>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="active">未结束</option>
            <option value="all">全部</option>
            {PROPOSAL_STATUSES.map((status) => (
              <option key={status} value={status}>{PROPOSAL_STATUS_LABEL[status]}</option>
            ))}
          </select>
        </label>
        <label className="field-inline">
          <span>严重度</span>
          <select value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
            <option value="">全部</option>
            {severities.map((severity) => (
              <option key={severity} value={severity}>
                {PROPOSAL_SEVERITY_LABEL[severity] ?? severity}
              </option>
            ))}
          </select>
        </label>
        <label className="field-inline">
          <span>组件</span>
          <select value={componentFilter} onChange={(e) => setComponentFilter(e.target.value)}>
            <option value="">全部</option>
            {components.map((component) => (
              <option key={component} value={component}>
                {proposalComponentLabel(component)}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">显示 {filtered.length} / {items.length}</span>
        <button className="small" onClick={() => { void load(); }} disabled={listLoading}>
          刷新
        </button>
      </div>

      {listError && <p className="error">{listError}</p>}

      <div className="roles-layout tooling-proposals-layout">
        <div className="proposal-list-pane">
          {items.length === 0 && !listLoading ? (
            <EmptyState icon="wrench" title="暂无运行时提案"
              hint="角色遇到运行时问题时，会在这里生成待审提案。" />
          ) : filtered.length === 0 ? (
            <EmptyState icon="filter" title="当前筛选没有匹配项"
              action={<button className="small" onClick={clearFilters}>查看未结束提案</button>} />
          ) : (
            <ul className="roles-list proposal-list">
              {filtered.map((proposal) => (
                <li key={proposal.id} className={selected?.id === proposal.id ? "active" : ""}>
                  <a className="proposal-row-link"
                    href={toolingHref("proposals", proposal.id)}
                    aria-current={selected?.id === proposal.id ? "page" : undefined}
                    onClick={(e) => {
                      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                      e.preventDefault();
                      openProposal(proposal.id);
                    }}>
                    <span className="proposal-row-head">
                      <strong className="proposal-row-title">{proposal.title}</strong>
                      <span className={`proposal-status is-${proposal.status}`}>
                        {proposalStatusLabel(proposal.status)}
                      </span>
                    </span>
                    <span className="proposal-row-meta">
                      <span className="mono">{proposal.id}</span>
                      <span>{proposalComponentLabel(proposal.component_type)}</span>
                      <span className={`proposal-sev is-${proposal.severity}`}>
                        {proposalSeverityLabel(proposal.severity)}
                      </span>
                      <span>证据 {proposal.evidence_count}</span>
                      <span>{formatDateTime(proposal.updated_at)}</span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="roles-detail proposal-detail-shell">
          {selected ? (
            <ProposalDetail
              key={selected.id}
              proposal={selected}
              markdown={markdown}
              loading={detailLoading}
              error={detailError}
              onTransition={transition}
              onExport={exportMarkdown}
            />
          ) : (
            <div className="role-detail-body">
              <EmptyState icon="file-text" title="选择一个提案"
                hint="从左侧列表选择提案，查看 Markdown、证据计数与人工流转。"
                action={ripId ? (
                  <button className="small" onClick={() => {
                    deepLinkRef.current = null;
                    onSelectProposal(null);
                    clearSelection();
                  }}>清除选择</button>
                ) : undefined} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
