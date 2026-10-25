import { useCallback, useEffect, useState } from "react";
import { api, type KnowledgeStats } from "../../api/client";
import { EmptyState } from "../../components/ui";
import { formatDateTime } from "../../time";

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function KnowledgeStatsTab() {
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStats(await api.getKnowledgeStats());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (loading && !stats) return <p className="muted">加载中…</p>;
  if (!stats) {
    return (
      <div>
        {error && <p className="error">{error}</p>}
        <EmptyState icon="dashboard" title="暂无评估数据" hint="重启服务或稍后重试。" />
      </div>
    );
  }

  const totals = stats.totals;
  return (
    <div className="knowledge-stats">
      <div className="knowledge-scope-toolbar">
        <span className="muted">
          统计时间 {formatDateTime(stats.generated_at)}
        </span>
        <button type="button" className="small" onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
        {error && <span className="error">{error}</span>}
      </div>

      <div className="knowledge-stats-grid">
        <div className="knowledge-stat">
          <span>已批准频道知识</span>
          <strong>{totals.approved}</strong>
          <small>{totals.proposed} 条待批准</small>
        </div>
        <div className="knowledge-stat">
          <span>知识利用率（注入覆盖）</span>
          <strong>{pct(totals.hit_rate)}</strong>
          <small>{totals.used}/{totals.approved} 张卡被注入过</small>
        </div>
        <div className="knowledge-stat">
          <span>累计注入次数</span>
          <strong>{totals.hits}</strong>
          <small>平均每卡 {totals.avg_hits_per_card.toFixed(2)} 次</small>
        </div>
        <div className="knowledge-stat">
          <span>零命中</span>
          <strong>{totals.zero_hit}</strong>
          <small>尚未被任何任务注入</small>
        </div>
        <div className="knowledge-stat">
          <span>治理待处理</span>
          <strong>{totals.expired + totals.review_due + totals.conflicts}</strong>
          <small>
            过期 {totals.expired} · 复核 {totals.review_due} · 冲突 {totals.conflicts}
          </small>
        </div>
        <div className="knowledge-stat">
          <span>任务知识覆盖</span>
          <strong>{pct(stats.task_knowledge.coverage)}</strong>
          <small>
            {stats.task_knowledge.tasks_with_knowledge}/{stats.task_knowledge.tasks_total} 个任务
            · {stats.task_knowledge.active} 条知识
          </small>
        </div>
      </div>

      <h3 className="knowledge-list-title">频道知识注入情况</h3>
      {stats.channels.length === 0 ? (
        <EmptyState icon="book" title="暂无频道" hint="创建频道并写入频道知识后即可评估。" />
      ) : (
        <div className="table-scroll">
          <table className="knowledge-stats-table">
            <thead>
              <tr>
                <th>频道</th>
                <th>已批准</th>
                <th>被注入</th>
                <th>覆盖率</th>
                <th>注入次数</th>
                <th>任务数</th>
                <th>次/任务</th>
                <th>零命中</th>
              </tr>
            </thead>
            <tbody>
              {stats.channels.map((row) => (
                <tr key={row.workspace_id}>
                  <td>
                    <a href={`/channels?channel=${encodeURIComponent(row.workspace_id)}&tab=knowledge`}>
                      {row.workspace_name}
                    </a>
                  </td>
                  <td>{row.approved}</td>
                  <td>{row.used}</td>
                  <td>{pct(row.hit_rate)}</td>
                  <td>{row.hits}</td>
                  <td>{row.tasks}</td>
                  <td>{row.hits_per_task.toFixed(2)}</td>
                  <td>{row.zero_hit}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="knowledge-list-title">其他作用域</h3>
      <div className="knowledge-stats-grid">
        <div className="knowledge-stat">
          <span>共享经验</span>
          <strong>{stats.lessons.approved}</strong>
          <small>{stats.lessons.pending} 条待审批</small>
        </div>
        <div className="knowledge-stat">
          <span>角色知识</span>
          <strong>{stats.role_knowledge.notes_total}</strong>
          <small>{stats.role_knowledge.roles_with_notes} 个角色有笔记</small>
        </div>
        <div className="knowledge-stat">
          <span>角色知识使用追踪</span>
          <strong>{stats.role_knowledge.usage_tracked ? "已接入" : "未接入"}</strong>
          <small>当前只统计条数，不计算利用率</small>
        </div>
      </div>

      <p className="muted knowledge-stats-note">{stats.note}</p>
    </div>
  );
}
