import { useCallback, useEffect, useState } from "react";
import {
  api, type KnowledgeKind, type KnowledgeRepoBinding, type KnowledgeRepoProfile,
  type Task, type TaskKnowledge, type WorkspaceKnowledge,
} from "../api/client";
import {
  AUTHORITY_LABEL, KNOWLEDGE_CARD_FIELDS, KNOWLEDGE_QUALITY_FIELDS,
  KNOWLEDGE_KINDS, KNOWLEDGE_KIND_LABEL,
} from "../knowledgeCard";
import { UNBOUND_CHANNEL_VIEW } from "../channelView";
import { KnowledgeDetailModal } from "../pages/knowledge/KnowledgeDetail";

const REPO_SOURCE_LABEL: Record<string, string> = {
  auto: "自动",
  cindy: "Cindy",
  human: "人工",
  inherited: "继承",
  agent: "Agent",
};

export const KNOWLEDGE_MODE_LABEL: Record<string, string> = {
  shared: "主题知识库",
  local: "任务级知识库",
  temp: "临时知识空间",
};

const REPO_STATE_HINT: Record<string, string> = {
  applied: "已按任务标签/文本自动关联。",
  suggested: "低置信度建议，确认后才会参与检索。",
  needs_cindy: "任务还没有标签，启动后由 Cindy 识别。",
  fallback: "未匹配到主题知识库，回退到任务级/临时知识空间 + shared。",
  locked: "已人工固定，自动识别和 Cindy 不会覆盖。",
  off: "知识仓库功能已关闭。",
};

