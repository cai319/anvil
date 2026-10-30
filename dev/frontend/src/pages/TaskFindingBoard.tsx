import { useCallback, useEffect, useState } from "react";
import {
  api, type Finding, type FindingBoard, type FindingSchema, type FindingSummary,
} from "../api/client";
import { FindingDrawer, FINDING_STATUS_LABEL, riskLabel, severityLabel, sortFindings } from "../components/FindingDrawer";
import { Icon } from "../components/Icon";

const STATUS_FILTERS: { key: string; label: string }[] = [
  { key: "", label: "全部状态" },
  { key: "submitted", label: "待确认" },
  { key: "confirmed", label: "已确认真实" },
  { key: "false_positive", label: "误报" },
  { key: "changes_requested", label: "需重新评估" },
  { key: "draft", label: "草稿" },
];

const SEVERITY_FILTERS: { key: string; label: string }[] = [
  { key: "", label: "全部等级" },
  { key: "critical", label: "严重" },
  { key: "high", label: "高" },
  { key: "medium", label: "中" },
  { key: "low", label: "低" },
  { key: "info", label: "提示" },
];

/** 任务问题板：展示审核者填写的 finding，并由自然人逐条确认。 */
export function TaskFindingBoard({ taskId, onChanged, initialFindingId }: {
  taskId: string;
  onChanged: () => void;
  initialFindingId?: string | null;
}) {
  const [items, setItems] = useState<Finding[]>([]);
  const [summary, setSummary] = useState<FindingSummary | null>(null);
  const [schema, setSchema] = useState<FindingSchema | null>(null);
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const [riskType, setRiskType] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(initialFindingId || null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([
      api.listFindings(taskId),
      schema ? Promise.resolve(schema) : api.getFindingSchema(taskId),
    ]).then(([data, s]) => {
      setItems(data.items || []);
      setSummary(data);
      setSchema(s);
    }).catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [taskId, schema]);

  useEffect(() => {
    setSelected(initialFindingId || null);
  }, [initialFindingId, taskId]);

  useEffect(() => {
    load();
    // schema 只加载一次；load 内部复用已加载的 schema，避免循环。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  const notifyChanged = () => {
    load();
    onChanged();
  };

  const filtered = sortFindings(items.filter((f) => {
    if (status && f.status !== status) return false;
    if (severity && f.severity !== severity) return false;
    if (riskType && f.risk_type !== riskType) return false;
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      const haystack = [
        f.title, f.module, f.function_name, f.file_path, f.line_numbers,
        f.risk_description, f.attack_path, f.author_role,
      ].join(" ").toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  }));

  const board: FindingBoard | null = summary?.board ?? null;
  const counts = summary?.counts || {};
  const pending = (counts.submitted || 0) + (counts.changes_requested || 0);
  const completed = board?.status === "completed";

  return (
    <div className="finding-board">
      <div className="finding-board-head">
        <div className="finding-board-title">
          <span className="finding-board-kicker">SECURITY ISSUE BOARD</span>
          <h2>
            问题板
            <span className={`finding-board-state is-${completed ? "completed" : "in_progress"}`}>
              {completed ? "审核者已完成" : "分析进行中"}
            </span>
          </h2>
          <p className="muted">
            本任务排查出的安全问题集中在这里。审核者通过 CLI 按固定字段填写，
            你在这里逐条确认真实问题或误报；全部确认后任务才能最终通过。
          </p>
        </div>
        <div className="finding-board-reviewers">
          <span className="muted">审核者</span>
          {summary?.reviewers?.length ? (
            summary.reviewers.map((r) => <span key={r} className="chip">{r}</span>)
          ) : (
            <span className="muted">尚未指定（Cindy 规划时指定）</span>
          )}
        </div>
      </div>

      <div className="finding-board-stats">
        <div className="finding-stat is-total">
          <strong>{summary?.total ?? 0}</strong><span>全部</span>
        </div>
        <div className="finding-stat is-pending">
          <strong>{counts.submitted || 0}</strong><span>待你确认</span>
        </div>
        <div className="finding-stat is-confirmed">
          <strong>{counts.confirmed || 0}</strong><span>真实问题</span>
        </div>
        <div className="finding-stat is-fp">
          <strong>{counts.false_positive || 0}</strong><span>误报</span>
        </div>
        <div className="finding-stat is-rework">
          <strong>{counts.changes_requested || 0}</strong><span>需重评</span>
        </div>
        <div className="finding-stat is-draft">
          <strong>{counts.draft || 0}</strong><span>草稿</span>
        </div>
      </div>

      {(completed || pending > 0 || (counts.changes_requested || 0) > 0) && (
        <div className={`finding-board-banner ${completed && pending === 0 ? "is-done" : "is-wait"}`}>
          <Icon name={completed && pending === 0 ? "shield" : "alert"} />
          <span>
            {completed && pending === 0
              ? "问题板已完成且所有条目均已确认"
              : pending > 0
                ? `有 ${pending} 条 finding 等待你确认真实/误报`
                : "审核者正在补充或重新评估 findings"}
          </span>
          {board?.summary && <span className="muted finding-board-summary">总结：{board.summary}</span>}
        </div>
      )}

      {!completed && summary?.total === 0 && (
        <div className="finding-empty">
          <Icon name="shield" />
          <p>审核者尚未录入 finding。</p>
          <small className="muted">
            审核者可在自己的 agent 会话中执行
            {" "}<code>anvil finding add --task {taskId} --file finding.json</code>{" "}
            填写问题，完成后执行 <code>anvil finding complete --summary "..."</code>；
            即使没有发现问题，也必须显式完成并写明「未发现安全问题」。
          </small>
        </div>
      )}

      {summary?.total === 0 && completed && (
        <div className="finding-empty is-done">
          <Icon name="shield" />
          <p>审核者已完成问题板：本任务未发现安全问题。</p>
          {board?.summary && <small className="muted">{board.summary}</small>}
        </div>
      )}

      {summary?.total !== 0 && (
        <>
          <div className="finding-board-filters">
            <label className="finding-search">
              <Icon name="search" size={13} />
              <input value={query} onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索标题、模块、函数、文件…" />
            </label>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUS_FILTERS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
            <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
              {SEVERITY_FILTERS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
            <select value={riskType} onChange={(e) => setRiskType(e.target.value)}>
              <option value="">全部类型</option>
              {Object.entries(schema?.risk_types || {}).map(([key, label]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
            <span className="muted finding-board-filter-count">
              {filtered.length}/{items.length}
            </span>
          </div>

          {filtered.length === 0 && (
            <p className="muted finding-board-none">没有符合筛选条件的 finding。</p>
          )}

          <div className="finding-list">
            {filtered.map((f) => (
              <button type="button" key={f.id} className={`finding-card is-${f.status}`}
                onClick={() => setSelected(f.id)}>
                <span className={`finding-sev is-${f.severity}`}>
                  {severityLabel(f.severity)}
                </span>
                <span className="finding-card-main">
                  <span className="finding-card-title">
                    {f.title || "（未命名 finding）"}
                  </span>
                  <span className="finding-card-loc muted">
                    {f.module || "未填模块"} · {f.function_name || "未填函数"}
                    {f.file_path ? ` · ${f.file_path}:${f.line_numbers}` : ""}
                  </span>
                  <span className="finding-card-meta">
                    <span className={`finding-status is-${f.status}`}>
                      {FINDING_STATUS_LABEL[f.status] || f.status}
                    </span>
                    <span className="chip">{riskLabel(schema, f.risk_type)}</span>
                    <span className="chip">置信度 {f.confidence}%</span>
                    <span className="muted">by {f.author_role} · {f.updated_at}</span>
                  </span>
                </span>
                <span className="finding-card-arrow" aria-hidden="true">›</span>
              </button>
            ))}
          </div>
        </>
      )}

      {loading && <p className="muted">加载问题板…</p>}
      {error && <p className="error">{error}</p>}

      {selected && (
        <FindingDrawer
          taskId={taskId}
          findingId={selected}
          schema={schema}
          onClose={() => setSelected(null)}
          onChanged={notifyChanged}
        />
      )}
    </div>
  );
}
