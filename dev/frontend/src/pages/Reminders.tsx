import { useEffect, useState } from "react";
import { api, type Reminder, type Role } from "../api/client";

const KIND_LABEL: Record<string, string> = { cron: "定时", at: "延时", when: "条件" };
const STATUS_LABEL: Record<string, string> = {
  pending: "待触发",
  fired: "已触发",
  error: "异常",
};

export function Reminders() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [items, setItems] = useState<Reminder[]>([]);
  const [roleSlug, setRoleSlug] = useState("");
  const [kind, setKind] = useState<Reminder["kind"]>("cron");
  const [cron, setCron] = useState("0 9 * * 1");
  const [at, setAt] = useState("");
  const [when, setWhen] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    Promise.all([api.listRoles(), api.listReminders()])
      .then(([r, rem]) => {
        setRoles(r);
        setItems(rem);
        if (!roleSlug && r.length) setRoleSlug(r[0].slug);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const create = async () => {
    if (!roleSlug || !prompt.trim()) return;
    const body: Record<string, unknown> = { role_slug: roleSlug, kind, prompt: prompt.trim() };
    if (kind === "cron") body.cron_expr = cron.trim();
    if (kind === "at") body.fire_at = at ? new Date(at).toISOString() : "";
    if (kind === "when") body.when_condition = when.trim();
    try {
      await api.addReminder(body);
      setPrompt("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const fire = async (id: string) => {
    try {
      await api.fireReminder(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("确定删除该提醒？删除后不会再触发。")) return;
    try {
      await api.deleteReminder(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <h1>提醒（Reminders）</h1>
      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <select value={roleSlug} onChange={(e) => setRoleSlug(e.target.value)}>
          <option value="">选择角色…</option>
          {roles.map((r) => (
            <option key={r.slug} value={r.slug}>{r.display_name}</option>
          ))}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value as Reminder["kind"])}>
          <option value="cron">定时（cron）</option>
          <option value="at">延时（一次性）</option>
          <option value="when">条件（任务 done）</option>
        </select>
        {kind === "cron" && (
          <input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 9 * * 1" />
        )}
        {kind === "at" && (
          <input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} />
        )}
        {kind === "when" && (
          <input value={when} onChange={(e) => setWhen(e.target.value)} placeholder="任务 id（如 T-1042）" />
        )}
        <input
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && create()}
          placeholder="到期时注入给角色的提示"
          style={{ flex: 1 }}
        />
        <button className="primary" onClick={create} disabled={!roleSlug || !prompt.trim()}>添加提醒</button>
      </div>

      <table className="board">
        <thead>
          <tr><th>角色</th><th>类型</th><th>表达式 / 条件</th><th>提示</th><th>下次触发</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr><td colSpan={7} className="muted">暂无提醒 —— Agent 也可通过 `anvil reminder add` 自建。</td></tr>
          )}
          {items.map((r) => (
            <tr key={r.id}>
              <td>{r.role_slug ?? "—"}</td>
              <td>{KIND_LABEL[r.kind] ?? r.kind}</td>
              <td className="mono">{r.kind === "cron" ? r.cron_expr : r.kind === "at" ? r.fire_at : r.when_condition}</td>
              <td>{r.prompt ?? "—"}</td>
              <td className="mono muted">{r.next_fire_at?.slice(0, 19) ?? "—"}</td>
              <td>{STATUS_LABEL[r.status] ?? r.status}</td>
              <td>
                <button className="small" onClick={() => fire(r.id)}>立即触发</button>
                <button className="small danger" onClick={() => remove(r.id)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