export function KnowledgeRepoPanel({ task, onChanged }: { task: Task; onChanged: () => void }) {
  const [repos, setRepos] = useState<KnowledgeRepoBinding[]>(task.knowledge_repos ?? []);
  const [locked, setLocked] = useState(Boolean(task.knowledge_binding_locked));
  const [state, setState] = useState<string | null>(task.knowledge_binding_state ?? null);
  const [scope, setScope] = useState<string[] | null>(null);
  const [catalog, setCatalog] = useState<KnowledgeRepoProfile[]>([]);
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((payload: {
    locked?: boolean; state?: string | null; repos?: KnowledgeRepoBinding[];
    scope?: { repos: string[] | null };
  }) => {
    if (payload.repos) setRepos(payload.repos);
    if (payload.locked !== undefined) setLocked(Boolean(payload.locked));
    if (payload.state !== undefined) setState(payload.state);
    if (payload.scope) setScope(payload.scope.repos);
  }, []);

  const repoKey = JSON.stringify(task.knowledge_repos ?? []);
  useEffect(() => {
    setRepos(task.knowledge_repos ?? []);
    setLocked(Boolean(task.knowledge_binding_locked));
    setState(task.knowledge_binding_state ?? null);
  }, [repoKey, task.knowledge_binding_locked, task.knowledge_binding_state]);

  useEffect(() => {
    setRepos(task.knowledge_repos ?? []);
    setLocked(Boolean(task.knowledge_binding_locked));
    setState(task.knowledge_binding_state ?? null);
    api.getTaskKnowledgeRepos(task.id)
      .then((d) => apply(d))
      .catch(() => {});
    api.listKnowledgeRepos()
      .then((d) => setCatalog(d.repos ?? []))
      .catch(() => setCatalog([]));
  }, [task.id, apply]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const visible = repos.filter((r) => r.status !== "dismissed");
  const applied = visible.filter((r) => r.status === "applied");
  const suggested = visible.filter((r) => r.status === "suggested");

  return (
    <>
      <h3 className="side-h">知识主题</h3>
      <div className="repo-panel">
        {visible.length === 0 && (
          <p className="muted hint">{REPO_STATE_HINT[state ?? ""] ?? "尚未关联主题知识库。"}</p>
        )}
        <div className="repo-chips">
          {[...applied, ...suggested].map((r) => (
            <span
              key={`${r.repo}-${r.status}`}
              className={`repo-chip repo-${r.status}`}
              title={r.reason ?? undefined}
            >
              <span className="repo-chip-name">{r.repo}</span>
              <span className="repo-chip-meta">
                {REPO_SOURCE_LABEL[r.source] ?? r.source}
                {r.status === "suggested" ? " · 建议" : ""}
              </span>
              {r.status === "suggested" && (
                <span className="repo-chip-actions">
                  <button type="button" disabled={busy}
                    onClick={() => run(async () => {
                      const d = await api.acceptTaskKnowledgeRepo(task.id, r.repo) as {
                        locked: boolean; state: string; repos: KnowledgeRepoBinding[]; scope: { repos: string[] | null };
                      };
                      apply(d);
                    })}>采纳</button>
                  <button type="button" disabled={busy}
                    onClick={() => run(async () => {
                      const d = await api.dismissTaskKnowledgeRepo(task.id, r.repo) as {
                        locked: boolean; state: string; repos: KnowledgeRepoBinding[]; scope: { repos: string[] | null };
                      };
                      apply(d);
                    })}>忽略</button>
                </span>
              )}
            </span>
          ))}
        </div>
        {state && (
          <p className="muted hint">
            {REPO_STATE_HINT[state] ?? state}
            {scope?.length ? ` 检索范围：${scope.join("、")}。` : ""}
          </p>
        )}
        {locked && <p className="repo-lock">已人工固定</p>}
        {!editing ? (
          <div className="repo-actions">
            <button type="button" className="small" disabled={busy}
              onClick={() => {
                setSelected(applied.map((r) => r.repo));
                setEditing(true);
              }}>编辑</button>
            <button type="button" className="small" disabled={busy}
              onClick={() => run(async () => {
                const d = await api.reautoTaskKnowledgeRepos(task.id) as {
                  locked: boolean; state: string | null; repos: KnowledgeRepoBinding[]; scope: { repos: string[] | null };
                };
                apply(d);
              })}>重新识别</button>
          </div>
        ) : (
          <div className="repo-editor">
            <select
              multiple
              value={selected}
              onChange={(e) => setSelected(
                Array.from(e.currentTarget.selectedOptions).map((o) => o.value))}
            >
              {catalog.map((p) => (
                <option key={p.key} value={p.repo}>{p.repo}</option>
              ))}
            </select>
            <div className="repo-actions">
              <button type="button" className="small" disabled={busy}
                onClick={() => run(async () => {
                  const next = await api.patchTask(task.id, {
                    knowledge_repos: selected.map((repo) => ({ repo })),
                  });
                  setRepos(next.knowledge_repos ?? []);
                  setLocked(Boolean(next.knowledge_binding_locked));
                  setState(next.knowledge_binding_state ?? null);
                  setEditing(false);
                })}>保存并固定</button>
              <button type="button" className="small" disabled={busy}
                onClick={() => setEditing(false)}>取消</button>
            </div>
          </div>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    </>
  );
}

/** 绑定频道的任务显示已批准频道知识：约定为“约束”，其余为“参考”。 */
export function ChannelKnowledgePanel({ task, onChanged }: { task: Task; onChanged: () => void }) {
  const [cards, setCards] = useState<WorkspaceKnowledge[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkspaceKnowledge | null>(null);
  const workspaceId = task.workspace_id ?? null;

  const load = useCallback(() => {
    if (!workspaceId) {
      setCards([]);
      return;
    }
    api.listWorkspaceKnowledge(workspaceId, { status: "approved" })
      .then(setCards)
      .catch(() => setCards([]));
  }, [workspaceId]);

  useEffect(() => {
    load();
    setNote(null);
    setError(null);
  }, [load, task.id]);

  const harvest = async () => {
    if (!workspaceId) return;
    setBusy(true);
    setError(null);
    try {
      const card = await api.harvestWorkspaceKnowledge(workspaceId, task.id);
      setNote(card.status === "approved"
        ? "已收割到频道知识。"
        : "已生成频道知识草稿，等待人工批准。");
      load();
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const terminal = task.status === "done" || task.status === "closed";
  return (
    <>
      <h3 className="side-h">频道知识</h3>
      {!workspaceId ? (
        <p className="muted hint">
          任务未归属频道，不注入频道约定。{" "}
          <a href={`/channels?channel=${UNBOUND_CHANNEL_VIEW}`}>查看未绑定任务</a>
        </p>
      ) : (
        <div className="channel-knowledge-panel">
          {cards.length === 0 && <p className="muted hint">频道暂无已批准知识。</p>}
          <ul className="side-list channel-knowledge-list">
            {cards.map((k) => (
              <li key={k.id} title={k.tldr}>
                <span className={`authority-badge authority-${k.authority}`}>
                  {AUTHORITY_LABEL[k.authority]}
                </span>
                <span className="channel-knowledge-title">{k.title}</span>
                <small className="muted">{KNOWLEDGE_KIND_LABEL[k.kind] ?? k.kind}</small>
                <button type="button" className="small" onClick={() => setDetail(k)}>
                  详情
                </button>
              </li>
            ))}
          </ul>
          {terminal && (
            <button type="button" className="small" disabled={busy} onClick={harvest}>
              {busy ? "收割中…" : "收割到频道"}
            </button>
          )}
          {note && <p className="muted hint">{note}</p>}
          {error && <p className="error">{error}</p>}
          {detail && (
            <KnowledgeDetailModal
              heading="频道知识详情"
              title={detail.title}
              kind={detail.kind}
              tldr={detail.tldr}
              fields={detail.fields}
              body={detail.body_md}
              quality={detail.quality}
              meta={[
                { label: "约束力", value: AUTHORITY_LABEL[detail.authority] ?? detail.authority },
                { label: "类型", value: KNOWLEDGE_KIND_LABEL[detail.kind] ?? detail.kind },
                { label: "版本", value: `v${detail.version}` },
                { label: "标签", value: detail.tags.length ? detail.tags.join("、") : "无" },
                { label: "来源任务", value: detail.source_task_id || "—" },
                { label: "复核", value: detail.review_at || "—" },
              ]}
              onClose={() => setDetail(null)}
            />
          )}
        </div>
      )}
    </>
  );
}

/** 任务共享知识：任务参与角色共享；子任务来源显式标记。 */
export function TaskKnowledgePanel({ task }: { task: Task }) {
  const [cards, setCards] = useState<TaskKnowledge[]>([]);
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<KnowledgeKind>("task_finding");
  const [title, setTitle] = useState("");
  const [tldr, setTldr] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskKnowledge | null>(null);

  const load = useCallback(
    () => api.listTaskKnowledge(task.id, true).then(setCards).catch(() => setCards([])),
    [task.id],
  );

  useEffect(() => { load(); }, [load]);
  useEffect(() => { setFields({}); }, [kind]);

  const submit = async () => {
    if (!title.trim() || !tldr.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createTaskKnowledge(task.id, {
        title: title.trim(),
        tldr: tldr.trim(),
        kind,
        fields: Object.fromEntries(
          Object.entries(fields).filter(([, v]) => String(v || "").trim())
        ),
      });
      setTitle("");
      setTldr("");
      setFields({});
      setOpen(false);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (kid: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteTaskKnowledge(task.id, kid);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3 className="side-h">任务共享知识</h3>
      <div className="task-knowledge-panel">
        {cards.length === 0 && <p className="muted hint">暂无任务知识。</p>}
        <ul className="side-list task-knowledge-list">
          {cards.map((k) => (
            <li key={k.id}>
              <span className="channel-knowledge-title">{k.title}</span>
              <small className="muted">
                {KNOWLEDGE_KIND_LABEL[k.kind] ?? k.kind}
                {k.source_subtask_id ? ` · 来自子任务 ${k.source_subtask_id}` : " · 任务共享"}
              </small>
              <span className="muted">{k.tldr}</span>
              <button type="button" className="small" onClick={() => setDetail(k)}>
                详情
              </button>
              <button type="button" className="small danger" disabled={busy}
                onClick={() => remove(k.id)}>删除</button>
            </li>
          ))}
        </ul>
        {open ? (
          <div className="channel-meta-form">
            <label>标题
              <input value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <label>TL;DR
              <input value={tldr} onChange={(e) => setTldr(e.target.value)} />
            </label>
            <label>类型
              <select value={kind} onChange={(e) => setKind(e.target.value as KnowledgeKind)}>
                {KNOWLEDGE_KINDS.map((k) => (
                  <option key={k} value={k}>{KNOWLEDGE_KIND_LABEL[k]}</option>
                ))}
              </select>
            </label>
            <p className="muted hint">
              知识提交前必须条款：标题、TLDR 与该类型全部必填字段必须完整；
              缺失会被服务端拒绝（422）。
            </p>
            {[...KNOWLEDGE_QUALITY_FIELDS,
              ...KNOWLEDGE_CARD_FIELDS[kind]].map((f) => (
              <label key={f.key}>{f.label}
                <textarea rows={2} value={fields[f.key] || ""}
                  onChange={(e) => setFields({ ...fields, [f.key]: e.target.value })} />
              </label>
            ))}
            <div className="toolbar">
              <button type="button" className="primary" disabled={busy || !title.trim() || !tldr.trim()}
                onClick={submit}>写入任务知识</button>
              <button type="button" className="small" onClick={() => setOpen(false)}>取消</button>
            </div>
          </div>
        ) : (
          <button type="button" className="small" onClick={() => setOpen(true)}>添加任务知识</button>
        )}
        {error && <p className="error">{error}</p>}
        {detail && (
          <KnowledgeDetailModal
            heading="任务知识详情"
            title={detail.title}
            kind={detail.kind}
            tldr={detail.tldr}
            fields={detail.fields}
            body={detail.body_md}
            quality={detail.quality}
            meta={[
              { label: "类型", value: KNOWLEDGE_KIND_LABEL[detail.kind] ?? detail.kind },
              { label: "状态", value: detail.status },
              { label: "标签", value: detail.tags.length ? detail.tags.join("、") : "无" },
              { label: "来源角色", value: detail.source_role || "—" },
              { label: "子任务", value: detail.source_subtask_id || "—" },
              { label: "命中", value: `${detail.hit_count ?? 0} 次` },
            ]}
            onClose={() => setDetail(null)}
          />
        )}
      </div>
    </>
  );
}
