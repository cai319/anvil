import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type Channel,
  type KnowledgeKind,
  type RepoCard,
  type TaskKnowledge,
  type TopicRepo,
} from "../../api/client";
import { EmptyState } from "../../components/ui";
import {
  AUTHORITY_LABEL,
  KNOWLEDGE_KIND_LABEL,
  KNOWLEDGE_STATUS_LABEL,
} from "../../knowledgeCard";
import { formatDateTime } from "../../time";
import { useDebouncedValue } from "./useDebouncedValue";
import { KnowledgeCardEditor } from "./KnowledgeCardEditor";
import {
  KnowledgeCleanPreview,
  QualityBadge,
  QualityMissing,
  QualityWarnings,
} from "./KnowledgeQuality";

const STATUS_CLASS: Record<string, string> = {
  draft: "todo",
  proposed: "in_review",
  approved: "done",
  rejected: "closed",
  deprecated: "closed",
  expired: "closed",
};

const REPO_STATUS_LABEL: Record<string, string> = {
  proposed: "待批准",
  approved: "已批准",
  rejected: "已拒绝",
  deprecated: "已弃用",
  expired: "已过期",
  draft: "草稿",
};

function fmt(value?: string | null): string {
  return value ? formatDateTime(value) : "";
}

function RepoBadges({ repo }: { repo: TopicRepo }) {
  return (
    <span className="knowledge-record-badges">
      {Boolean(repo.auto_created) && <span className="chip">自动识别</span>}
      {Boolean(repo.needs_review) && <span className="review-reason">待复核</span>}
      {Boolean(repo.locked) && <span className="chip">已锁定</span>}
      {repo.pending > 0 && <span className="status status-in_review">待批 {repo.pending}</span>}
    </span>
  );
}

