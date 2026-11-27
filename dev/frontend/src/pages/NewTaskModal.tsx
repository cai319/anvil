import { useEffect, useRef, useState } from "react";
import { api, isOnboardingRole, type Channel, type Role, type Task, type WorkflowTemplate } from "../api/client";
import { RoleAvatar } from "../components/RoleAvatar";
import { Hint } from "../components/Hint";
import { Icon } from "../components/Icon";
import type { StatusTone } from "../roleStatus";

/** 新建任务弹窗（参考 multica/raft：名称 + 描述 + 可滚动角色多选）。 */
export function NewTaskModal({ roles, roleStatus, channels = [], defaultChannelId, onClose, onCreated }: {
  roles: Role[];
  roleStatus?: Record<string, StatusTone>;
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

  const candidates = roles.filter((r) => !r.deleted && !isOnboardingRole(r));
  const selectedChannel = channelOptions.find((c) => c.id === channelId) ?? null;
  const channelHint = (selectedChannel?.repo
    ? `未填写执行 repo 时将继承频道 repo：${selectedChannel.repo}。`
    : "频道未绑定 repo；任务不会自动继承 repo。")
    + " 子任务必须继承父任务结构标记，不能覆盖。";

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
      <div className="modal modal-flex new-task-modal" role="dialog" aria-label="新建任务">
        <div className="modal-head">
          <h2>新建任务</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭"><Icon name="x" size={12} /></button>
        </div>

        <div className="modal-flex-body new-task-body">
        <div className="new-task-main">
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

        <div className="ntm-section-head">
          <h3>执行配置</h3>
        </div>

        <div className="field">
          <span className="field-label">所属频道 <Hint text={channelHint} /></span>
          <div className="new-task-channel-row">
            <select value={channelId} onChange={(e) => setChannelId(e.target.value)}>
              <option value="">无频道</option>
              {channelOptions.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
            <div className="new-task-channel-actions">
              <input value={newChannelName} onChange={(e) => setNewChannelName(e.target.value)}
                placeholder="新频道名（可留空）" />
              <button type="button" onClick={createChannelInline}
                disabled={busy || !newChannelName.trim()}>新建频道</button>
            </div>
          </div>
        </div>

        <label className="field">
          <span className="field-label">工作流模板种子（可选）
            <Hint text="模板只作为 Cindy 的规划骨架；Cindy 会结合任务和角色调整，最终草案仍需你审批。" />
          </span>
          <select value={seedId} onChange={(e) => setSeedId(e.target.value)}>
            <option value="">不使用模板，由 Cindy 自由规划</option>
            {seeds.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · v{item.published_version}
              </option>
            ))}
          </select>
        </label>

        <label className="field finding-board-toggle">
          <span className="finding-board-toggle-head">
            <input type="checkbox" checked={issueBoard}
              onChange={(e) => setIssueBoard(e.target.checked)} />
            <span className="field-label">启用问题板（安全审计 findings）
              <Hint text="启用后任务详情会增加「问题板」页签，收集本任务排查出的安全问题。Cindy 会从已选角色中指定审核者；没有合适角色时会绑定现有角色或提案新建（仍需你批准）。所有问题分析完成并由你逐条确认真实/误报后，任务才能最终通过。" />
            </span>
          </span>
        </label>

        {error && <p className="error">{error}</p>}
        </div>

        <aside className="new-task-side new-task-members">
          <div className="ntm-section-head">
            <h3>参与成员</h3>
            <span className="ntm-count">{selected.length} 已选</span>
            {selected.length > 0 && (
              <button type="button" className="ntm-clear"
                onClick={() => setSelected([])}>清空</button>
            )}
            <Hint text="选中角色会成为任务 Roster（必须参与）：Cindy 规划时必须把每个角色安排进步骤或 loop 阶段，不会自动忽略；角色按自身职责、运行时能力和并发上限参与，可被 @，其运行与成本计入本任务。多角色任务启动时先由 Cindy 澄清并规划，草案经你批准后执行；不选角色则先由 Cindy 分析并建议角色，需你批准后加入。选得过多会增加协调和等待成本。" />
          </div>
          <div className="member-list">
            {candidates.length === 0 && <p className="muted">暂无角色，请先在角色工坊创建。</p>}
            {candidates.map((r) => {
              const desc = typeof r.definition?.description === "string" ? r.definition.description : "";
              const picked = selected.includes(r.slug);
              return (
                <div
                  key={r.slug}
                  className={`member-row ${picked ? "picked" : ""}`}
                  role="button"
                  tabIndex={0}
                  aria-pressed={picked}
                  onClick={() => toggle(r.slug)}
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget) return;
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggle(r.slug);
                    }
                  }}
                >
                  <RoleAvatar
                    slug={r.slug}
                    name={r.display_name}
                    avatar={r.avatar}
                    color={r.avatar_color}
                    size={30}
                    tone={roleStatus?.[r.slug] ?? "gray"}
                  />
                  <span className="member-main">
                    <span className="member-line">
                      <span className="member-name">{r.display_name}</span>
                      <span className="member-slug">{r.slug}</span>
                    </span>
                  </span>
                  {desc && <Hint text={desc} />}
                  <span className="member-check" aria-hidden="true">
                    {picked ? "✓" : ""}
                  </span>
                </div>
              );
            })}
          </div>
        </aside>
        </div>

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
