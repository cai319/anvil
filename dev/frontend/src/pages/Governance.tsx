import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatDateTime } from "../time";
import { PageHeader } from "../components/ui";

interface AuditRow {
  created_at: string;
  role_slug: string | null;
  task_id: string | null;
  action: string;
}

interface BudgetInfo {
  role: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  today_total_tokens: number;
  max_parallel_system?: number;
  max_parallel_runtime?: number;
  daily_token_budget?: number | null;
}

interface TokenRole {
  role: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

interface TokenSummary {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  today_total_tokens: number;
  by_role: TokenRole[];
}

const fmt = (n: number) => n.toLocaleString();

export function Governance() {
  const [frozen, setFrozen] = useState(false);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [tokens, setTokens] = useState<TokenSummary | null>(null);
  const [limits, setLimits] = useState({
    max_parallel_system: 6,
    max_parallel_runtime: 4,
    daily_token_budget: "",
  });
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const [f, b, a, t] = await Promise.all([
        api.getFreeze(), api.getBudget(), api.listAudit(), api.getTokens(),
      ]);
      setFrozen(f.frozen);
      setAudit(a as AuditRow[]);
      setTokens(t as TokenSummary);
      const bb = b as BudgetInfo;
      setLimits({
        max_parallel_system: bb.max_parallel_system ?? 6,
        max_parallel_runtime: bb.max_parallel_runtime ?? 4,
        daily_token_budget: bb.daily_token_budget == null ? "" : String(bb.daily_token_budget),
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
        daily_token_budget: limits.daily_token_budget,
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
      <PageHeader kicker="GOVERNANCE" title="治理" />
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <button onClick={toggle} className={frozen ? "danger" : ""}>
          {frozen ? "解除全场冻结" : "全场冻结"}
        </button>
        {tokens && (
          <span>
            累计 Token {fmt(tokens.total_tokens)}
            <span className="muted">（入 {fmt(tokens.input_tokens)} / 出 {fmt(tokens.output_tokens)}）</span>
          </span>
        )}
        {tokens && (
          <span className="muted">今日 {fmt(tokens.today_total_tokens)} tokens</span>
        )}
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
          <label>每日 Token 预算（留空=不限）
            <input
              type="number"
              min={0}
              step={1000}
              value={limits.daily_token_budget}
              onChange={(e) => setLimits({ ...limits, daily_token_budget: e.target.value })}
              placeholder="不限"
            />
          </label>
          <button onClick={saveLimits}>保存</button>
        </div>
      </section>

      <section>
        <h2>Token 消耗分解 · 总 {fmt(tokens?.total_tokens ?? 0)} tokens</h2>
        <div className="table-scroll">
        <table className="board">
          <thead>
            <tr><th>角色</th><th>输入</th><th>输出</th><th>总量</th></tr>
          </thead>
          <tbody>
            {(tokens?.by_role ?? []).length === 0 && (
              <tr><td colSpan={4} className="muted">暂无 token 记录。</td></tr>
            )}
            {(tokens?.by_role ?? []).map((r) => (
              <tr key={r.role}>
                <td>{r.role}</td>
                <td className="mono">{fmt(r.input_tokens)}</td>
                <td className="mono">{fmt(r.output_tokens)}</td>
                <td className="mono">{fmt(r.total_tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </section>

      <h2>审计日志</h2>
      <div className="table-scroll">
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
              <td className="mono">{formatDateTime(a.created_at)}</td>
              <td>{a.role_slug ?? "—"}</td>
              <td>{a.task_id ?? "—"}</td>
              <td>{a.action}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}