function RepoCardRow({ card, selected, busy, channels, llmEnabled,
  onToggle, onAct, onEdit, onClean, onFeedback, onReview, onToChannel, onToShared }: {
  card: RepoCard;
  selected?: boolean;
  busy: boolean;
  channels: Channel[];
  llmEnabled: boolean;
  onToggle?: (id: string) => void;
  onAct: (fn: () => Promise<unknown>) => void;
  onEdit: () => void;
  onClean: () => void;
  onFeedback: (signal: "helpful" | "unhelpful") => void;
  onReview: () => void;
  onToChannel: (workspaceId: string) => void;
  onToShared: () => void;
}) {
  const [channel, setChannel] = useState("");
  const [open, setOpen] = useState(false);
  const reviewDue = Boolean(card.review_at
    && card.review_at.slice(0, 10) <= new Date().toISOString().slice(0, 10));
  return (
    <li className="knowledge-record">
      <div className="knowledge-record-head">
        <div className="knowledge-record-main">
          <div className="knowledge-record-title-row">
            {onToggle && card.status === "proposed" && (
              <input type="checkbox" checked={Boolean(selected)} disabled={busy}
                aria-label={`选择 ${card.title}`}
                onChange={() => onToggle(card.id)} />
            )}
            <strong className="knowledge-record-title">{card.title}</strong>
            <span className="knowledge-record-badges">
              <span className={`status status-${STATUS_CLASS[card.status] ?? "closed"}`}>
                {KNOWLEDGE_STATUS_LABEL[card.status] ?? card.status}
              </span>
              <span className={`authority-badge authority-${card.authority}`}>
                {AUTHORITY_LABEL[card.authority] ?? card.authority}
              </span>
              <span className="chip">
                {KNOWLEDGE_KIND_LABEL[card.kind as KnowledgeKind] ?? card.kind}
              </span>
              <QualityBadge quality={card.quality} />
              <QualityMissing quality={card.quality} />
            </span>
          </div>
          <span className="knowledge-record-meta">
            <span className="mono">{card.repo}</span>
            {card.tags.map((tag) => <span key={tag}>#{tag}</span>)}
            {card.source_role && <span className="muted">来源：{card.source_role}</span>}
            {card.source_event && <span className="review-reason">{card.source_event}</span>}
            {card.source_task_id && (
              <a href={`/tasks/${card.source_task_id}`}>来源任务</a>
            )}
            <span className="knowledge-record-hits">
              {card.hit_count ? `命中 ${card.hit_count} 次` : "未命中"}
              {card.last_hit_at ? ` · 最近 ${fmt(card.last_hit_at)}` : ""}
            </span>
          </span>
          <span className="knowledge-record-tldr">{card.tldr}</span>
          <QualityWarnings quality={card.quality} />
          <button type="button" className="knowledge-record-toggle"
            onClick={() => setOpen((v) => !v)}>
            {open ? "收起知识卡" : "查看知识卡"}
          </button>
          {open && <pre className="knowledge-record-body">{card.body_md}</pre>}
        </div>
        <span className="knowledge-record-actions">
          {card.status === "proposed" && (
            <>
              <button type="button" className="small primary" disabled={busy}
                onClick={() => {
                  if ((card.quality?.blocking.length ?? 0) > 0) onEdit();
                  else onAct(() => api.approveRepoCard(card.id));
                }}>
                {(card.quality?.blocking.length ?? 0) > 0 ? "补全后批准" : "批准"}
              </button>
              <button type="button" className="small" disabled={busy}
                onClick={onEdit}>编辑</button>
              {llmEnabled && (
                <button type="button" className="small" disabled={busy}
                  onClick={onClean}>LLM 清洗</button>
              )}
              <button type="button" className="small danger" disabled={busy}
                onClick={() => onAct(() => api.rejectRepoCard(card.id))}>拒绝</button>
            </>
          )}
          {card.status === "approved" && channels.length > 0 && (
            <span className="knowledge-inline-form">
              <select value={channel} disabled={busy}
                aria-label="收割到频道"
                onChange={(e) => setChannel(e.target.value)}>
                <option value="">收割到频道…</option>
                {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
              <button type="button" className="small" disabled={busy || !channel}
                onClick={() => onToChannel(channel)}>收割</button>
            </span>
          )}
          {card.status === "approved" && (
            <>
              <button type="button" className="small" disabled={busy}
                onClick={() => onFeedback("helpful")}>有效</button>
              <button type="button" className="small" disabled={busy}
                onClick={() => onFeedback("unhelpful")}>无效</button>
              <button type="button" className="small" disabled={busy}
                onClick={onToShared}>→ 共享经验</button>
              {reviewDue && (
                <button type="button" className="small" disabled={busy}
                  onClick={onReview}>复核通过</button>
              )}
            </>
          )}
        </span>
      </div>
    </li>
  );
}

function TaskQueueRow({ card, selected, busy, repos, promote, onToggle,
  onEdit, onApprove, onPromote, onPromoteTarget }: {
  card: TaskKnowledge;
  selected: boolean;
  busy: boolean;
  repos: TopicRepo[];
  promote: string;
  onToggle: (id: string) => void;
  onEdit: () => void;
  onApprove: () => void;
  onPromote: () => void;
  onPromoteTarget: (repo: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className="knowledge-record">
      <div className="knowledge-record-head">
        <div className="knowledge-record-main">
          <div className="knowledge-record-title-row">
            <input type="checkbox" checked={selected} disabled={busy}
              aria-label={`选择 ${card.title}`} onChange={() => onToggle(card.id)} />
            <strong className="knowledge-record-title">{card.title}</strong>
            <span className="knowledge-record-badges">
              <span className="status status-in_review">待批准</span>
              <span className="chip">任务级草稿</span>
              <span className="chip">
                {KNOWLEDGE_KIND_LABEL[card.kind as KnowledgeKind] ?? card.kind}
              </span>
              <QualityBadge quality={card.quality} />
              <QualityMissing quality={card.quality} />
            </span>
          </div>
          <span className="knowledge-record-meta">
            <a href={`/tasks/${card.task_id}`}>{card.task_title || card.task_id}</a>
            {card.tags.map((tag) => <span key={tag}>#{tag}</span>)}
            {card.source_role && <span className="muted">来源：{card.source_role}</span>}
            {card.source_event && <span className="review-reason">{card.source_event}</span>}
            {card.repo_key && <span className="mono">{card.repo_key}</span>}
          </span>
          <span className="knowledge-record-tldr">{card.tldr}</span>
          <QualityWarnings quality={card.quality} />
          <button type="button" className="knowledge-record-toggle"
            onClick={() => setOpen((v) => !v)}>
            {open ? "收起知识卡" : "查看知识卡"}
          </button>
          {open && <pre className="knowledge-record-body">{card.body_md}</pre>}
        </div>
        <span className="knowledge-record-actions">
          <button type="button" className="small primary" disabled={busy}
            onClick={() => {
              if ((card.quality?.blocking.length ?? 0) > 0) onEdit();
              else onApprove();
            }}>
            {(card.quality?.blocking.length ?? 0) > 0 ? "补全后批准" : "批准"}
          </button>
          <button type="button" className="small" disabled={busy}
            onClick={onEdit}>编辑</button>
          <span className="knowledge-inline-form">
            <select value={promote} disabled={busy}
              aria-label="晋升到主题知识库"
              onChange={(e) => onPromoteTarget(e.target.value)}>
              <option value="">晋升到…</option>
              {repos.map((repo) => (
                <option key={repo.id} value={repo.display_name}>{repo.display_name}</option>
              ))}
            </select>
            <button type="button" className="small" disabled={busy || !promote}
              onClick={onPromote}>晋升</button>
          </span>
          <a className="small button-like" href={`/tasks/${card.task_id}`}>任务页</a>
        </span>
      </div>
    </li>
  );
}

function QualitySummaryPanel({
  summary, selectedRepo,
}: {
  summary: Awaited<ReturnType<typeof api.getKnowledgeQualitySummary>>;
  selectedRepo: string;
}) {
  const bucket = selectedRepo
    ? summary.repos.find((item) => item.repo === selectedRepo)
    : undefined;
  const totals = summary.totals as Record<string, unknown>;
  const avg = bucket?.avg_score ?? Number(totals.avg_score || 0);
  const total = bucket?.total ?? Number(totals.cards || 0);
  const approved = bucket?.approved ?? Number(totals.approved || 0);
  const proposed = bucket?.proposed ?? Number(totals.proposed || 0);
  const coverage = bucket?.coverage ?? { use_when: 0, expected_effect: 0, evidence: 0 };
  const kindCounts = bucket?.kind_counts ?? {};
  return (
    <div className="knowledge-quality-summary">
      <span className="knowledge-quality-score">质量 {avg}</span>
      <span>共 {total}</span>
      <span>已批准 {approved}</span>
      <span>待批 {proposed}</span>
      <span className="knowledge-quality-coverage">
        场景 {Math.round(coverage.use_when * 100)}% ·
        效果 {Math.round(coverage.expected_effect * 100)}% ·
        证据 {Math.round(coverage.evidence * 100)}%
      </span>
      {bucket && bucket.legacy > 0 && <span className="review-reason">历史 {bucket.legacy}</span>}
      {bucket && bucket.review_due > 0 && <span className="review-reason">待复核 {bucket.review_due}</span>}
      {Object.entries(kindCounts).map(([kind, count]) => (
        <span className="chip" key={kind}>
          {KNOWLEDGE_KIND_LABEL[kind as KnowledgeKind] ?? kind} {count}
        </span>
      ))}
    </div>
  );
}

export function TopicKnowledgeTab({ channels, onTotal }: {
  channels: Channel[];
  onTotal: (total: number) => void;
}) {
  const [repos, setRepos] = useState<TopicRepo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState("");
  const [cards, setCards] = useState<RepoCard[]>([]);
  const [queue, setQueue] = useState<RepoCard[]>([]);
  const [taskQueue, setTaskQueue] = useState<TaskKnowledge[]>([]);
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebouncedValue(query);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const [pickedTasks, setPickedTasks] = useState<Set<string>>(() => new Set());
  const [promoteTargets, setPromoteTargets] = useState<Record<string, string>>({});
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [qualitySummary, setQualitySummary] = useState<
    Awaited<ReturnType<typeof api.getKnowledgeQualitySummary>> | null>(null);
  const [llmEnabled, setLlmEnabled] = useState(false);
  const [editor, setEditor] = useState<{
    mode: "repo" | "task"; card: RepoCard | TaskKnowledge; taskId?: string;
  } | null>(null);
  const [cleanPreview, setCleanPreview] = useState<{
    card: RepoCard; preview: Record<string, unknown>;
  } | null>(null);
  const [cleanBusy, setCleanBusy] = useState(false);
  const onTotalRef = useRef(onTotal);
  useEffect(() => { onTotalRef.current = onTotal; }, [onTotal]);

  const selected = repos.find((r) => r.display_name === selectedRepo);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [repoList, queueResult, taskPage, cardResult, quality] = await Promise.all([
        api.listTopicRepos(),
        api.listRepoCards({ status: "proposed", q: debouncedQuery, limit: 500 }),
        api.listAllTaskKnowledge({ status: "proposed", q: debouncedQuery, limit: 500 }),
        api.listRepoCards({
          repo: selectedRepo || undefined,
          status: status === "all" ? undefined : status,
          q: debouncedQuery,
          limit: 500,
        }),
        api.getKnowledgeQualitySummary(selectedRepo || undefined),
      ]);
      setRepos(repoList.repos ?? []);
      setQueue(queueResult.cards ?? []);
      setTaskQueue(taskPage.items ?? []);
      setCards(cardResult.cards ?? []);
      setQualitySummary(quality);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [debouncedQuery, selectedRepo, status]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    api.listFeatureFlags()
      .then((data) => setLlmEnabled(Boolean(
        data.flags.find((flag) => flag.key === "knowledge_llm_cleaning")?.value)))
      .catch(() => setLlmEnabled(false));
  }, []);
  useEffect(() => {
    onTotalRef.current(queue.length + taskQueue.length);
  }, [queue.length, taskQueue.length]);

  useEffect(() => {
    // 选中的库被合并/删除后回落到“全部主题”。
    if (!loading && selectedRepo && !repos.some((r) => r.display_name === selectedRepo)) {
      setSelectedRepo("");
    }
  }, [loading, repos, selectedRepo]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
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

  const openEditor = (mode: "repo" | "task", card: RepoCard | TaskKnowledge,
                      taskId?: string) => {
    setEditor({ mode, card, taskId });
  };

  const cleanCard = async (card: RepoCard) => {
    setCleanBusy(true);
    setError(null);
    try {
      const preview = await api.cleanRepoCard(card.id, { dry_run: true });
      setCleanPreview({ card, preview });
    } catch (e) {
      setError(String(e));
    } finally {
      setCleanBusy(false);
    }
  };

  const applyClean = async () => {
    if (!cleanPreview) return;
    setCleanBusy(true);
    setError(null);
    try {
      await api.cleanRepoCard(cleanPreview.card.id, {
        dry_run: false, cleaned: cleanPreview.preview,
        runtime_id: cleanPreview.preview.runtime_id as string | undefined,
      });
      setCleanPreview(null);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setCleanBusy(false);
    }
  };

  const toggle = (set: Set<string>, id: string, apply: (next: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id); else next.add(id);
    apply(next);
  };

  const batchRepo = async (approve: boolean) => {
    const ids = [...picked].filter((id) => queue.some((c) => c.id === id));
    if (ids.length === 0) return;
    setBusy("batch-repo");
    setError(null);
    try {
      for (const id of ids) {
        await (approve ? api.approveRepoCard(id) : api.rejectRepoCard(id));
      }
      setPicked(new Set());
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const batchTask = async () => {
    const ids = [...pickedTasks].filter((id) => taskQueue.some((c) => c.id === id));
    if (ids.length === 0) return;
    setBusy("batch-task");
    setError(null);
    try {
      for (const id of ids) {
        const card = taskQueue.find((c) => c.id === id);
        if (card) await api.approveTaskKnowledge(card.task_id, card.id);
      }
      setPickedTasks(new Set());
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const createRepo = async () => {
    const name = newName.trim();
    if (!name) return;
    setBusy("create");
    setError(null);
    try {
      await api.createTopicRepo({ repo: name, description: newDesc.trim() });
      setNewName("");
      setNewDesc("");
      setCreateOpen(false);
      setSelectedRepo(name);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const lockSelected = async () => {
    if (!selected) return;
    await act("lock", () => api.lockTopicRepo(
      selected.display_name, !Boolean(selected.locked)));
  };

  const pendingTotal = queue.length + taskQueue.length;

  return (
    <div className="topic-layout">
      <aside className="topic-repos">
        <div className="topic-repos-head">
          <strong>主题知识库</strong>
          <button type="button" className="small"
            onClick={() => setCreateOpen((v) => !v)}>
            {createOpen ? "取消" : "新建"}
          </button>
        </div>
        {createOpen && (
          <div className="topic-create">
            <input value={newName} onChange={(e) => setNewName(e.target.value)}
              placeholder="知识主题名称，如 openssl" aria-label="新主题知识库名称" />
            <input value={newDesc} onChange={(e) => setNewDesc(e.target.value)}
              placeholder="一句话说明（可选）" aria-label="主题描述" />
            <button type="button" className="small primary"
              disabled={busy === "create" || !newName.trim()}
              onClick={() => { void createRepo(); }}>创建</button>
          </div>
        )}
        <ul className="roles-list topic-repo-list">
          <li className={selectedRepo === "" ? "active" : ""}>
            <button type="button" className="topic-repo-btn" onClick={() => setSelectedRepo("")}>
              <span className="topic-repo-name">全部主题</span>
              <span className="topic-repo-meta">
                {repos.length} 个主题{pendingTotal > 0 ? ` · ${pendingTotal} 待批` : ""}
              </span>
            </button>
          </li>
          {repos.map((repo) => (
            <li key={repo.id} className={selectedRepo === repo.display_name ? "active" : ""}>
              <button type="button" className="topic-repo-btn"
                onClick={() => setSelectedRepo(repo.display_name)}>
                <span className="topic-repo-name">{repo.display_name}</span>
                <span className="topic-repo-meta">
                  {repo.card_count} 卡 · {repo.task_count} 任务
                </span>
                <RepoBadges repo={repo} />
              </button>
            </li>
          ))}
        </ul>
        {repos.length === 0 && !loading && (
          <p className="muted">暂无主题知识库。任务产生的知识先进入任务级知识库，人工晋升后才会出现在这里。</p>
        )}
      </aside>

      <section className="topic-main">
        <div className="knowledge-scope-toolbar">
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索知识卡标题、摘要或标签" aria-label="搜索主题知识" />
          <select value={status} onChange={(e) => setStatus(e.target.value)}
            aria-label="按卡片状态筛选">
            <option value="all">全部状态</option>
            {Object.entries(REPO_STATUS_LABEL).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
          <span className="muted knowledge-scope-count">
            待审批 {pendingTotal} · 当前列表 {cards.length} 张
          </span>
        </div>
        <p className="muted knowledge-scope-note">
          文件是真相（<span className="mono">data/knowledge-repos/&lt;repo&gt;/cards/</span>）；
          Agent 只能提交草稿，批准、晋升、收割都必须人工操作。
        </p>
        {qualitySummary && (
          <QualitySummaryPanel summary={qualitySummary} selectedRepo={selectedRepo} />
        )}
        {error && <p className="error">{error}</p>}

        {selected && (
          <div className="topic-head">
            <div>
              <h3>{selected.display_name}</h3>
              <div className="muted">
                {selected.description || "暂无说明"}
                {selected.aliases.length > 1 ? ` · 别名：${selected.aliases.join("、")}` : ""}
              </div>
            </div>
            <div className="topic-head-actions">
              <button type="button" className="small" disabled={busy === "lock"}
                onClick={() => { void lockSelected(); }}>
                {selected.locked ? "解锁" : "锁定"}
              </button>
            </div>
          </div>
        )}

        {(queue.length > 0 || taskQueue.length > 0) && (
          <div className="topic-queue">
            <div className="topic-section-head">
              <h3>待审批队列 <span className="tab-count">{pendingTotal}</span></h3>
              <span className="topic-batch-actions">
                {queue.length > 0 && (
                  <>
                    <span className="muted">已选 {picked.size}</span>
                    <button type="button" className="small primary"
                      disabled={busy === "batch-repo" || picked.size === 0}
                      onClick={() => { void batchRepo(true); }}>批量批准主题卡</button>
                    <button type="button" className="small danger"
                      disabled={busy === "batch-repo" || picked.size === 0}
                      onClick={() => { void batchRepo(false); }}>批量拒绝</button>
                  </>
                )}
                {taskQueue.length > 0 && (
                  <>
                    <span className="muted">任务草稿已选 {pickedTasks.size}</span>
                    <button type="button" className="small primary"
                      disabled={busy === "batch-task" || pickedTasks.size === 0}
                      onClick={() => { void batchTask(); }}>批量批准任务卡</button>
                  </>
                )}
              </span>
            </div>

            {queue.length > 0 && (
              <>
                <h4 className="knowledge-list-title">主题知识库草稿 {queue.length}</h4>
                <ul className="knowledge-record-list">
                  {queue.map((card) => (
                    <RepoCardRow
                      key={card.id}
                      card={card}
                      selected={picked.has(card.id)}
                      busy={busy === card.id || busy === "batch-repo" || cleanBusy}
                      channels={channels}
                      llmEnabled={llmEnabled}
                      onToggle={(id) => toggle(picked, id, setPicked)}
                      onEdit={() => openEditor("repo", card)}
                      onClean={() => { void cleanCard(card); }}
                      onFeedback={(signal) => {
                        void act(card.id, () => api.feedbackRepoCard(card.id, { signal }));
                      }}
                      onReview={() => {
                        void act(card.id, () => api.reviewRepoCard(card.id, "keep"));
                      }}
                      onAct={(fn) => { void act(card.id, fn); }}
                      onToChannel={(workspaceId) => {
                        void act(card.id, () => api.repoCardToChannel(card.id, workspaceId));
                      }}
                      onToShared={() => {
                        void act(card.id, () => api.repoCardToShared(card.id));
                      }}
                    />
                  ))}
                </ul>
              </>
            )}

            {taskQueue.length > 0 && (
              <>
                <h4 className="knowledge-list-title">任务级草稿 {taskQueue.length}</h4>
                <ul className="knowledge-record-list">
                  {taskQueue.map((card) => (
                    <TaskQueueRow
                      key={card.id}
                      card={card}
                      selected={pickedTasks.has(card.id)}
                      busy={busy === card.id || busy === "batch-task"}
                      repos={repos}
                      promote={promoteTargets[card.id] ?? ""}
                      onToggle={(id) => toggle(pickedTasks, id, setPickedTasks)}
                      onEdit={() => openEditor("task", card, card.task_id)}
                      onApprove={() => { void act(card.id, () => api.approveTaskKnowledge(card.task_id, card.id)); }}
                      onPromoteTarget={(repo) => setPromoteTargets((prev) => ({ ...prev, [card.id]: repo }))}
                      onPromote={() => {
                        const repo = promoteTargets[card.id];
                        if (repo) {
                          void act(card.id, () => api.promoteTaskKnowledge(card.task_id, card.id, repo));
                        }
                      }}
                    />
                  ))}
                </ul>
              </>
            )}
          </div>
        )}

        <h3 className="knowledge-list-title">
          {selected ? `${selected.display_name} 的卡片` : "全部主题知识卡"} {cards.length}
        </h3>
        {loading ? (
          <p className="muted">加载中…</p>
        ) : cards.length === 0 ? (
          <EmptyState icon="book" title="暂无主题知识卡"
            hint="任务完成、失败或返工时会生成任务级草稿；批准后可晋升到这里。" />
        ) : (
          <ul className="knowledge-record-list">
            {cards.map((card) => (
              <RepoCardRow
                key={card.id}
                card={card}
                busy={busy === card.id || cleanBusy}
                channels={channels}
                llmEnabled={llmEnabled}
                onEdit={() => openEditor("repo", card)}
                onClean={() => { void cleanCard(card); }}
                onFeedback={(signal) => {
                  void act(card.id, () => api.feedbackRepoCard(card.id, { signal }));
                }}
                onReview={() => {
                  void act(card.id, () => api.reviewRepoCard(card.id, "keep"));
                }}
                onAct={(fn) => { void act(card.id, fn); }}
                onToChannel={(workspaceId) => {
                  void act(card.id, () => api.repoCardToChannel(card.id, workspaceId));
                }}
                onToShared={() => {
                  void act(card.id, () => api.repoCardToShared(card.id));
                }}
              />
            ))}
          </ul>
        )}
      </section>

      {editor && (
        <KnowledgeCardEditor
          card={editor.card}
          mode={editor.mode}
          taskId={editor.taskId}
          onClose={() => setEditor(null)}
          onSaved={() => { setEditor(null); void load(); }}
        />
      )}
      {cleanPreview && (
        <KnowledgeCleanPreview
          card={cleanPreview.card}
          preview={cleanPreview.preview}
          busy={cleanBusy}
          onClose={() => setCleanPreview(null)}
          onApply={() => { void applyClean(); }}
        />
      )}
    </div>
  );
}
