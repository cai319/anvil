import { useEffect, useMemo, useState } from "react";
import { api, type WorkflowDossier } from "../api/client";
import { PageHeader } from "../components/ui";
import { Icon } from "../components/Icon";
import { formatDateTime, formatDuration } from "../time";
import { METRIC_TIPS } from "../metricsHelp";

/**
 * Execution dossier: a read-only, exportable snapshot of one workflow
 * execution — metrics, planned vs actual, steps, packets and decisions.
 */
export function ExecutionDossier({ id }: { id: string }) {
  const [dossier, setDossier] = useState<WorkflowDossier | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const wid = useMemo(
    () => new URLSearchParams(window.location.search).get("workflow_id"),
    []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        let target = wid;
        if (!target) {
          const current = await api.getTaskWorkflow(id);
          target = current.id;
        }
        const data = await api.getWorkflowDossier(target);
        if (alive) setDossier(data);
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    load();
    return () => { alive = false; };
  }, [id, wid]);

  const download = async (format: "md" | "json") => {
    if (!dossier) return;
    setBusy(format); setError(null);
    try {
      const blob = await api.downloadWorkflowDossier(
        String(dossier.workflow.id), format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `dossier-${String(dossier.workflow.id).slice(0, 8)}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); }
    finally { setBusy(null); }
  };

  if (error && !dossier) return <p className="error">{error}</p>;
  if (!dossier) return <p className="muted">加载执行档案…</p>;

  const wf = dossier.workflow;
  const m = dossier.metrics;
  const diff = dossier.diff;
  const packets = dossier.packets || [];

  return (
    <div className="session dossier-page">
      <p className="session-back">
        <a href={`/tasks/${id}`}><Icon name="chevron-left" /> 返回任务</a>
      </p>
      <PageHeader
        kicker="EXECUTION DOSSIER"
        title={<>执行档案 · 第 {String(wf.execution_no ?? 1)} 次执行</>}
        subtitle={
          <>
            {String(wf.status)} · {String(wf.weight || "standard")} ·{" "}
            {formatDateTime(String(wf.created_at))}
            {" · "}<span className="mono" title={dossier.digest}>
              摘要 {dossier.digest.slice(0, 12)}…
            </span>
          </>
        }
        actions={
          <>
            <button className="small" disabled={busy !== null}
              onClick={() => download("md")}>
              {busy === "md" ? "导出中…" : "导出 Markdown"}
            </button>
            <button className="small" disabled={busy !== null}
              onClick={() => download("json")}>
              {busy === "json" ? "导出中…" : "导出 JSON"}
            </button>
          </>
        }
      />
      {error && <p className="error">{error}</p>}

      <div className="dossier-metrics">
        <div title={METRIC_TIPS.steps}>
          <span>步骤</span><strong>{m.steps.done}/{m.steps.total}</strong>
        </div>
        <div title={METRIC_TIPS.workflowDuration}>
          <span>工作流耗时</span><strong>{formatDuration(m.duration_seconds)}</strong>
        </div>
        <div title={METRIC_TIPS.activeDuration}>
          <span>有效执行</span>
          <strong>{formatDuration(m.active_duration_seconds
            ?? Math.max(0, m.duration_seconds - (m.waiting.excluded_seconds ?? 0)))}</strong>
        </div>
        <div title={METRIC_TIPS.humanWait}>
          <span>工作流人工等待</span><strong>{formatDuration(m.waiting.excluded_seconds ?? 0)}</strong>
        </div>
        <div title={METRIC_TIPS.maxDepth}>
          <span>最大深度</span><strong>{m.steps.max_depth}</strong>
        </div>
        <div title={METRIC_TIPS.gates}>
          <span>审核门</span><strong>{m.gates.total}</strong>
        </div>
        <div title={METRIC_TIPS.decisions}>
          <span>决策</span><strong>{m.decisions.total}</strong>
        </div>
        <div title={METRIC_TIPS.humanInterventions}>
          <span>人工干预</span>
          <strong>{m.decisions.human_interventions ?? m.decisions.human}</strong>
        </div>
        <div title={METRIC_TIPS.rework}>
          <span>打回</span><strong>{m.decisions.rejected}</strong>
        </div>
        <div title={METRIC_TIPS.executionTokens}>
          <span>执行 Token</span>
          <strong>{(m.tokens.total_tokens
            ?? (m.tokens.input_tokens ?? 0) + (m.tokens.output_tokens ?? 0)
          ).toLocaleString("en-US")}</strong>
        </div>
        <div title={METRIC_TIPS.taskTokens}>
          <span>任务 Token</span>
          <strong>{(m.tokens.task_total_tokens
            ?? (m.tokens.task_input_tokens ?? 0) + (m.tokens.task_output_tokens ?? 0)
          ).toLocaleString("en-US")}</strong>
        </div>
        <div title={METRIC_TIPS.generatedSlices}>
          <span>切片步骤</span><strong>{m.steps.generated_slices}</strong>
        </div>
      </div>

      <section className="dossier-section">
        <h2>计划 vs 实际</h2>
        <p className="muted">
          计划节点 {m.steps.total - (diff.added?.length ?? 0)} ·
          运行期新增 {diff.added?.length ?? 0} ·
          字段变更 {diff.changed?.length ?? 0}
        </p>
        <div className="dossier-diff">
          {(diff.added ?? []).map((n) => (
            <div key={`add-${n.step_id}`} className="dossier-diff-row">
              <span className="badge badge-in_progress">新增</span>
              <span className="mono">{n.step_id}</span>
              <span>{n.title}</span>
              <span className="muted">{n.source_step_id ? `由 ${n.source_step_id} 展开` : ""}</span>
            </div>
          ))}
          {(diff.changed ?? []).map((c, i) => (
            <div key={`chg-${c.step_id}-${c.field}-${i}`} className="dossier-diff-row">
              <span className="badge badge-todo">变更</span>
              <span className="mono">{c.step_id}</span>
              <span>{c.field}：{String(c.planned)} → {String(c.actual)}</span>
            </div>
          ))}
          {(diff.added ?? []).length === 0 && (diff.changed ?? []).length === 0 && (
            <p className="muted">实际执行与计划一致。</p>
          )}
        </div>
      </section>

      <section className="dossier-section">
        <h2>步骤</h2>
        <div className="dossier-steps">
          {dossier.steps.map((s) => (
            <div key={s.step_id} className="dossier-step">
              <span className="mono">{s.step_id}</span>
              <span>{s.title}</span>
              <span className="muted">{s.role_slug}</span>
              <span className="dossier-step-status">{s.status}</span>
              {s.rework_count ? <span className="muted">重做 {s.rework_count}</span> : null}
              {s.summary && <span className="muted dossier-step-summary">{s.summary}</span>}
            </div>
          ))}
        </div>
      </section>

      <section className="dossier-section">
        <h2>决策与证据</h2>
        {packets.length === 0 && <p className="muted">本次执行没有生成 review packet。</p>}
        {packets.map((p) => (
          <details key={p.id} className="dossier-packet">
            <summary>
              <span className="mono">{p.step_id}</span>
              <strong>{p.question}</strong>
              <span className="badge">{p.authority}</span>
              <span className="muted">{p.status}</span>
            </summary>
            <div className="dossier-evidence">
              {(p.evidence ?? []).map((e) => (
                <div key={e.id} className={e.fresh ? "" : "is-stale"}>
                  <span className="badge">{e.kind}</span>
                  <span className="mono">{e.ref}</span>
                  <span className="muted">{e.fresh ? "fresh" : "stale"}</span>
                </div>
              ))}
            </div>
            <div className="dossier-decisions">
              {(p as unknown as { decisions?: Record<string, unknown>[] }).decisions?.map((d, i) => (
                <div key={String(d.id ?? i)}>
                  <span className="badge">决策</span>
                  <span>{String(d.decider ?? "")}</span>
                  <strong>{String(d.decision ?? "")}</strong>
                  <span className="muted">{String(d.feedback ?? "")}</span>
                </div>
              ))}
            </div>
          </details>
        ))}
      </section>

      <section className="dossier-section">
        <h2>事件</h2>
        <div className="dossier-events">
          {Object.entries(dossier.event_counts || {}).map(([kind, count]) => (
            <span key={kind} className="badge">{kind} {count}</span>
          ))}
        </div>
      </section>
    </div>
  );
}
