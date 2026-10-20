import { useEffect, useMemo, useRef, useState } from "react";
import type { Role, Workflow, WorkflowStep } from "../api/client";

/**
 * 工作流 DAG 图谱（纯 SVG，无第三方依赖）。
 * 分层规则：layer = max(依赖的 layer)+1；同一层 = 可并行执行的步骤。
 */

const NODE_W = 190;
const NODE_H = 70;
const GAP_X = 76;
const GAP_Y = 18;
const PAD = 24;

const STATUS_META: Record<WorkflowStep["status"], { label: string; color: string; bg: string }> = {
  pending:   { label: "待办",   color: "#8b94a9", bg: "rgba(139,148,169,.14)" },
  running:   { label: "进行中", color: "#60a5fa", bg: "rgba(96,165,250,.16)" },
  submitted: { label: "待审核", color: "#fbbf24", bg: "rgba(251,191,36,.16)" },
  approved:  { label: "完成",   color: "#34d399", bg: "rgba(52,211,153,.14)" },
  rejected:  { label: "已驳回", color: "#fb923c", bg: "rgba(251,146,60,.16)" },
  failed:    { label: "失败",   color: "#f87171", bg: "rgba(248,113,113,.16)" },
  skipped:   { label: "跳过",   color: "#5c6579", bg: "rgba(92,101,121,.12)" },
};

function computeLayers(steps: WorkflowStep[]): Map<string, number> {
  const byId = new Map(steps.map((s) => [s.step_id, s]));
  const layers = new Map<string, number>();
  const visiting = new Set<string>();
  const layerOf = (id: string): number => {
    if (layers.has(id)) return layers.get(id)!;
    if (visiting.has(id)) return 0; // 环保护
    visiting.add(id);
    const deps = (byId.get(id)?.depends_on ?? []).filter((d) => byId.has(d));
    const value = deps.length ? Math.max(...deps.map((d) => layerOf(d) + 1)) : 0;
    visiting.delete(id);
    layers.set(id, value);
    return value;
  };
  steps.forEach((s) => layerOf(s.step_id));
  return layers;
}

