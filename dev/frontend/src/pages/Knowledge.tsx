import { useCallback, useEffect, useState } from "react";
import { api, type Channel, type Lesson, type Proposal, type Role } from "../api/client";
import { PageHeader } from "../components/ui";
import { Icon } from "../components/Icon";
import { RoleAvatar } from "../components/RoleAvatar";
import { formatDateTime } from "../time";
import { ChannelKnowledgeTab } from "./knowledge/ChannelKnowledgeTab";
import { TaskKnowledgeTab } from "./knowledge/TaskKnowledgeTab";
import { RoleKnowledgeTab } from "./knowledge/RoleKnowledgeTab";
import { KnowledgeStatsTab } from "./knowledge/KnowledgeStatsTab";

const LESSON_STATUS_LABEL: Record<Lesson["status"], string> = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已拒绝",
};

function LessonTags({ tags }: { tags: string[] }) {
  return <>{tags.length ? tags.map((t) => `#${t}`).join(" ") : "无标签"}</>;
}

/** 共享经验的提出者：角色头像；旧数据没有 source_role 时用中性占位头像。 */
function LessonSource({ lesson, roles, showName = false }: {
  lesson: Lesson;
  roles: Role[];
  showName?: boolean;
}) {
  const slug = lesson.source_role;
  const role = slug ? roles.find((r) => r.slug === slug) : undefined;
  const human = slug === "human";
  const unknown = !slug;
  const name = unknown ? "未知来源" : human ? "人类" : (role?.display_name ?? slug);
  return (
    <span className="lesson-source">
      <RoleAvatar
        slug={unknown ? "unknown" : human ? "human" : slug}
        name={unknown ? "?" : name}
        avatar={unknown || human ? "initials" : role?.avatar}
        color={unknown ? "#8b8496" : human ? "#22d3ee" : role?.avatar_color}
        size={24}
        label={`提供者：${name}`}
      />
      {showName && <span className="lesson-source-name">{name}</span>}
    </span>
  );
}

function LessonDetailModal({ lesson, roles, error, onClose, onApprove, onReject, onDelete }: {
  lesson: Lesson;
  roles: Role[];
  error: string | null;
  onClose: () => void;
  onApprove: () => void;
  onReject: () => void;
  onDelete: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        className="modal lesson-detail"
        role="dialog"
        aria-modal="true"
        aria-label="共享经验详情"
      >
        <div className="modal-head">
          <h2>共享经验详情</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭" autoFocus>
            <Icon name="x" size={12} />
          </button>
        </div>

        <p className="lesson-detail-content">{lesson.content}</p>

        <dl className="lesson-detail-meta">
          <dt>状态</dt>
          <dd>{LESSON_STATUS_LABEL[lesson.status]}</dd>
          <dt>提供者</dt>
          <dd><LessonSource lesson={lesson} roles={roles} showName /></dd>
          <dt>标签</dt>
          <dd><LessonTags tags={lesson.tags} /></dd>
          <dt>仓库</dt>
          <dd className="mono">{lesson.repo || "shared"}</dd>
          <dt>来源任务</dt>
          <dd>
            {lesson.source_task
              ? <a className="mono" href={`/tasks/${lesson.source_task}`}>{lesson.source_task}</a>
              : "—"}
          </dd>
          <dt>创建时间</dt>
          <dd className="mono">{formatDateTime(lesson.created_at)}</dd>
          <dt>批准时间</dt>
          <dd className="mono">{lesson.approved_at ? formatDateTime(lesson.approved_at) : "—"}</dd>
        </dl>

        {error && <p className="error">{error}</p>}

        <div className="modal-actions">
          <button type="button" onClick={onClose}>关闭</button>
          {lesson.status === "pending" && (
            <>
              <button type="button" className="danger" onClick={onReject}>拒绝</button>
              <button type="button" className="primary" onClick={onApprove}>批准</button>
            </>
          )}
          {lesson.status === "approved" && (
            <button type="button" className="danger" onClick={onDelete}>删除</button>
          )}
        </div>
      </div>
    </div>
  );
}

