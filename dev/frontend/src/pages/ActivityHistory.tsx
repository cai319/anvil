import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatDateTime } from "../time";
import { PageHeader } from "../components/ui";

/** 活动历史：完整审计记录（实时看板只显示最近 30 分钟，历史集中在这里）。 */

interface AuditRow {
  id: number;
  role_slug: string | null;
  task_id: string | null;
  action: string;
  payload: Record<string, unknown>;
  created_at: string;
}

const ACTION_OPTIONS = [
  "", "claim", "unclaim", "submit", "review", "wake", "starvation",
  "cancel", "reopen", "delete", "lease_expired", "plan",
];

export function ActivityHistory() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [role, setRole] = useState("");
  const [action, setAction] = useState("");
  const [limit, setLimit] = useState(200);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    api.listAudit({ limit, role: role.trim() || undefined, action: action || undefined })
      .then((r) => { setRows(r as AuditRow[]); setError(null); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(load, [limit]);

  const describe = (p: Record<string, unknown>) => {
    if (!p || Object.keys(p).length === 0) return "";
    if (p.summary) return `summary: ${String(p.summary).slice(0, 60)}`;
    if (p.message) return String(p.message).slice(0, 60);
    if (p.body) return String(p.body).slice(0, 60);
    if (p.approve !== undefined) return p.approve ? "通过" : "驳回";
    if (p.reason) return String(p.reason).slice(0, 60);
    return JSON.stringify(p).slice(0, 60);
  };

  return (
    <div className="session">
      <PageHeader kicker="ACTIVITY" title="活动历史" />
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <input placeholder="角色 slug（可选）" value={role} onChange={(e) => setRole(e.target.value)}
          style={{ width: 160 }} />
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          {ACTION_OPTIONS.map((a) => <option key={a} value={a}>{a || "全部动作"}</option>)}
        </select>
        <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          <option value={200}>200 条</option>
          <option value={500}>500 条</option>
          <option value={1000}>1000 条</option>
        </select>
        <button className="primary" onClick={load} disabled={loading}>{loading ? "加载中…" : "查询"}</button>
        <span className="spacer" />
        <a className="muted" href="/">← 返回任务大厅</a>
      </div>

      {error && <p className="error">{error}</p>}
      {rows.length === 0 && !loading && <p className="muted">没有匹配的记录。</p>}

      <div className="table-scroll">
      <table className="history-table">
        <thead>
          <tr><th>时间</th><th>角色</th><th>动作</th><th>任务</th><th>详情</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="mono">{formatDateTime(r.created_at)}</td>
              <td>{r.role_slug ?? "—"}</td>
              <td><span className="badge badge-in_progress">{r.action}</span></td>
              <td>{r.task_id ? <a href={`/tasks/${r.task_id}`}>{r.task_id.slice(0, 8)}</a> : "—"}</td>
              <td className="muted">{describe(r.payload)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}
