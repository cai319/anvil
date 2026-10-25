import { useEffect, useRef, useState } from "react";
import {
  api, type KnowledgeHit, type Role, type RoleKnowledgeIndex,
} from "../../api/client";
import { EmptyState } from "../../components/ui";
import { formatDateTime } from "../../time";
import { useDebouncedValue } from "./useDebouncedValue";

const NOTE_KIND_LABEL: Record<string, string> = {
  task_snapshot: "任务快照",
  vulnerability_pattern: "漏洞模式",
  failure_scenario: "故障场景",
  module_knowledge: "模块知识",
  decision: "决策",
  note: "笔记",
};

export function RoleKnowledgeTab({ roles, onTotal }: {
  roles: Role[];
  onTotal: (total: number) => void;
}) {
  const [roleSlug, setRoleSlug] = useState("");
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebouncedValue(query);
  const [index, setIndex] = useState<RoleKnowledgeIndex | null>(null);
  const [hits, setHits] = useState<KnowledgeHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const onTotalRef = useRef(onTotal);
  useEffect(() => { onTotalRef.current = onTotal; }, [onTotal]);

  useEffect(() => {
    if (!roleSlug && roles.length > 0) setRoleSlug(roles[0].slug);
  }, [roles, roleSlug]);

  useEffect(() => {
    if (!roleSlug) return;
    let alive = true;
    setLoading(true);
    setError(null);
    const needle = debouncedQuery.trim();
    const run = async () => {
      try {
        if (needle) {
          const result = await api.searchRoleKnowledge(roleSlug, needle, 30);
          if (!alive) return;
          setHits(result.results);
          setIndex(null);
        } else {
          const result = await api.getRoleKnowledgeIndex(roleSlug);
          if (!alive) return;
          setIndex(result);
          setHits([]);
          onTotalRef.current(result.total_role);
        }
      } catch (e) {
        if (alive) setError(String(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void run();
    return () => { alive = false; };
  }, [roleSlug, debouncedQuery]);

  if (roles.length === 0) {
    return <EmptyState icon="users" title="暂无角色" hint="请先在角色工坊创建角色。" />;
  }

  return (
    <div>
      <div className="knowledge-scope-toolbar">
        <select value={roleSlug} onChange={(e) => setRoleSlug(e.target.value)} aria-label="选择角色">
          {roles.map((r) => (
            <option key={r.slug} value={r.slug}>{r.display_name || r.slug}</option>
          ))}
        </select>
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索角色 notes；留空显示最近笔记" aria-label="搜索角色知识" />
        <a className="small button-like" href={`/roles/${encodeURIComponent(roleSlug)}`}>
          打开角色工坊
        </a>
        <span className="muted knowledge-scope-count">
          {index ? `角色笔记 ${index.total_role}` : hits.length > 0 ? `匹配 ${hits.length}` : ""}
        </span>
      </div>
      <p className="muted">
        角色知识私有，仅作人工审计；完整文件和版本在角色工坊查看，不会导入其他角色的知识库。
      </p>
      {error && <p className="error">{error}</p>}
      {loading ? (
        <p className="muted">加载中…</p>
      ) : query.trim() ? (
        hits.length === 0 ? (
          <EmptyState icon="search" title="没有匹配的角色知识" hint="换一个关键词试试。" />
        ) : (
          <ul className="knowledge-record-list">
            {hits.map((hit) => (
              <li key={hit.id} className="knowledge-record">
                <div className="knowledge-record-main">
                  <span className="knowledge-record-title mono">{hit.path}</span>
                  <span className="knowledge-record-meta">
                    <span className="chip">{NOTE_KIND_LABEL[hit.kind] ?? hit.kind}</span>
                    {hit.repo && <span>repo={hit.repo}</span>}
                    {hit.updated_at && <span>{formatDateTime(hit.updated_at)}</span>}
                  </span>
                  <span className="knowledge-record-tldr">{hit.snippet}</span>
                </div>
              </li>
            ))}
          </ul>
        )
      ) : index ? (
        <>
          <div className="knowledge-role-stats">
            <span className="chip">角色笔记 {index.total_role}</span>
            {Object.entries(index.role_counts).map(([kind, count]) => (
              <span key={kind} className="chip">{NOTE_KIND_LABEL[kind] ?? kind} {count}</span>
            ))}
          </div>
          <h3 className="knowledge-list-title">最近笔记</h3>
          {index.recent.length === 0 ? (
            <EmptyState icon="book" title="该角色暂无笔记"
              hint="角色通过实践后的提炼写入 notes。" />
          ) : (
            <ul className="knowledge-record-list">
              {index.recent.map((note) => (
                <li key={note.id} className="knowledge-record">
                  <div className="knowledge-record-main">
                    <span className="knowledge-record-title">{note.path}</span>
                    <span className="knowledge-record-meta">
                      <span className="chip">{NOTE_KIND_LABEL[note.kind] ?? note.kind}</span>
                      {note.repo && <span>repo={note.repo}</span>}
                      {note.updated_at && <span>{formatDateTime(note.updated_at)}</span>}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <EmptyState icon="book" title="暂无角色知识" hint="选择角色后查看其最近笔记。" />
      )}
    </div>
  );
}
