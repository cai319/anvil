import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Channel, type TaskKnowledge } from "../../api/client";
import { EmptyState } from "../../components/ui";
import { TaskKnowledgeRow } from "./KnowledgeCardRow";
import { useDebouncedValue } from "./useDebouncedValue";

export function TaskKnowledgeTab({ channels, onTotal }: {
  channels: Channel[];
  onTotal: (total: number) => void;
}) {
  const [cards, setCards] = useState<TaskKnowledge[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebouncedValue(query);
  const [status, setStatus] = useState("active");
  const [workspaceId, setWorkspaceId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const onTotalRef = useRef(onTotal);
  useEffect(() => { onTotalRef.current = onTotal; }, [onTotal]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.listAllTaskKnowledge({
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

  return (
    <div>
      <div className="knowledge-scope-toolbar">
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索知识标题、摘要或任务名" aria-label="搜索任务知识" />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="按状态筛选">
          <option value="active">进行中任务</option>
          <option value="archived">已归档任务</option>
          <option value="all">全部</option>
        </select>
        <select value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} aria-label="按频道筛选">
          <option value="">全部频道</option>
          {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <span className="muted knowledge-scope-count">
          共 {total} 条{total > 500 ? "，显示前 500 条" : ""}
        </span>
      </div>
      <p className="muted knowledge-scope-note">
        任务知识用于任务内共享；进入 CIC / one-shot 上下文时计入命中，列表浏览不计。
      </p>
      {error && <p className="error">{error}</p>}
      {loading ? (
        <p className="muted">加载中…</p>
      ) : cards.length === 0 ? (
        <EmptyState icon="book" title="暂无任务共享知识"
          hint="任务参与角色可在任务详情页写入任务知识。" />
      ) : (
        <ul className="knowledge-record-list">
          {cards.map((card) => <TaskKnowledgeRow key={card.id} card={card} />)}
        </ul>
      )}
    </div>
  );
}
