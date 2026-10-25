import { useEffect, useMemo, useState } from "react";
import {
  api,
  type Channel,
  type KnowledgeAuthority,
  type KnowledgeCardCheck,
  type KnowledgeKind,
  type KnowledgeReview,
  type Participant,
  type Role,
  type Task,
  type WorkspaceKnowledge,
} from "../api/client";
import { PageHeader } from "../components/ui";
import {
  AUTHORITY_LABEL,
  KNOWLEDGE_CARD_FIELDS,
  KNOWLEDGE_KINDS,
  KNOWLEDGE_KIND_LABEL,
  KNOWLEDGE_STATUS_LABEL,
} from "../knowledgeCard";

type Tab = "overview" | "knowledge" | "assets" | "participants" | "settings";

const REVIEW_REASON_LABEL: Record<string, string> = {
  expired: "已过期",
  review_due: "复核到期",
  stale: "长期未复核",
  conflict: "冲突未裁决",
  low_hit: "长期未命中",
};

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "概览" },
  { key: "knowledge", label: "知识" },
  { key: "assets", label: "资产" },
  { key: "participants", label: "参与角色" },
  { key: "settings", label: "设置" },
];

export function Channels() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [participants, setParticipants] = useState<Participant[]>([]);
  const [knowledge, setKnowledge] = useState<WorkspaceKnowledge[]>([]);
  const [review, setReview] = useState<KnowledgeReview | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [repo, setRepo] = useState("");
  const [description, setDescription] = useState("");
  const [knowledgeRepo, setKnowledgeRepo] = useState("");
  const [roleSlug, setRoleSlug] = useState("");

  const [cardTitle, setCardTitle] = useState("");
  const [cardTldr, setCardTldr] = useState("");
  const [cardKind, setCardKind] = useState<KnowledgeKind>("convention");
  const [cardAuthority, setCardAuthority] = useState<KnowledgeAuthority>("normative");
  const [cardFields, setCardFields] = useState<Record<string, string>>({});
  const [cardCheck, setCardCheck] = useState<KnowledgeCardCheck | null>(null);
  const [harvestTask, setHarvestTask] = useState("");

  const selectedChannel = channels.find((c) => c.id === selected) ?? null;

  const refreshChannels = () =>
    api.listChannels().then(setChannels).catch((e) => setError(String(e)));

  const refreshDetail = (id: string) =>
    Promise.all([
      api.listParticipants(id),
      api.listWorkspaceKnowledge(id),
      api.reviewWorkspaceKnowledge(id),
      api.listTasks({ workspace_id: id, include_archived: true }),
    ])
      .then(([p, k, r, t]) => {
        setParticipants(p);
        setKnowledge(k);
        setReview(r);
        setTasks(t);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    // 支持从任务详情深链：/channels?channel=<id>&tab=knowledge
    const params = new URLSearchParams(window.location.search);
    const wanted = params.get("channel");
    const wantedTab = params.get("tab") as Tab | null;
    if (wanted) setSelected(wanted);
    if (wantedTab && TABS.some((t) => t.key === wantedTab)) setTab(wantedTab);
    refreshChannels();
    api.listRoles().then(setRoles).catch(() => {});
  }, []);

  useEffect(() => {
    if (!selected) return;
    refreshDetail(selected);
    setRoleSlug("");
    setHarvestTask("");
  }, [selected]);

  useEffect(() => {
    setRepo(selectedChannel?.repo ?? "");
    setDescription(selectedChannel?.description ?? "");
    setKnowledgeRepo(selectedChannel?.knowledge_repo ?? "");
  }, [selectedChannel?.id, selectedChannel?.repo, selectedChannel?.description, selectedChannel?.knowledge_repo]);

  useEffect(() => {
    if (cardKind === "convention") setCardAuthority("normative");
    else setCardAuthority("reference");
    setCardFields({});
    setCardCheck(null);
  }, [cardKind]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      if (selected) await refreshDetail(selected);
      await refreshChannels();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const createChannel = () =>
    run(async () => {
      if (!name.trim()) return;
      const c = await api.createChannel(name.trim());
      setName("");
      setSelected(c.id);
      setTab("overview");
    });

  const saveChannel = () =>
    run(async () => {
      if (!selected) return;
      await api.updateChannel(selected, {
        repo: repo.trim() || null,
        description: description.trim() || null,
        knowledge_repo: knowledgeRepo.trim() || null,
      });
    });

  const addParticipant = () =>
    run(async () => {
      if (!selected || !roleSlug) return;
      await api.addParticipant(selected, roleSlug);
      setRoleSlug("");
    });

  const removeParticipant = (slug: string) =>
    run(async () => {
      if (!selected) return;
      await api.removeParticipant(selected, slug);
    });

  const cardFieldsPayload = () => Object.fromEntries(
    Object.entries(cardFields).filter(([, v]) => String(v || "").trim())
  );

  const validateCard = async () => {
    setCardCheck(null);
    try {
      setCardCheck(await api.validateKnowledgeCard({
        kind: cardKind,
        fields: cardFieldsPayload(),
        for_approval: true,
      }));
    } catch (e) {
      setError(String(e));
    }
  };

  const createCard = () =>
    run(async () => {
      if (!selected || !cardTitle.trim() || !cardTldr.trim()) return;
      await api.createWorkspaceKnowledge(selected, {
        title: cardTitle.trim(),
        tldr: cardTldr.trim(),
        kind: cardKind,
        authority: cardAuthority,
        fields: cardFieldsPayload(),
      });
      setCardTitle("");
      setCardTldr("");
      setCardFields({});
      setCardCheck(null);
    });

  const approveCard = (card: WorkspaceKnowledge) =>
    run(() => api.approveWorkspaceKnowledge(card.workspace_id, card.id, card.version));
  const rejectCard = (card: WorkspaceKnowledge) =>
    run(() => api.rejectWorkspaceKnowledge(card.workspace_id, card.id, card.version));
  const deleteCard = (card: WorkspaceKnowledge) =>
    run(() => api.deleteWorkspaceKnowledge(card.workspace_id, card.id));
  const resolveConflicts = (card: WorkspaceKnowledge) =>
    run(() => api.resolveWorkspaceKnowledgeConflicts(card.workspace_id, card.id));

  const harvest = () =>
    run(async () => {
      if (!selected || !harvestTask) return;
      await api.harvestWorkspaceKnowledge(selected, harvestTask);
      setHarvestTask("");
    });

  const deleteChannel = () => {
    if (!selected || !window.confirm("删除频道？任务会解绑，频道知识会删除。")) return;
    run(async () => {
      await api.deleteChannel(selected);
      setSelected(null);
    });
  };

  const doneTasks = useMemo(
    () => tasks.filter((t) => t.status === "done" || t.status === "closed"),
    [tasks]
  );

  return (
    <div className="roles">
      <PageHeader kicker="CHANNELS" title="频道" />
      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="频道名" />
        <button onClick={createChannel} disabled={busy}>建频道</button>
      </div>

      <div className="roles-layout">
        <ul className="roles-list">
          {channels.length === 0 && <li className="muted">暂无频道，先在上方新建。</li>}
          {channels.map((c) => (
            <li key={c.id} className={selected === c.id ? "active" : ""}>
              <a href="#" onClick={(e) => { e.preventDefault(); setSelected(c.id); }}>
                {c.name} <small>({c.participant_count ?? c.member_count} 参与 · {c.task_count} 任务)</small>
              </a>
            </li>
          ))}
        </ul>

        <div className="roles-detail">
          {!selectedChannel ? (
            <p className="muted">选择左侧频道管理知识、资产与参与角色。</p>
          ) : (
            <>
              <h2>{selectedChannel.name}</h2>
              <div className="toolbar" style={{ flexWrap: "wrap" }}>
                {TABS.map((t) => (
                  <button key={t.key} className={tab === t.key ? "primary" : ""}
                    onClick={() => setTab(t.key)}>{t.label}</button>
                ))}
              </div>

              {tab === "overview" && (
                <>
                  <div className="channel-meta-form">
                    <label>
                      执行 repo
                      <input value={repo} onChange={(e) => setRepo(e.target.value)}
                        placeholder="频道默认/领域 repo；任务可不同" />
                    </label>
                    <label>
                      知识 repo
                      <input value={knowledgeRepo} onChange={(e) => setKnowledgeRepo(e.target.value)}
                        placeholder="频道知识卡所属 knowledge repo" />
                    </label>
                    <label>
                      描述
                      <textarea value={description} onChange={(e) => setDescription(e.target.value)}
                        rows={3} placeholder="这个频道负责什么、涉及哪些技术领域" />
                    </label>
                    <button type="button" onClick={saveChannel} disabled={busy}>保存频道信息</button>
                  </div>
                  <h2>最近任务</h2>
                  <p className="muted">
                    {tasks.length} 个任务（{tasks.filter((t) => !t.archived_at).length} 个未归档）。
                    任务始终在任务大厅查看与筛选。
                  </p>
                  <ul className="roles-list">
                    {tasks.slice(0, 5).map((t) => (
                      <li key={t.id}>
                        <a href={`/tasks/${t.id}`}>{t.title}</a>{" "}
                        <small>{t.status}{t.archived_at ? " · 已归档" : ""}</small>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              {tab === "knowledge" && (
                <>
                  <h2>新增知识卡</h2>
                  <div className="channel-meta-form">
                    <label>标题
                      <input value={cardTitle} onChange={(e) => setCardTitle(e.target.value)} />
                    </label>
                    <label>TL;DR
                      <input value={cardTldr} onChange={(e) => setCardTldr(e.target.value)} />
                    </label>
                    <label>类型
                      <select value={cardKind} onChange={(e) => setCardKind(e.target.value as KnowledgeKind)}>
                        {KNOWLEDGE_KINDS.map((k) => <option key={k} value={k}>{KNOWLEDGE_KIND_LABEL[k]}</option>)}
                      </select>
                    </label>
                    <label>约束力
                      <select value={cardAuthority} onChange={(e) => setCardAuthority(e.target.value as KnowledgeAuthority)}>
                        <option value="normative">normative（仅 convention，绑定频道时约束）</option>
                        <option value="reference">reference</option>
                      </select>
                    </label>
                    {KNOWLEDGE_CARD_FIELDS[cardKind].map((f) => (
                      <label key={f.key}>{f.label}
                        <textarea rows={2} value={cardFields[f.key] || ""}
                          onChange={(e) => setCardFields({ ...cardFields, [f.key]: e.target.value })} />
                      </label>
                    ))}
                    <div className="toolbar">
                      <button type="button" onClick={validateCard} disabled={busy}>
                        校验完整度
                      </button>
                      <button onClick={createCard} disabled={busy}>创建知识卡</button>
                      {cardCheck && (
                        <span className="muted">
                          完整度 {Math.round(cardCheck.completeness * 100)}%
                        </span>
                      )}
                    </div>
                    {cardCheck?.errors.map((msg) => (
                      <p key={msg} className="error">{msg}</p>
                    ))}
                    {cardCheck?.warnings.map((msg) => (
                      <p key={msg} className="muted">⚠ {msg}</p>
                    ))}
                  </div>

                  <h2>从任务收割</h2>
                  <div className="toolbar">
                    <select value={harvestTask} onChange={(e) => setHarvestTask(e.target.value)}>
                      <option value="">选择已完成任务</option>
                      {doneTasks.map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
                    </select>
                    <button onClick={harvest} disabled={busy || !harvestTask}>收割</button>
                  </div>

                  <h2>知识复盘</h2>
                  {!review || review.total === 0 ? (
                    <p className="muted">暂无过期、待复核、冲突或长期未命中的知识卡。</p>
                  ) : (
                    <ul className="roles-list">
                      {review.items.map(({ card, reasons }) => (
                        <li key={card.id} style={{ display: "block" }}>
                          <strong>{card.title}</strong>{" "}
                          {reasons.map((reason) => (
                            <span key={reason} className="review-reason">
                              {REVIEW_REASON_LABEL[reason] ?? reason}
                            </span>
                          ))}
                          <small className="muted">
                            {" "}新鲜度 {(card.freshness ?? 0).toFixed(2)}
                          </small>
                          <div className="toolbar">
                            {reasons.includes("conflict") && (
                              <button onClick={() => resolveConflicts(card)} disabled={busy}>
                                冲突已裁决
                              </button>
                            )}
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}

                  <h2>频道知识</h2>
                  <ul className="roles-list">
                    {knowledge.length === 0 && <li className="muted">暂无频道知识。</li>}
                    {knowledge.map((k) => (
                      <li key={k.id} style={{ display: "block" }}>
                        <strong>{k.title}</strong>{" "}
                        <small>
                          <span className={`authority-badge authority-${k.authority}`}>
                            {AUTHORITY_LABEL[k.authority]}
                          </span>{" "}
                          {KNOWLEDGE_KIND_LABEL[k.kind] ?? k.kind} ·
                          {KNOWLEDGE_STATUS_LABEL[k.status] ?? k.status} ·
                          完整度 {Math.round(k.completeness * 100)}%
                          {k.expires_at ? ` · 过期 ${k.expires_at.slice(0, 10)}` : ""}
                          {k.is_expired ? " · 已过期（不注入）" : ""}
                          {k.review_at ? ` · 复核 ${k.review_at.slice(0, 10)}` : ""}
                          {k.conflicts.length > 0 ? ` · 冲突 ${k.conflicts.length} 条` : ""}
                        </small>
                        <div>{k.tldr}</div>
                        <details>
                          <summary>查看知识卡</summary>
                          <pre style={{ whiteSpace: "pre-wrap" }}>{k.body_md}</pre>
                        </details>
                        <div className="toolbar">
                          {k.status === "proposed" && (
                            <>
                              <button onClick={() => approveCard(k)} disabled={busy}>批准</button>
                              <button className="danger" onClick={() => rejectCard(k)} disabled={busy}>拒绝</button>
                            </>
                          )}
                          {k.conflicts.length > 0 && (
                            <button onClick={() => resolveConflicts(k)} disabled={busy}>
                              冲突已裁决
                            </button>
                          )}
                          <button className="danger" onClick={() => deleteCard(k)} disabled={busy}>删除</button>
                        </div>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              {tab === "assets" && (
                <>
                  <h2>频道资产</h2>
                  <ul className="roles-list">
                    <li>执行 repo：{selectedChannel.repo || "未绑定"}</li>
                    <li>知识 repo：{selectedChannel.knowledge_repo || "未绑定"}</li>
                    <li>任务数：{tasks.length}（未归档 {tasks.filter((t) => !t.archived_at).length}）</li>
                    <li>知识卡：{knowledge.length}（已批准 {knowledge.filter((k) => k.status === "approved").length}）</li>
                  </ul>
                </>
              )}

              {tab === "participants" && (
                <>
                  <h2>参与角色</h2>
                  <p className="muted">仅作自动派发偏好与展示，不是权限；Cindy/claim 不受此限制。</p>
                  <div className="toolbar">
                    <select value={roleSlug} onChange={(e) => setRoleSlug(e.target.value)}>
                      <option value="">选择角色</option>
                      {roles.map((r) => <option key={r.slug} value={r.slug}>{r.display_name || r.slug}</option>)}
                    </select>
                    <button onClick={addParticipant} disabled={busy || !roleSlug}>加入</button>
                  </div>
                  <ul className="roles-list">
                    {participants.length === 0 && <li className="muted">暂无参与角色。</li>}
                    {participants.map((p) => (
                      <li key={p.role_slug}>
                        <span>{p.role_slug}</span>
                        <button className="danger" onClick={() => removeParticipant(p.role_slug)} disabled={busy}>移除</button>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              {tab === "settings" && (
                <>
                  <h2>设置</h2>
                  <p className="muted">删除频道会解绑任务（不删除任务），并删除频道知识与参与角色。</p>
                  <button className="danger" onClick={deleteChannel} disabled={busy}>删除频道</button>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
