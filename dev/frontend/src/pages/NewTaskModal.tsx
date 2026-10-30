import { useEffect, useRef, useState } from "react";
import { api, isOnboardingRole, type Channel, type Role, type Task, type WorkflowTemplate } from "../api/client";
import { RoleAvatar } from "../components/RoleAvatar";
import { Icon } from "../components/Icon";

/** 新建任务弹窗（参考 multica/raft：名称 + 描述 + 可滚动角色多选）。 */
export function NewTaskModal({ roles, roleStatus, channels = [], defaultChannelId, onClose, onCreated }: {
  roles: Role[];
  roleStatus?: Record<string, string>;
  channels?: Channel[];
  defaultChannelId?: string | null;
  onClose: () => void;
  onCreated: (task: Task) => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [seeds, setSeeds] = useState<WorkflowTemplate[]>([]);
  const [seedId, setSeedId] = useState("");
  const [channelOptions, setChannelOptions] = useState<Channel[]>(channels);
  const [channelId, setChannelId] = useState(defaultChannelId ?? "");
  const [newChannelName, setNewChannelName] = useState("");
  const [issueBoard, setIssueBoard] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const composingRef = useRef(false);

  const candidates = roles.filter((r) => !isOnboardingRole(r));
  const selectedChannel = channelOptions.find((c) => c.id === channelId) ?? null;

  useEffect(() => {
    setChannelOptions(channels);
  }, [channels]);

  useEffect(() => {
    api.listWorkflowTemplates(undefined, "active").then((list) => {
      setSeeds(list.filter((item) => item.published_version
        && (item.use_mode === "seed" || item.use_mode === "both")));
    }).catch(() => setSeeds([]));
  }, []);

  const toggle = (slug: string) =>
    setSelected((prev) => prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug]);

  const createChannelInline = async () => {
    if (!newChannelName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const channel = await api.createChannel(newChannelName.trim());
      setChannelOptions((prev) => [...prev, channel]);
      setChannelId(channel.id);
      setNewChannelName("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!title.trim()) { setError("请填写任务名称"); return; }
    setBusy(true);
    setError(null);
    try {
      const seed = seeds.find((item) => item.id === seedId);
      const task = await api.createTask(title.trim(), {
        description: description.trim() || undefined,
        assignees: selected.length ? selected : undefined,
        workspace_id: channelId || undefined,
        plan_seed: seed?.published_version ? {
          template_id: seed.id,
          version: seed.published_version,
          name: seed.name,
        } : undefined,
        issue_board_enabled: issueBoard || undefined,
      });
      onCreated(task);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="新建任务">
        <div className="modal-head">
          <h2>新建任务</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭"><Icon name="x" size={12} /></button>
        </div>

        <label className="field">
          <span>任务名称</span>
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="例如：开发一个五子棋 Web 程序"
            onCompositionStart={() => { composingRef.current = true; }}
            onCompositionEnd={() => { composingRef.current = false; }}
            onKeyDown={(e) => {
              // 中文输入法用 Enter 上屏英文/候选词时，不能触发创建任务。
              if (e.nativeEvent.isComposing || composingRef.current || e.keyCode === 229) return;
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
        </label>

        <label className="field">
          <span>任务描述</span>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={4}
            placeholder="目标、验收标准、约束…（角色会读到这段描述）" />
        </label>

        <div className="field">
          <span>所属频道</span>
          <div className="toolbar" style={{ flexWrap: "wrap" }}>
            <select value={channelId} onChange={(e) => setChannelId(e.target.value)}>
              <option value="">无频道</option>
              {channelOptions.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
            <input value={newChannelName} onChange={(e) => setNewChannelName(e.target.value)}
              placeholder="新频道名（可留空）" />
            <button type="button" onClick={createChannelInline}
              disabled={busy || !newChannelName.trim()}>新建频道</button>
          </div>
          <small className="muted">
            {selectedChannel?.repo
              ? `未填写执行 repo 时将继承频道 repo：${selectedChannel.repo}`
              : "频道未绑定 repo；任务不会自动继承 repo。"}
            {" "}子任务必须继承父任务结构标记，不能覆盖。
          </small>
        </div>

        <label className="field">
          <span>工作流模板种子（可选）</span>
          <select value={seedId} onChange={(e) => setSeedId(e.target.value)}>
            <option value="">不使用模板，由 Cindy 自由规划</option>
            {seeds.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · v{item.published_version}
              </option>
            ))}
          </select>
          <small className="muted">
            模板只作为 Cindy 的规划骨架；Cindy 会结合任务和角色调整，最终草案仍需你审批。
          </small>
        </label>

        <label className="field finding-board-toggle">
          <span className="finding-board-toggle-head">
            <input type="checkbox" checked={issueBoard}
              onChange={(e) => setIssueBoard(e.target.checked)} />
            <span>启用问题板（安全审计 findings）</span>
          </span>
          <small className="muted">
            启用后任务详情会增加「问题板」页签，收集本任务排查出的安全问题。
            Cindy 会从已选角色中指定审核者；没有合适角色时会绑定现有角色或提案新建（仍需你批准）。
            所有问题分析完成并由你逐条确认真实/误报后，任务才能最终通过。
          </small>
        </label>

        <div className="field">
          <span>参与成员（{selected.length} 已选）</span>
          <div className="member-list">
            {candidates.length === 0 && <p className="muted">暂无角色，请先在角色工坊创建。</p>}
            {candidates.map((r) => {
              const desc = typeof r.definition?.description === "string" ? r.definition.description : "";
              const picked = selected.includes(r.slug);
              return (
                <button
                  type="button"
                  key={r.slug}
                  className={`member-row ${picked ? "picked" : ""}`}
                  onClick={() => toggle(r.slug)}
                  aria-pressed={picked}
                >
                  <RoleAvatar
                    slug={r.slug}
                    name={r.display_name}
                    avatar={r.avatar}
                    color={r.avatar_color}
                    size={30}
                    status={roleStatus?.[r.slug] ?? "idle"}
                  />
                  <span className="member-main">
                    <span className="member-line">
                      <span className="member-name">{r.display_name}</span>
                      <span className="member-slug">{r.slug}</span>
                    </span>
                    {desc && <span className="member-desc">{desc}</span>}
                  </span>
                  <span className="member-check" aria-hidden="true">
                    {picked ? "✓" : ""}
                  </span>
                </button>
              );
            })}
          </div>
          <small className="muted">
            多角色任务派发时，会先由规划助理 Cindy 分析并设计工作流，草案经你批准后按依赖执行。
            任务创建后还会按标签自动关联知识仓库；没有标签时由 Cindy 识别，关联结果可在任务详情查看和修改。
          </small>
        </div>

        {error && <p className="error">{error}</p>}

        <div className="modal-actions">
          <button onClick={onClose} disabled={busy}>取消</button>
          <button className="primary" onClick={submit} disabled={busy || !title.trim()}>
            {busy ? "创建中…" : "创建任务"}
          </button>
        </div>
      </div>
    </div>
  );
}