function LessonProposeModal({ content, tags, repo, repoOptions, error, busy, onContentChange, onTagsChange, onRepoChange, onClose, onSubmit }: {
  content: string;
  tags: string;
  repo: string;
  repoOptions: string[];
  error: string | null;
  busy: boolean;
  onContentChange: (value: string) => void;
  onTagsChange: (value: string) => void;
  onRepoChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => event.target === event.currentTarget && !busy && onClose()}
    >
      <div
        className="modal lesson-propose"
        role="dialog"
        aria-modal="true"
        aria-label="提议升格共享经验"
      >
        <div className="modal-head">
          <h2>提议升格共享经验</h2>
          <button className="modal-close" onClick={onClose} disabled={busy} aria-label="关闭">
            <Icon name="x" size={12} />
          </button>
        </div>

        <label className="field">
          <span>经验内容</span>
          <textarea
            value={content}
            onChange={(e) => onContentChange(e.target.value)}
            rows={4}
            autoFocus
            placeholder="要共享给所有角色的经验…"
          />
        </label>
        <label className="field">
          <span>标签</span>
          <input
            value={tags}
            onChange={(e) => onTagsChange(e.target.value)}
            placeholder="逗号分隔，例如 anvil,reminder"
          />
        </label>
        <label className="field">
          <span>仓库</span>
          <select value={repo} onChange={(e) => onRepoChange(e.target.value)}>
            {(repoOptions.includes(repo) ? repoOptions : [repo, ...repoOptions]).map((option) => (
              <option key={option} value={option}>
                {option === "shared" ? "shared（全局）" : option}
              </option>
            ))}
          </select>
          <small>
            shared = 全角色、全仓库通用；选择具体仓库时，只对该仓库的任务生效。
          </small>
        </label>

        {error && <p className="error">{error}</p>}

        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={busy}>取消</button>
          <button
            type="button"
            className="primary"
            onClick={onSubmit}
            disabled={!content.trim() || busy}
          >
            {busy ? "提交中…" : "提交提议"}
          </button>
        </div>
      </div>
    </div>
  );
}