export function WorkflowGraph({ workflow, roles, busy, onReview, onRetry }: {
  workflow: Workflow;
  roles: Role[];
  busy: boolean;
  onReview: (step: WorkflowStep, approve: boolean) => void;
  onRetry: (step: WorkflowStep) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const step = workflow.steps.find((s) => s.step_id === selected) ?? null;
  const wrapRef = useRef<HTMLDivElement>(null);
  const [fitScale, setFitScale] = useState(1);
  const [zoom, setZoom] = useState<number | null>(null);
  const scale = zoom ?? fitScale;

  // 竖向布局：层 = 行（从上到下），同层节点横向并排（可并行）。
  const { positions, edges, width, height } = useMemo(() => {
    const layers = computeLayers(workflow.steps);
    const rows: WorkflowStep[][] = [];
    for (const s of workflow.steps) {
      const l = layers.get(s.step_id) ?? 0;
      (rows[l] ??= []).push(s);
    }
    const rowWidths = rows.map((r) => (r ? r.length * (NODE_W + GAP_X) - GAP_X : 0));
    const maxW = Math.max(NODE_W, ...rowWidths);
    const pos = new Map<string, { x: number; y: number }>();
    rows.forEach((row, li) => {
      if (!row) return;
      const rowW = row.length * (NODE_W + GAP_X) - GAP_X;
      const left = PAD + (maxW - rowW) / 2;
      row.forEach((s, i) => {
        pos.set(s.step_id, {
          x: left + i * (NODE_W + GAP_X),
          y: PAD + li * (NODE_H + GAP_Y),
        });
      });
    });
    const es: { from: string; to: string; x1: number; y1: number; x2: number; y2: number }[] = [];
    for (const s of workflow.steps) {
      const p2 = pos.get(s.step_id);
      if (!p2) continue;
      for (const d of s.depends_on) {
        const p1 = pos.get(d);
        if (!p1) continue;
        es.push({
          from: d, to: s.step_id,
          x1: p1.x + NODE_W / 2, y1: p1.y + NODE_H,
          x2: p2.x + NODE_W / 2, y2: p2.y,
        });
      }
    }
    const h = rows.length ? rows.length * (NODE_H + GAP_Y) - GAP_Y : NODE_H;
    return { positions: pos, edges: es, width: maxW + PAD * 2, height: h + PAD * 2 };
  }, [workflow]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    // 竖向布局：横向填满容器（可放大），纵向自然滚动
    const update = () => {
      const w = Math.max(1, el.clientWidth - 8);
      const s = w / width;
      setFitScale(Math.max(0.3, Math.min(2.5, s)));
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [width]);

  const roleName = (slug: string) => roles.find((r) => r.slug === slug)?.display_name ?? slug;
  const parallelLayers = useMemo(() => {
    const counts = new Map<number, number>();
    const layers = computeLayers(workflow.steps);
    for (const n of layers.values()) counts.set(n, (counts.get(n) ?? 0) + 1);
    return [...counts.entries()].filter(([, n]) => n > 1).map(([l]) => l);
  }, [workflow]);

  return (
    <div className="wf-graph-wrap">
      <div className="wf-legend">
        {Object.entries(STATUS_META).map(([k, v]) => (
          <span key={k} className="wf-legend-item">
            <i style={{ background: v.color }} /> {v.label}
          </span>
        ))}
        <span className="muted">· 同一行（层）= 可并行；箭头向下 = 依赖</span>
        <span className="wf-zoom">
          <button className="small" title="缩小" onClick={() => setZoom(Math.max(0.3, scale - 0.15))}>−</button>
          <span className="muted">{Math.round(scale * 100)}%</span>
          <button className="small" title="放大" onClick={() => setZoom(Math.min(1.6, scale + 0.15))}>＋</button>
          <button className="small" title="横向填满容器（纵向滚动查看全图）"
            onClick={() => setZoom(null)}>适应宽度</button>
        </span>
      </div>

      <div className="wf-graph-scroll" ref={wrapRef}>
        <div style={{ width: Math.ceil(width * scale), height: Math.ceil(height * scale), position: "relative" }}>
        <div className="wf-graph" style={{ width, height, transform: `scale(${scale})`, transformOrigin: "top left" }}>
          <svg width={width} height={height} className="wf-edges">
            <defs>
              <marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5"
                markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#3a435c" />
              </marker>
            </defs>
            {edges.map((e, i) => (
              <path key={i} d={`M ${e.x1} ${e.y1} C ${e.x1} ${e.y1 + 42}, ${e.x2} ${e.y2 - 42}, ${e.x2} ${e.y2}`}
                fill="none" stroke="#3a435c" strokeWidth="1.5" markerEnd="url(#wf-arrow)" />
            ))}
          </svg>

          {workflow.steps.map((s) => {
            const p = positions.get(s.step_id);
            if (!p) return null;
            const meta = STATUS_META[s.status];
            return (
              <button
                key={s.step_id}
                className={`wf-node ${selected === s.step_id ? "selected" : ""}`}
                style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H,
                  borderColor: meta.color, background: meta.bg }}
                onClick={() => setSelected(selected === s.step_id ? null : s.step_id)}
                title={`${s.step_id} ${s.title} · ${meta.label}${s.gate ? " · 审核门" : ""}`}
              >
                <span className="wf-node-top">
                  <span className="wf-node-id mono">{s.step_id}</span>
                  {s.gate && <span className="wf-node-gate">◆ 审核门</span>}
                  <span className="wf-node-status" style={{ color: meta.color }}>{meta.label}</span>
                </span>
                <span className="wf-node-title">{s.title}</span>
                <span className="wf-node-role">{roleName(s.role_slug)}</span>
              </button>
            );
          })}
        </div>
        </div>
      </div>

      {parallelLayers.length > 0 && (
        <p className="muted wf-parallel-note">
          可并行阶段：{parallelLayers.map((l) => `第 ${l + 1} 层`).join("、")}（同一行的步骤可同时执行）
        </p>
      )}

      {step && (
        <div className="wf-detail card">
          <div className="wf-detail-head">
            <strong>{step.step_id} · {step.title}</strong>
            <span className="muted">{step.role_slug}</span>
            <span className="status" style={{ color: STATUS_META[step.status].color }}>
              {STATUS_META[step.status].label}
            </span>
          </div>
          {step.reviewer && <div className="muted">验证者（builder ≠ verifier）：{step.reviewer}</div>}
          {step.contract && (
            <div className="muted">契约：<span className="wf-contract">{step.contract}</span></div>
          )}
          {step.failure_reason && <div className="muted">失败归因：{step.failure_reason}</div>}
          {step.depends_on.length > 0 && (
            <div className="muted">依赖：{step.depends_on.join("、")}</div>
          )}
          {step.instructions && <p className="wf-detail-text">{step.instructions}</p>}
          {step.acceptance.length > 0 && (
            <div className="muted">验收：{step.acceptance.join("；")}</div>
          )}
          {step.summary && <div className="muted">提交摘要：{step.summary}</div>}
          <div className="wf-actions">
            {step.run_id && <a href={`/runs/${step.run_id}`}>查看会话</a>}
            {step.gate && step.status === "submitted" && (
              <>
                <button className="small primary" disabled={busy}
                  onClick={() => onReview(step, true)}>通过</button>
                <button className="small danger" disabled={busy}
                  onClick={() => onReview(step, false)}>驳回</button>
              </>
            )}
            {(step.status === "failed" || step.status === "rejected") && (
              <button className="small" disabled={busy} onClick={() => onRetry(step)}>重试</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
