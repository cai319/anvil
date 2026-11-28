import { useEffect, useState } from "react";
import { api, type SandboxSelfCheck, type SandboxStatus } from "../api/client";
import { formatDateTime } from "../time";
import { PageHeader } from "../components/ui";
import { confirmDialog } from "../components/dialogs";
import { useLiveRefresh } from "../useLiveRefresh";

interface AuditRow {
  created_at: string;
  role_slug: string | null;
  task_id: string | null;
  action: string;
  payload?: Record<string, unknown>;
}

function auditAction(row: AuditRow): string {
  const payload = row.payload || {};
  if (row.action === "feature.toggle") {
    return `功能开关 ${String(payload.key ?? "")} → ${payload.value ? "开启" : "关闭"}`;
  }
  if (row.action === "feature.reset") {
    return `功能开关 ${String(payload.key ?? "")} → 恢复默认${payload.value ? "开启" : "关闭"}`;
  }
  return row.action;
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
  const [sandbox, setSandbox] = useState<SandboxStatus | null>(null);
  const [selfCheck, setSelfCheck] = useState<SandboxSelfCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [provisioning, setProvisioning] = useState(false);
  const [provisionLog, setProvisionLog] = useState<string | null>(null);
  const [limits, setLimits] = useState({
    max_parallel_system: 6,
    max_parallel_runtime: 4,
    daily_token_budget: "",
  });
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const [f, b, a, t, s] = await Promise.all([
        api.getFreeze(), api.getBudget(), api.listAudit(), api.getTokens(),
        api.getSandboxStatus(),
      ]);
      setFrozen(f.frozen);
      setAudit(a as AuditRow[]);
      setTokens(t as TokenSummary);
      setSandbox(s);
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

  useLiveRefresh(refresh, {
    topics: ["task"],
    tickMinGapMs: 15_000,
    pollMs: 60_000,
    initial: false,
  });

  const toggle = async () => {
    try {
      const r = frozen ? await api.resumeAll() : await api.freezeAll();
      setFrozen(r.frozen);
    } catch (e) {
      setError(String(e));
    }
  };

  const provisionWsl = async () => {
    const ok = await confirmDialog({
      title: "准备 WSL 沙箱？",
      message:
        "会在默认 WSL2 发行版中以 root 安装 bubblewrap/工具链，并创建专用用户 anvil-sbx。"
        + "该操作记录审计；完成后严格档 PoC 才能运行。",
      confirmLabel: "开始准备",
    });
    if (!ok) return;
    setProvisioning(true);
    setProvisionLog(null);
    try {
      const r = await api.provisionSandbox(true);
      setProvisionLog(r.log || (r.ok ? "准备完成。" : "准备失败。"));
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setProvisioning(false);
    }
  };

  return (
    <div className="governance-page">
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

      <section className="governance-limits">
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

      <section className="governance-limits sandbox-status-panel">
        <h2>执行沙箱</h2>
        {sandbox ? (
          <>
            <div className="toolbar">
              <span>模式 <strong>{sandbox.mode}</strong></span>
              <span>标准档 <strong>{sandbox.standard.backend}</strong></span>
              <span className={sandbox.standard.capabilities.write_scope ? "muted" : "error"}>
                写范围 {sandbox.standard.capabilities.write_scope ? "已强制" : "未强制"}
              </span>
              <span className="muted">
                读拒绝 {sandbox.standard.capabilities.read_deny ? "开启" : "关闭"}
              </span>
              <button
                disabled={checking}
                onClick={() => {
                  setChecking(true);
                  api.runSandboxSelfCheck()
                    .then(setSelfCheck)
                    .catch((e) => setError(String(e)))
                    .finally(() => setChecking(false));
                }}
              >{checking ? "自检中…" : "重新自检"}</button>
            </div>
            <p className="muted">
              严格档（PoC/动态验证）：
              {sandbox.strict.available
                ? sandbox.strict.backend
                : `未启用（${sandbox.strict.detail}）`}
            </p>
            {sandbox.substrates.wsl === true && !sandbox.strict.available && (
              <div className="toolbar">
                <button disabled={provisioning} onClick={provisionWsl}>
                  {provisioning ? "准备中…" : "准备 WSL 沙箱"}
                </button>
                <span className="muted">
                  一次性 root 配置：bubblewrap + 工具链 + 专用用户
                </span>
              </div>
            )}
            {provisionLog && <pre className="poc-pre">{provisionLog}</pre>}
            {sandbox.warnings.length > 0 && (
              <p className="sandbox-warning">
                能力/模式提示：{sandbox.warnings.join(" · ")}
              </p>
            )}
            {selfCheck && (
              <p className={selfCheck.ok ? "muted" : "error"}>
                自检：{selfCheck.ok ? "通过" : "未通过"}
                {selfCheck.warnings.length > 0 ? ` · ${selfCheck.warnings.join(" · ")}` : ""}
              </p>
            )}
          </>
        ) : (
          <p className="muted">加载中…</p>
        )}
      </section>

      <div className="governance-panels">
        <section className="governance-panel">
          <div className="governance-panel-head">
            <h2>Token 消耗分解</h2>
            <span className="muted">总 {fmt(tokens?.total_tokens ?? 0)} tokens</span>
          </div>
          <div className="table-scroll governance-table-scroll">
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

        <section className="governance-panel">
          <div className="governance-panel-head">
            <h2>审计日志</h2>
            <span className="muted">{audit.length} 条</span>
          </div>
          <div className="table-scroll governance-table-scroll">
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
                    <td title={JSON.stringify(a.payload || {})}>{auditAction(a)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
