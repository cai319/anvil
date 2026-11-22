import type {
  Task, Workflow, WorkflowGraphResponse, WorkflowMetrics, WorkflowStep,
} from "../api/client";
import { STATUS_META } from "../workflowGroups";
import { formatDuration } from "../time";
import { Icon } from "./Icon";
import { WEIGHT_META } from "./WorkflowWeightDialog";

export function WorkflowSummary({ workflow, taskStatus, metrics, diff }: {
  workflow: Workflow;
  taskStatus: Task["status"];
  metrics: WorkflowMetrics | null;
  diff: WorkflowGraphResponse | null;
}) {
  const total = workflow.steps.length;
  const approved = workflow.steps.filter((s) => s.status === "approved").length;
  const percent = total ? Math.round((approved / total) * 100) : 0;
  const stateLabel = workflow.status === "done"
    ? (taskStatus === "done" ? "已完成" : "已完成 · 等待最终评审")
    : workflow.status === "running" ? "推进中"
      : workflow.status === "terminated" ? "已终止 · 历史执行" : "已失败";
  const counts = (Object.keys(STATUS_META) as WorkflowStep["status"][])
    .map((status) => ({
      status,
      count: workflow.steps.filter((s) => s.status === status).length,
    }))
    .filter(({ status, count }) => count > 0 && status !== "approved");
  const feedbackRules = workflow.definition?.feedback_rules ?? [];
  const verification = workflow.definition?.verification ?? [];

  return (
    <div className={`wf-summary is-${workflow.status}`}>
      <div className="wf-summary-main">
        <span className="wf-summary-state">{stateLabel}</span>
        {workflow.pending_revision && (
          <span className="chip" title={workflow.pending_revision.impact?.summary || "待审批的增量修订"}>
            待审修订
          </span>
        )}
        {workflow.weight && (
          <span className={`wf-weight is-${workflow.weight}`}
            title={workflow.weight_reason || WEIGHT_META[workflow.weight].hint}>
            {WEIGHT_META[workflow.weight].label}
            <small>{workflow.weight_source === "human" ? "人工" : ""}</small>
          </span>
        )}
        <div className="wf-summary-progress">
          <span className="wf-summary-bar"><i style={{ width: `${percent}%` }} /></span>
          <span className="wf-summary-count"><strong>{approved}</strong> / {total} 步完成</span>
        </div>
        <div className="wf-summary-pills">
          {counts.map(({ status, count }) => (
            <span key={status} className="wf-summary-pill" style={{ color: STATUS_META[status].color }}>
              <i style={{ background: STATUS_META[status].color }} />
              {STATUS_META[status].label} {count}
            </span>
          ))}
        </div>
      </div>
      {metrics && (
        <div className="wf-metrics">
          <span title="最大依赖深度">深度 {metrics.steps.max_depth}</span>
          <span title="workflow 创建到完成/终止">耗时 {formatDuration(metrics.duration_seconds)}</span>
          <span title="扣除纯等待人工介入的时间">
            有效 {formatDuration(metrics.active_duration_seconds
              ?? Math.max(0, metrics.duration_seconds
                - (metrics.waiting.excluded_seconds ?? 0)))}
          </span>
          <span title="人工审核门">审核门 {metrics.gates.total}（待 {metrics.gates.pending}）</span>
          <span title="决策与返工">决策 {metrics.decisions.total} · 打回 {metrics.decisions.rejected}</span>
          <span title="没有角色在推进、纯等待人工审核的时长（区间去重）">
            人工等待 {formatDuration(metrics.waiting.excluded_seconds
              ?? (metrics.waiting.human_seconds + metrics.waiting.pending_human_seconds))}
          </span>
          <span title="仅 workflow 步骤关联 run 的 Token 总量（输入 + 输出 + 缓存读取 + 缓存写入）">
            Token {(metrics.tokens.total_tokens
              ?? (metrics.tokens.input_tokens ?? 0) + (metrics.tokens.output_tokens ?? 0)
            ).toLocaleString("en-US")}
          </span>
          {metrics.steps.generated_slices > 0 && (
            <span>切片 {metrics.steps.generated_slices} 步</span>
          )}
          {diff && (
            <details className="wf-diff">
              <summary>
                计划 vs 实际：新增 {(diff.added?.length ?? 0)} · 变更 {(diff.changed?.length ?? 0)}
              </summary>
              <div className="wf-diff-list">
                {(diff.added ?? []).length === 0 && <span className="muted">没有计划外节点。</span>}
                {(diff.added ?? []).map((n) => (
                  <span key={n.step_id} className="wf-diff-item">
                    + <span className="mono">{n.step_id}</span> {n.title}
                    {n.source_step_id ? `（由 ${n.source_step_id} 展开）` : ""}
                  </span>
                ))}
                {(diff.changed ?? []).map((c, i) => (
                  <span key={`${c.step_id}-${c.field}-${i}`} className="wf-diff-item">
                    ~ <span className="mono">{c.step_id}</span> {c.field}：
                    {String(c.planned)} → {String(c.actual)}
                  </span>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
      {(feedbackRules.length > 0 || verification.length > 0) && (
        <details className="wf-summary-rules">
          <summary>
            <Icon className="wf-summary-chevron" name="chevron-right" size={12} />
            规则与验证
            <span className="muted">反馈 {feedbackRules.length} · 验证 {verification.length}</span>
          </summary>
          <div className="wf-summary-rules-body">
            {feedbackRules.length > 0 && (
              <p className="muted">反馈路径：{feedbackRules.join("；")}</p>
            )}
            {verification.length > 0 && (
              <div className="wf-matrix">
                <span className="muted">验证矩阵：</span>
                {verification.map((v, i) => (
                  <span key={i} className="wf-matrix-cell">
                    <strong>{v.cell}</strong> · {v.method}
                    {v.verifier_role ? ` · ${v.verifier_role}` : ""}
                  </span>
                ))}
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

