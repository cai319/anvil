import { useEffect, useState } from "react";
import { api, type Channel, type Member } from "../api/client";
import { PageHeader } from "../components/ui";

export function Channels() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [name, setName] = useState("");
  const [roleSlug, setRoleSlug] = useState("");
  const [access, setAccess] = useState("write");
  const [error, setError] = useState<string | null>(null);

  const refreshChannels = () => api.listChannels().then(setChannels).catch((e) => setError(String(e)));

  useEffect(() => {
    refreshChannels();
  }, []);

  useEffect(() => {
    if (!selected) return;
    api.listMembers(selected).then(setMembers).catch((e) => setError(String(e)));
  }, [selected]);

  const create = async () => {
    if (!name.trim()) return;
    try {
      const c = await api.createChannel(name.trim());
      setName("");
      await refreshChannels();
      setSelected(c.id);
    } catch (e) {
      setError(String(e));
    }
  };

  const bind = async () => {
    if (!selected || !roleSlug.trim()) return;
    try {
      setMembers(await api.addMember(selected, roleSlug.trim(), access));
      setRoleSlug("");
      await refreshChannels();
    } catch (e) {
      setError(String(e));
    }
  };

  const changeAccess = async (m: Member, newAccess: string) => {
    try {
      setMembers(await api.updateMember(selected!, m.role_slug, newAccess));
    } catch (e) {
      setError(String(e));
    }
  };

  const unbind = async (m: Member) => {
    if (!window.confirm(`确定将「${m.role_slug}」移出该频道？`)) return;
    try {
      await api.removeMember(selected!, m.role_slug);
      setMembers(await api.listMembers(selected!));
      await refreshChannels();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="roles">
      <PageHeader kicker="CHANNELS" title="频道" />
      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="频道名" />
        <button onClick={create}>建频道</button>
      </div>

      <div className="roles-layout">
        <ul className="roles-list">
          {channels.length === 0 && <li className="muted">暂无频道，先在右侧新建。</li>}
          {channels.map((c) => (
            <li key={c.id} className={selected === c.id ? "active" : ""}>
              <a href="#" onClick={(e) => { e.preventDefault(); setSelected(c.id); }}>
                {c.name} <small>({c.member_count} 成员 · {c.task_count} 任务)</small>
              </a>
            </li>
          ))}
        </ul>

        <div className="roles-detail">
          {!selected ? (
            <p className="muted">选择左侧频道管理成员。</p>
          ) : (
            <>
              <h2>成员</h2>
              <div className="toolbar">
                <input value={roleSlug} onChange={(e) => setRoleSlug(e.target.value)} placeholder="角色 slug" />
                <select value={access} onChange={(e) => setAccess(e.target.value)}>
                  <option value="read">read</option>
                  <option value="write">write</option>
                  <option value="admin">admin</option>
                </select>
                <button onClick={bind}>绑定角色</button>
              </div>
              <ul className="roles-list">
                {members.length === 0 && <li className="muted">暂无成员 —— 绑定角色后可在此频道认领任务。</li>}
                {members.map((m) => (
                  <li key={m.role_slug}>
                    <span>{m.role_slug}</span>
                    <select value={m.access} onChange={(e) => changeAccess(m, e.target.value)}>
                      <option value="read">read</option>
                      <option value="write">write</option>
                      <option value="admin">admin</option>
                    </select>
                    <button onClick={() => unbind(m)} className="danger">解绑</button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
