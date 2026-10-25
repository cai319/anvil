import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Channel, type WorkspaceKnowledge } from "../../api/client";
import { EmptyState } from "../../components/ui";
import { KNOWLEDGE_STATUS_LABEL } from "../../knowledgeCard";
import { ChannelKnowledgeRow } from "./KnowledgeCardRow";
import { useDebouncedValue } from "./useDebouncedValue";

export function ChannelKnowledgeTab({ channels, onTotal }: {
  channels: Channel[];
  onTotal: (total: number) => void;
}) {
  const [cards, setCards] = useState<WorkspaceKnowledge[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebouncedValue(query);
  const [status, setStatus] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onTotalRef = useRef(onTotal);
  useEffect(() => { onTotalRef.current = onTotal; }, [onTotal]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.listAllWorkspaceKnowledge({
        q: debouncedQuery,
        status,
        workspace_id: workspaceId,
        limit: 500,
      });
      setCards(page.items);
      setTotal(page.total);
      onTotalRef.current(page.total);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [debouncedQuery, status, workspaceId]);

  useEffect(() => { void load(); }, [load]);

  const act = async (card: WorkspaceKnowledge, fn: () => Promise<unknown>) => {
    setBusy(card.id);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <div className="knowledge-scope-toolbar">
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索标题、摘要、频道或标签" aria-label="搜索频道知识" />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="按状态筛选">
          <option value="">全部状态</option>
          {Object.entries(KNOWLEDGE_STATUS_LABEL).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <select value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} aria-label="按频道筛选">
          <option value="">全部频道</option>
          {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <span className="muted knowledge-scope-count">
          共 {total} 条{total > 500 ? "，显示前 500 条" : ""}
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {loading ? (
        <p className="muted">加载中…</p>
      ) : cards.length === 0 ? (
        <EmptyState icon="book" title="暂无频道知识"
          hint="可在频道页新增知识卡，或从已完成任务收割。" />
      ) : (
        <ul className="knowledge-record-list">
          {cards.map((card) => (
            <ChannelKnowledgeRow
              key={card.id}
              card={card}
              busy={busy === card.id}
              onApprove={(item) => act(item,
                () => api.approveWorkspaceKnowledge(item.workspace_id, item.id, item.version))}
              onReject={(item) => act(item,
                () => api.rejectWorkspaceKnowledge(item.workspace_id, item.id, item.version))}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
