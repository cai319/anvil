import { useCallback, useEffect, useState } from "react";
import { api, type KnowledgeStats, type WorkspaceKnowledge } from "../../api/client";
import { EmptyState } from "../../components/ui";
import { formatDateTime } from "../../time";

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function KnowledgeStatsTab() {
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [cards, setCards] = useState<WorkspaceKnowledge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextStats, page] = await Promise.all([
        api.getKnowledgeStats(),
        api.listAllWorkspaceKnowledge({ status: "approved", limit: 500 }),
      ]);
      setStats(nextStats);
      setCards(page.items);
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
  const topCards = [...cards]
    .sort((a, b) => (b.hit_count ?? 0) - (a.hit_count ?? 0))
    .slice(0, 10);
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
          <span>知识利用率（命中覆盖）</span>
          <strong>{pct(totals.hit_rate)}</strong>
          <small>{totals.used}/{totals.approved} 张卡被命中过</small>
        </div>
        <div className="knowledge-stat">
          <span>累计命中次数</span>
          <strong>{totals.hits}</strong>
          <small>平均每卡 {totals.avg_hits_per_card.toFixed(2)} 次</small>
        </div>
        <div className="knowledge-stat">
          <span>零命中</span>
          <strong>{totals.zero_hit}</strong>
          <small>尚未被任何任务命中</small>
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

      <h3 className="knowledge-list-title">频道知识命中情况</h3>
      {stats.channels.length === 0 ? (
        <EmptyState icon="book" title="暂无频道" hint="创建频道并写入频道知识后即可评估。" />
      ) : (
        <div className="table-scroll">
          <table className="knowledge-stats-table">
            <thead>
              <tr>
                <th>频道</th>
                <th>已批准</th>
                <th>被命中</th>
                <th>命中率</th>
                <th>命中次数</th>
                <th>任务数</th>
                <th>命中/任务</th>
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

      <h3 className="knowledge-list-title">单条知识效果（按命中次数 Top 10）</h3>
      {topCards.length === 0 ? (
        <EmptyState icon="book" title="暂无已批准的频道知识"
          hint="批准频道知识后即可查看单条命中次数。" />
      ) : (
        <div className="table-scroll">
          <table className="knowledge-stats-table">
            <thead>
              <tr>
                <th>知识卡</th>
                <th>频道</th>
                <th>命中次数</th>
                <th>最近命中</th>
                <th>完整度</th>
                <th>告警</th>
              </tr>
            </thead>
            <tbody>
              {topCards.map((card) => (
                <tr key={card.id}>
                  <td>
                    <a href={`/channels?channel=${encodeURIComponent(card.workspace_id)}&tab=knowledge`}>
                      {card.title}
                    </a>
                  </td>
                  <td>{card.workspace_name || "已删除频道"}</td>
                  <td>{card.hit_count ?? 0}</td>
                  <td>{card.last_hit_at ? formatDateTime(card.last_hit_at) : "—"}</td>
                  <td>{Math.round(card.completeness * 100)}%</td>
                  <td>
                    {card.is_expired ? "已过期" : ""}
                    {card.conflicts.length > 0 ? ` 冲突 ${card.conflicts.length}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted knowledge-stats-note">
        单条命中次数目前只对频道知识接入；任务知识、角色知识和共享经验暂未追踪命中次数。
      </p>

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
