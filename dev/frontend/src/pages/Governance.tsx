import { useEffect, useState } from "react";
import { api } from "../api/client";

interface AuditRow {
  created_at: string;
  role_slug: string | null;
  task_id: string | null;
  action: string;
}

interface BudgetInfo {
  role: string;
  cost_used_usd: number;
  cost_today_usd?: number;
  max_parallel_system?: number;
  max_parallel_runtime?: number;
  daily_cost_budget_usd?: number | null;
}

export function Governance() {
  const [frozen, setFrozen] = useState(false);
  const [budget, setBudget] = useState<BudgetInfo | null>(null);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [costs, setCosts] = useState<{ total_usd: number; by_role: { role: string; usd: number }[] } | null>(null);
  const [tokens, setTokens] = useState<{ total_tokens: number; input_tokens: number; output_tokens: number } | null>(null);
  const [limits, setLimits] = useState({ max_parallel_system: 6, max_parallel_runtime: 4, daily_cost_budget_usd: "" });
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const [f, b, a, c, t] = await Promise.all([
        api.getFreeze(), api.getBudget(), api.listAudit(), api.getCosts(), api.getTokens(),
      ]);
      setFrozen(f.frozen);
      setBudget(b as BudgetInfo);
      setAudit(a as AuditRow[]);
      setCosts(c);
      setTokens({ total_tokens: t.total_tokens, input_tokens: t.input_tokens,
                  output_tokens: t.output_tokens });
      const bb = b as BudgetInfo;
      setLimits({
        max_parallel_system: bb.max_parallel_system ?? 6,
        max_parallel_runtime: bb.max_parallel_runtime ?? 4,
        daily_cost_budget_usd: bb.daily_cost_budget_usd == null ? "" : String(bb.daily_cost_budget_usd),
      });
    } catch (e) {
      setError(String(e));
    }
  };

  const saveLimits = async () => {
    try {
      await api.updateBudget({
        max_parallel_system: Number(limits.max_parallel_system),
        max_parallel_runtime: Number(limits.max_parallel_runtime),
        daily_cost_budget_usd: limits.daily_cost_budget_usd,
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const toggle = async () => {
    try {
      const r = frozen ? await api.resumeAll() : await api.freezeAll();
      setFrozen(r.frozen);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <h1>治理</h1>
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <button onClick={toggle} className={frozen ? "danger" : ""}>
          {frozen ? "解除全场冻结" : "全场冻结"}
        </button>
        {tokens && <span>累计 Token {tokens.total_tokens.toLocaleString()}（入 {tokens.input_tokens.toLocaleString()} / 出 {tokens.output_tokens.toLocaleString()}）</span>}
        {budget && <span className="muted">成本账本 ${budget.cost_used_usd.toFixed(4)}</span>}
      </div>

      <section>
        <h2>闸门上限</h2>
        <div className="toolbar">
          <label>系统并发
            <input
              type="number" min={0} value={limits.max_parallel_system}
              onChange={(e) => setLimits({ ...limits, max_parallel_system: Number(e.target.value) })}
            />
          </label>
          <label>运行时并发
            <input
              type="number" min={0} value={limits.max_parallel_runtime}
              onChange={(e) => setLimits({ ...limits, max_parallel_runtime: Number(e.target.value) })}
            />
          </label>
          <label>日成本预算（USD，留空=不限）
            <input
              value={limits.daily_cost_budget_usd}
              onChange={(e) => setLimits({ ...limits, daily_cost_budget_usd: e.target.value })}
              placeholder="不限"
            />
          </label>
          <button onClick={saveLimits}>保存</button>
        </div>
      </section>

      <section>
        <h2>成本分解 · 总 ${(costs?.total_usd ?? 0).toFixed(4)}</h2>
        <table className="board">
          <thead><tr><th>角色</th><th>成本（USD）</th></tr></thead>
          <tbody>
            {(costs?.by_role ?? []).length === 0 && (
              <tr><td colSpan={2} className="muted">暂无成本记录。</td></tr>
            )}
            {(costs?.by_role ?? []).map((r) => (
              <tr key={r.role}><td>{r.role}</td><td className="mono">{r.usd.toFixed(4)}</td></tr>
            ))}
          </tbody>
        </table>
      </section>

      <h2>审计日志</h2>
      <table className="board">
        <thead>
          <tr><th>时间</th><th>角色</th><th>任务</th><th>动作</th></tr>
        </thead>
        <tbody>
          {audit.length === 0 && (
            <tr><td colSpan={4} className="muted">暂无审计记录 —— 有认领/提交/评审等操作后这里会显示。</td></tr>
          )}
          {audit.map((a, i) => (
            <tr key={i}>
              <td className="mono">{a.created_at}</td>
              <td>{a.role_slug ?? "—"}</td>
              <td>{a.task_id ?? "—"}</td>
              <td>{a.action}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
