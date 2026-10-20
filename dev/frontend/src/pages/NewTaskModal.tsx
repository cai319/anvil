import { useState } from "react";
import { api, isOnboardingRole, type Role, type Task } from "../api/client";

const AVATAR_COLORS = ["#6d7cff", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa"];
function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

/** 新建任务弹窗（参考 multica/raft：名称 + 描述 + 可滚动角色多选）。 */
export function NewTaskModal({ roles, onClose, onCreated }: {
  roles: Role[];
  onClose: () => void;
  onCreated: (task: Task) => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const candidates = roles.filter((r) => !isOnboardingRole(r));

  const toggle = (slug: string) =>
    setSelected((prev) => prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug]);

  const submit = async () => {
    if (!title.trim()) { setError("请填写任务名称"); return; }
    setBusy(true);
    setError(null);
    try {
      const task = await api.createTask(title.trim(), {
        description: description.trim() || undefined,
        assignees: selected.length ? selected : undefined,
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
          <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        </div>

        <label className="field">
          <span>任务名称</span>
          <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)}
            placeholder="例如：开发一个五子棋 Web 程序"
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && submit()} />
        </label>

        <label className="field">
          <span>任务描述</span>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={4}
            placeholder="目标、验收标准、约束…（角色会读到这段描述）" />
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
                  <span className="member-avatar" style={{ background: avatarColor(r.slug) }}>
                    {r.display_name.slice(0, 1)}
                  </span>
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