function LessonList({ lessons, roles, empty, dateKey, onOpen, onDelete }: {
  lessons: Lesson[];
  roles: Role[];
  empty: string;
  dateKey: "created_at" | "approved_at";
  onOpen: (lesson: Lesson) => void;
  onDelete?: (lesson: Lesson) => void;
}) {
  if (lessons.length === 0) return <p className="muted">{empty}</p>;
  return (
    <ul className="lesson-list">
      {lessons.map((lesson) => (
        <li key={lesson.id} className="lesson-row-wrap">
          <button type="button" className="lesson-row" onClick={() => onOpen(lesson)}>
            <LessonSource lesson={lesson} roles={roles} />
            <span className="lesson-row-main">
              <span className="lesson-row-title">{lesson.content}</span>
              <span className="lesson-row-meta">
                <LessonTags tags={lesson.tags} />
                <span>repo={lesson.repo || "shared"}</span>
              </span>
            </span>
            <span className="lesson-row-date">{formatDateTime(lesson[dateKey]) || "—"}</span>
            <span className="lesson-row-chevron" aria-hidden="true">
              <Icon name="chevron-right" size={14} />
            </span>
          </button>
          {onDelete && lesson.status === "approved" && (
            <button
              type="button"
              className="lesson-row-delete"
              onClick={() => onDelete(lesson)}
              aria-label={`删除共享经验：${lesson.content.slice(0, 30)}`}
              title="删除"
            >
              <Icon name="trash" size={14} />
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

type Scope = "channel" | "task" | "role" | "shared" | "proposals" | "stats";

const SCOPES: { key: Scope; label: string }[] = [
  { key: "channel", label: "频道知识" },
  { key: "task", label: "任务知识" },
  { key: "role", label: "角色知识" },
  { key: "shared", label: "共享经验" },
  { key: "proposals", label: "演化提案" },
  { key: "stats", label: "效果评估" },
];

export function Knowledge() {
  const [scope, setScope] = useState<Scope>("channel");
  const [visited, setVisited] = useState<Set<Scope>>(() => new Set(["channel"]));
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [pending, setPending] = useState<Lesson[]>([]);
  const [approved, setApproved] = useState<Lesson[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [repoOptions, setRepoOptions] = useState<string[]>(["shared"]);
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [repo, setRepo] = useState("shared");
  const [query, setQuery] = useState("");
  const [proposeOpen, setProposeOpen] = useState(false);
  const [proposing, setProposing] = useState(false);
  const [detail, setDetail] = useState<Lesson | null>(null);
  const [error, setError] = useState<string | null>(null);

  const setChannelTotal = useCallback(
    (n: number) => setCounts((prev) => ({ ...prev, channel: n })), []);
  const setTaskTotal = useCallback(
    (n: number) => setCounts((prev) => ({ ...prev, task: n })), []);
  const setRoleTotal = useCallback(
    (n: number) => setCounts((prev) => ({ ...prev, role: n })), []);

  useEffect(() => {
    setVisited((prev) => (prev.has(scope) ? prev : new Set(prev).add(scope)));
  }, [scope]);

  const refresh = () =>
    Promise.all([
      api.listLessons("pending"),
      api.listLessons("approved"),
      api.listProposals(),
      api.listRoles(),
      api.listTasks().catch(() => []),
      api.listChannels().catch(() => [] as Channel[]),
      api.listKnowledgeRepos().catch(() => ({ repos: [], total: 0, enabled: false })),
    ])
      .then(([p, a, props, rs, tasks, chans, catalog]) => {
        setPending(p);
        setApproved(a);
        setProposals(props);
        setRoles(rs);
        setChannels(chans);
        const names = new Set<string>(["shared"]);
        for (const item of catalog.repos ?? []) if (item.repo) names.add(item.repo);
        for (const lesson of [...p, ...a]) if (lesson.repo) names.add(lesson.repo);
        for (const task of tasks) if (task.repo) names.add(task.repo);
        for (const channel of chans) if (channel.repo) names.add(channel.repo);
        setRepoOptions([...names].sort((x, y) => (
          x === "shared" ? -1 : y === "shared" ? 1 : x.localeCompare(y)
        )));
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const propose = async () => {
    if (!content.trim() || proposing) return;
    setProposing(true);
    setError(null);
    try {
      await api.proposeLesson(
        content.trim(),
        tags.split(",").map((t) => t.trim()).filter(Boolean),
        repo.trim() || "shared",
      );
      setContent("");
      setTags("");
      setRepo("shared");
      setProposeOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setProposing(false);
    }
  };

  const openPropose = () => {
    setError(null);
    setProposeOpen(true);
  };

  const closePropose = () => {
    setProposeOpen(false);
    setError(null);
  };

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const openDetail = (lesson: Lesson) => {
    setError(null);
    setDetail(lesson);
  };

  const closeDetail = () => {
    setDetail(null);
    setError(null);
  };

  const actDetail = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setDetail(null);
      setError(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const removeDetail = (lesson: Lesson) => {
    if (!window.confirm("确定删除该条经验？删除后不可恢复。")) return;
    void actDetail(() => api.deleteLesson(lesson.id));
  };

  const removeLesson = (lesson: Lesson) => {
    if (!window.confirm("确定删除该条经验？删除后不可恢复。")) return;
    void act(() => api.deleteLesson(lesson.id));
  };

  const proposerLabel = (slug: string | null) => {
    if (!slug) return "未知来源";
    if (slug === "human") return "人类 human";
    const role = roles.find((r) => r.slug === slug);
    return `${role?.display_name ?? slug} ${slug}`;
  };
  const needle = query.trim().toLowerCase();
  const matchesQuery = (lesson: Lesson) => !needle || [
    lesson.content,
    proposerLabel(lesson.source_role),
    ...lesson.tags,
    ...lesson.tags.map((t) => `#${t}`),
    lesson.repo,
  ].join(" ").toLowerCase().includes(needle);
  const filteredPending = pending.filter(matchesQuery);
  const filteredApproved = approved.filter(matchesQuery);
  const sharedCount = pending.length + approved.length;
  const pendingProposals = proposals.filter((p) => p.status === "pending").length;

  const tabCount = (key: Scope): number | undefined => {
    if (key === "channel") return counts.channel;
    if (key === "task") return counts.task;
    if (key === "role") return counts.role;
    if (key === "shared") return sharedCount;
    if (key === "proposals") return pendingProposals;
    return undefined;
  };

  return (
    <div className="knowledge-page">
      <PageHeader
        kicker="KNOWLEDGE"
        title="知识库"
        subtitle="频道 / 任务 / 角色 / 共享经验"
        actions={scope === "shared" ? (
          <button className="primary propose-btn" onClick={openPropose}>
            <Icon name="plus" size={14} /> 提议升格
          </button>
        ) : undefined}
      />
      {error && <p className="error">{error}</p>}

      <div className="tabs knowledge-tabs">
        {SCOPES.map((item) => {
          const count = tabCount(item.key);
          return (
            <button
              key={item.key}
              className={scope === item.key ? "active" : ""}
              onClick={() => setScope(item.key)}
            >
              {item.label}
              {count !== undefined && count > 0 && (
                <span className="tab-count">{count}</span>
              )}
            </button>
          );
        })}
      </div>

      <div className="knowledge-scope-body">
        {visited.has("channel") && (
          <div hidden={scope !== "channel"}>
            <ChannelKnowledgeTab channels={channels} onTotal={setChannelTotal} />
          </div>
        )}
        {visited.has("task") && (
          <div hidden={scope !== "task"}>
            <TaskKnowledgeTab channels={channels} onTotal={setTaskTotal} />
          </div>
        )}
        {visited.has("role") && (
          <div hidden={scope !== "role"}>
            <RoleKnowledgeTab roles={roles} onTotal={setRoleTotal} />
          </div>
        )}
        {visited.has("shared") && (
          <div hidden={scope !== "shared"}>
            <div className="knowledge-scope-toolbar">
              <input
                type="search"
                className="knowledge-search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索内容或提议人（角色名 / slug）"
                aria-label="搜索共享经验"
              />
              {query && (
                <button type="button" className="small" onClick={() => setQuery("")}>清除</button>
              )}
              <span className="muted knowledge-scope-count">
                待审批 {pending.length} · 已批准 {approved.length}
              </span>
            </div>
            <h3 className="knowledge-list-title">
              待审批 {pending.length}
              {needle && ` · 匹配 ${filteredPending.length}`}
            </h3>
            <LessonList
              lessons={filteredPending}
              roles={roles}
              empty={needle ? "没有匹配的待审经验。" : "没有待审经验。"}
              dateKey="created_at"
              onOpen={openDetail}
            />
            <h3 className="knowledge-list-title">
              已批准 {approved.length}
              {needle && ` · 匹配 ${filteredApproved.length}`}
            </h3>
            <LessonList
              lessons={filteredApproved}
              roles={roles}
              empty={needle ? "没有匹配的已批准经验。" : "暂无已批准经验。"}
              dateKey="approved_at"
              onOpen={openDetail}
              onDelete={removeLesson}
            />
          </div>
        )}
        {visited.has("proposals") && (
          <div hidden={scope !== "proposals"}>
            <div className="knowledge-scope-toolbar">
              <span className="muted">
                共 {proposals.length} 条 · {pendingProposals} 条待审
              </span>
            </div>
            <ul className="drafts">
              {proposals.length === 0 && <li className="muted">暂无提案 —— Agent 可用 `anvil self propose` 提交。</li>}
              {proposals.map((p) => (
                <li key={p.id}>
                  <strong>{p.role_slug}</strong> · <span className="mono">{p.field}</span>{" "}
                  <span className={`status status-${p.status === "pending" ? "in_review" : p.status === "approved" ? "done" : "closed"}`}>
                    {p.status}
                  </span>
                  <pre className="run-log">{JSON.stringify(p.proposed, null, 2)}</pre>
                  {p.rationale && <div className="muted">依据：{p.rationale}</div>}
                  {p.risk && <div className="muted">风险：{p.risk}</div>}
                  {p.status === "pending" && (
                    <div className="card-actions">
                      <button onClick={() => act(() => api.approveProposal(p.id))}>批准并应用</button>
                      <button className="danger" onClick={() => act(() => api.rejectProposal(p.id))}>拒绝</button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {visited.has("stats") && (
          <div hidden={scope !== "stats"}>
            <KnowledgeStatsTab />
          </div>
        )}
      </div>

      {detail && (
        <LessonDetailModal
          lesson={detail}
          roles={roles}
          error={error}
          onClose={closeDetail}
          onApprove={() => actDetail(() => api.approveLesson(detail.id))}
          onReject={() => actDetail(() => api.rejectLesson(detail.id))}
          onDelete={() => removeDetail(detail)}
        />
      )}

      {proposeOpen && (
        <LessonProposeModal
          content={content}
          tags={tags}
          repo={repo}
          repoOptions={repoOptions}
          error={error}
          busy={proposing}
          onContentChange={setContent}
          onTagsChange={setTags}
          onRepoChange={setRepo}
          onClose={closePropose}
          onSubmit={() => { void propose(); }}
        />
      )}
    </div>
  );
}
