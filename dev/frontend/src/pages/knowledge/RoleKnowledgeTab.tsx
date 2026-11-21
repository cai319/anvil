import { useEffect, useRef, useState } from "react";
import {
  api, type KnowledgeHit, type Role, type RoleKnowledgeIndex,
} from "../../api/client";
import { EmptyState } from "../../components/ui";
import { confirmDialog } from "../../components/dialogs";
import { CompletionFlag } from "../../components/CompletionFlag";
import {
  completionActionLabel,
  completionBlocked,
  completionLabel,
} from "../../knowledgeCard";
import { formatDateTime } from "../../time";
import { useDebouncedValue } from "./useDebouncedValue";
import { KnowledgeDetailModal } from "./KnowledgeDetail";

const NOTE_KIND_LABEL: Record<string, string> = {
  task_snapshot: "任务快照",
  vulnerability_pattern: "漏洞模式",
  failure_scenario: "故障场景",
  module_knowledge: "模块知识",
  decision: "决策",
  note: "笔记",
};

function RepoSource({ repo, display }: {
  repo?: string | null;
  display?: string | null;
}) {
  if (!repo && !display) return null;
  return (
    <span className="repo-source"
      title="写入时所在任务的知识主题，仅用于溯源与相关性排序；不代表知识归属、访问权限或存储绑定。">
      来源主题：{display || repo}
    </span>
  );
}

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
  const [detail, setDetail] = useState<KnowledgeHit | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [completionNotice, setCompletionNotice] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
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
  }, [roleSlug, debouncedQuery, refreshTick]);

  const openDetail = async (note: { id: string }) => {
    try {
      setCompletionNotice("");
      setDetail(await api.getRoleKnowledge(roleSlug, note.id));
    } catch (e) {
      setError(String(e));
    }
  };

  const requestCompletion = async () => {
    if (!detail) return;
    setRequesting(true);
    setError(null);
    try {
      const result = await api.requestRoleKnowledgeCompletion(roleSlug, detail.id);
      setDetail({ ...detail, completion: result.completion });
      setCompletionNotice(
        result.status === "already_complete"
          ? "该知识已满足质量要求，无需补全。"
          : result.status === "already_queued"
            ? "补全请求已在排队/处理中。"
            : result.queued
              ? `角色 ${result.role} 正在工作，补全请求已排队；当前工作结束后会用自身上下文精炼该知识。`
              : `已通知角色 ${result.role} 补全/精炼该知识；完成后刷新详情即可查看。`);
      setRefreshTick((value) => value + 1);
    } catch (e) {
      setError(String(e));
    } finally {
      setRequesting(false);
    }
  };

  const removeNote = (note: { id: string; path?: string }) => {
    void confirmDialog({
      title: "删除角色知识？",
      message: `将删除 ${note.path || "该条知识"} 的索引与角色 home 文件，且不可恢复。`,
      confirmLabel: "删除",
      danger: true,
    }).then(async (ok) => {
      if (!ok) return;
      setDeletingId(note.id);
      setError(null);
      try {
        await api.deleteRoleKnowledge(roleSlug, note.id);
        setRefreshTick((value) => value + 1);
      } catch (e) {
        setError(String(e));
      } finally {
        setDeletingId(null);
      }
    });
  };

  if (roles.length === 0) {
    return <EmptyState icon="users" title="暂无角色" hint="请先在角色工坊创建角色。" />;
  }

  const detailCompletion = detail ? completionLabel(detail.completion) : "";
  const detailBlocked = detail
    ? completionBlocked(detail.ready, detail.completion)
    : false;
  const showDetailCompletion = Boolean(detailCompletion)
    && !(detail?.completion?.state === "completed" && detail?.ready === true);

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
        角色知识归该角色私有，仅作人工审计；来源主题只用于溯源与相关性排序，
        不是知识库绑定，也不会改变归属。完整文件和版本在角色工坊查看，不会导入其他角色的知识库。
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
                <div className="knowledge-record-head">
                  <div className="knowledge-record-main">
                    <span className="knowledge-record-title-row knowledge-title-line">
                      <span className="knowledge-record-title mono">{hit.path}</span>
                      <CompletionFlag completion={hit.completion} />
                    </span>
                    <span className="knowledge-record-meta">
                      <span className="chip">{NOTE_KIND_LABEL[hit.kind] ?? hit.kind}</span>
                      <RepoSource repo={hit.repo} display={hit.repo_display} />
                      {hit.updated_at && <span>{formatDateTime(hit.updated_at)}</span>}
                      <span className="knowledge-record-hits"
                        title="角色知识被检索命中并加入上下文的次数">
                        {hit.hit_count ? `命中 ${hit.hit_count} 次` : "未命中"}
                        {hit.last_hit_at ? ` · 最近命中 ${formatDateTime(hit.last_hit_at)}` : ""}
                      </span>
                    </span>
                    <span className="knowledge-record-tldr">{hit.snippet}</span>
                  </div>
                  <span className="knowledge-record-actions">
                    <button type="button" className="small"
                      onClick={() => { void openDetail(hit); }}>查看详情</button>
                    <button type="button" className="small danger"
                      disabled={deletingId === hit.id}
                      onClick={() => removeNote(hit)}>删除</button>
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )
      ) : index ? (
        <>
          <div className="knowledge-role-stats">
            <span className="chip">归属：角色私有</span>
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
                  <div className="knowledge-record-head">
                    <div className="knowledge-record-main">
                      <span className="knowledge-record-title-row knowledge-title-line">
                        <span className="knowledge-record-title">{note.path}</span>
                        <CompletionFlag completion={note.completion} />
                      </span>
                      <span className="knowledge-record-meta">
                        <span className="chip">{NOTE_KIND_LABEL[note.kind] ?? note.kind}</span>
                        <RepoSource repo={note.repo} display={note.repo_display} />
                        {note.updated_at && <span>{formatDateTime(note.updated_at)}</span>}
                        <span className="knowledge-record-hits"
                          title="角色知识被检索命中并加入上下文的次数">
                          {note.hit_count ? `命中 ${note.hit_count} 次` : "未命中"}
                          {note.last_hit_at ? ` · 最近命中 ${formatDateTime(note.last_hit_at)}` : ""}
                        </span>
                      </span>
                    </div>
                    <span className="knowledge-record-actions">
                      <button type="button" className="small"
                        onClick={() => { void openDetail(note); }}>查看详情</button>
                      <button type="button" className="small danger"
                        disabled={deletingId === note.id}
                        onClick={() => removeNote(note)}>删除</button>
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
      {detail && (
        <KnowledgeDetailModal
          heading="角色知识详情"
          title={detail.title || detail.path}
          kind={detail.kind}
          tldr={detail.tldr}
          fields={detail.fields}
          body={detail.content || detail.snippet}
          error={error}
          meta={[
            { label: "路径", value: <span className="mono">{detail.path}</span> },
            { label: "类型", value: NOTE_KIND_LABEL[detail.kind] ?? detail.kind },
            { label: "来源主题", value: detail.repo_display || detail.repo || "—" },
            { label: "标签", value: detail.tags.length ? detail.tags.join("、") : "无" },
            { label: "更新时间", value: formatDateTime(detail.updated_at || "") || "—" },
            { label: "命中", value: `${detail.hit_count ?? 0} 次` },
          ]}
          onClose={() => { setDetail(null); setCompletionNotice(""); }}
          actions={(
            <>
              <button type="button" onClick={() => { setDetail(null); setCompletionNotice(""); }}>
                关闭
              </button>
              {detail.kind !== "task_snapshot" && (
                <button type="button" className="primary"
                  disabled={requesting || detailBlocked}
                  title={detail.ready ? "已满足质量要求"
                    : detail.completion?.active ? detailCompletion : undefined}
                  onClick={() => { void requestCompletion(); }}>
                  {requesting ? "通知中…"
                    : completionActionLabel(
                      detail.ready, detail.completion,
                      "让角色智能补全", "已满足质量要求")}
                </button>
              )}
            </>
          )}
        >
          {showDetailCompletion && (
            <p className={`lesson-completion-notice ${
              detail.completion?.state === "failed" ? "is-error" : ""
            }`}>
              {detailCompletion}
              {detail.completion?.role_slug ? ` · ${detail.completion.role_slug}` : ""}
            </p>
          )}
          {completionNotice && <p className="lesson-completion-notice">{completionNotice}</p>}
        </KnowledgeDetailModal>
      )}
    </div>
  );
}
