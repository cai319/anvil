import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { Role, Workflow, WorkflowStep } from "../api/client";
import {
  STATUS_META,
  isDynamicStep,
  slicePhases,
  summarizeLoops,
  type LoopSummary,
} from "../workflowGroups";
import { Icon } from "../components/Icon";

/**
 * 工作流 DAG 图谱（纯 SVG，无第三方依赖）。
 *
 * 图谱只表达 workflow 的逻辑与架构：
 * - 静态步骤按依赖分层；
 * - 运行期切片的每个小闭环折叠成一个「切片循环」节点，
 *   节点上实时显示当前片 / 阶段 / 完成计数，不展开全部步骤。
 */

const NODE_W = 210;
const NODE_H = 92;
const GAP_X = 76;
const GAP_Y = 20;
const PAD = 24;

type VisualNode =
  | { id: string; kind: "step"; step: WorkflowStep; deps: string[] }
  | { id: string; kind: "loop"; loop: LoopSummary; deps: string[] };

/** 把真实步骤折叠成「静态步骤 + 每个 plan_slices 一个循环节点」。 */
function buildVisualModel(workflow: Workflow): VisualNode[] {
  const stepById = new Map(workflow.steps.map((s) => [s.step_id, s]));
  const visualId = (stepId: string): string => {
    const s = stepById.get(stepId);
    if (!s) return `step:${stepId}`;
    return isDynamicStep(s) ? `loop:${s.source_step_id}` : `step:${s.step_id}`;
  };

  const nodes: VisualNode[] = [];
  for (const step of workflow.steps) {
    if (isDynamicStep(step)) continue;
    const id = `step:${step.step_id}`;
    const deps = [...new Set(step.depends_on.map(visualId))].filter((d) => d !== id);
    nodes.push({ id, kind: "step", step, deps });
  }
  for (const loop of summarizeLoops(workflow.steps)) {
    nodes.push({
      id: `loop:${loop.sourceStepId}`,
      kind: "loop",
      loop,
      deps: [`step:${loop.sourceStepId}`],
    });
  }
  return nodes;
}

function computeLayers(nodes: VisualNode[]): Map<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const layers = new Map<string, number>();
  const visiting = new Set<string>();
  const layerOf = (id: string): number => {
    if (layers.has(id)) return layers.get(id)!;
    if (visiting.has(id)) return 0; // 环保护
    visiting.add(id);
    const deps = (byId.get(id)?.deps ?? []).filter((d) => byId.has(d));
    const value = deps.length ? Math.max(...deps.map((d) => layerOf(d) + 1)) : 0;
    visiting.delete(id);
    layers.set(id, value);
    return value;
  };
  nodes.forEach((n) => layerOf(n.id));
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
  const [selectedSliceKey, setSelectedSliceKey] = useState<string | null>(null);
  const visualNodes = useMemo(() => buildVisualModel(workflow), [workflow]);
  const selectedNode = visualNodes.find((n) => n.id === selected) ?? null;
  const selectedStep = selectedNode?.kind === "step" ? selectedNode.step : null;
  const selectedLoop = selectedNode?.kind === "loop" ? selectedNode.loop : null;
  // 点击循环节点默认展开“当前片”；也可以在切片列表里切换查看任意一片。
  const activeGroup = selectedLoop
    ? (selectedLoop.groups.find((g) => g.key === selectedSliceKey)
      ?? selectedLoop.currentGroup
      ?? selectedLoop.groups[0]
      ?? null)
    : null;
  const activePhases = activeGroup ? slicePhases(activeGroup) : [];
  const activeStep = activeGroup?.active ?? null;
  const wrapRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const [fitScale, setFitScale] = useState(1);
  const [zoom, setZoom] = useState<number | null>(null);
  const scale = zoom ?? fitScale;

  // 竖向布局：层 = 行（从上到下），同层节点横向并排（可并行）。
  const { positions, edges, rejectEdges, width, height } = useMemo(() => {
    const layers = computeLayers(visualNodes);
    const rows: VisualNode[][] = [];
    for (const n of visualNodes) {
      const l = layers.get(n.id) ?? 0;
      (rows[l] ??= []).push(n);
    }
    const rowWidths = rows.map((r) => (r ? r.length * (NODE_W + GAP_X) - GAP_X : 0));
    const maxW = Math.max(NODE_W, ...rowWidths);
    const pos = new Map<string, { x: number; y: number }>();
    rows.forEach((row, li) => {
      if (!row) return;
      const rowW = row.length * (NODE_W + GAP_X) - GAP_X;
      const left = PAD + (maxW - rowW) / 2;
      row.forEach((n, i) => {
        pos.set(n.id, {
          x: left + i * (NODE_W + GAP_X),
          y: PAD + li * (NODE_H + GAP_Y),
        });
      });
    });
    const es: { from: string; to: string; x1: number; y1: number; x2: number; y2: number }[] = [];
    for (const node of visualNodes) {
      const p2 = pos.get(node.id);
      if (!p2) continue;
      for (const dep of node.deps) {
        const p1 = pos.get(dep);
        if (!p1) continue;
        es.push({
          from: dep, to: node.id,
          x1: p1.x + NODE_W / 2, y1: p1.y + NODE_H,
          x2: p2.x + NODE_W / 2, y2: p2.y,
        });
      }
    }
    // 静态 decision 步骤的打回回边；动态 decision 已折叠进循环节点，不单独画。
    const stepById = new Map(workflow.steps.map((s) => [s.step_id, s]));
    const rs: { x1: number; y1: number; x2: number; y2: number }[] = [];
    for (const node of visualNodes) {
      if (node.kind !== "step" || node.step.kind !== "decision") continue;
      const p2 = pos.get(node.id);
      if (!p2) continue;
      for (const target of node.step.on_reject ?? []) {
        const targetStep = stepById.get(target);
        const targetId = targetStep
          ? (isDynamicStep(targetStep) ? `loop:${targetStep.source_step_id}` : `step:${targetStep.step_id}`)
          : `step:${target}`;
        if (targetId === node.id) continue;
        const p1 = pos.get(targetId);
        if (!p1) continue;
        rs.push({
          x1: p2.x + NODE_W / 2, y1: p2.y + NODE_H / 2,
          x2: p1.x + NODE_W / 2, y2: p1.y + NODE_H / 2,
        });
      }
    }
    const h = rows.length ? rows.length * (NODE_H + GAP_Y) - GAP_Y : NODE_H;
    return {
      positions: pos, edges: es, rejectEdges: rs,
      width: maxW + PAD * 2, height: h + PAD * 2,
    };
  }, [visualNodes, workflow.steps]);

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

  // 点击节点后把详情面板滚入视野，避免“点了没反应”的错觉。
  useEffect(() => {
    if (!selected) return;
    const raf = requestAnimationFrame(() => {
      detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => cancelAnimationFrame(raf);
  }, [selected]);

  const roleName = (slug: string) => roles.find((r) => r.slug === slug)?.display_name ?? slug;
  const parallelLayers = useMemo(() => {
    const counts = new Map<number, number>();
    const layers = computeLayers(visualNodes);
    for (const n of layers.values()) counts.set(n, (counts.get(n) ?? 0) + 1);
    return [...counts.entries()].filter(([, n]) => n > 1).map(([l]) => l);
  }, [visualNodes]);

  return (
    <div className="wf-graph-wrap">
      <div className="wf-legend">
        {Object.entries(STATUS_META).map(([k, v]) => (
          <span key={k} className="wf-legend-item">
            <i style={{ background: v.color }} /> {v.label}
          </span>
        ))}
        <span className="wf-legend-item">
          <i style={{ background: "#60a5fa" }} /> <Icon name="refresh" size={12} /> = 切片循环（不展开每步）
        </span>
        <span className="wf-legend-item">
          <i style={{ background: "#fb923c" }} /> 虚线 = 决策打回重做
        </span>
        <span className="muted">· 同一行（层）= 可并行；箭头向下 = 依赖</span>
        <span className="wf-zoom">
          <button className="small" title="缩小" onClick={() => setZoom(Math.max(0.3, scale - 0.15))}><Icon name="minus" /></button>
          <span className="muted">{Math.round(scale * 100)}%</span>
          <button className="small" title="放大" onClick={() => setZoom(Math.min(1.6, scale + 0.15))}><Icon name="plus" /></button>
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
              <marker id="wf-arrow-rework" viewBox="0 0 10 10" refX="9" refY="5"
                markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#fb923c" />
              </marker>
            </defs>
            {edges.map((e, i) => (
              <path key={i} d={`M ${e.x1} ${e.y1} C ${e.x1} ${e.y1 + 42}, ${e.x2} ${e.y2 - 42}, ${e.x2} ${e.y2}`}
                fill="none" stroke="#3a435c" strokeWidth="1.5" markerEnd="url(#wf-arrow)" />
            ))}
            {rejectEdges.map((e, i) => (
              <path key={`rework-${i}`}
                d={`M ${e.x1} ${e.y1} C ${e.x1 + 90} ${e.y1 - 40}, ${e.x2 + 90} ${e.y2 + 40}, ${e.x2} ${e.y2}`}
                fill="none" stroke="#fb923c" strokeWidth="1.4" strokeDasharray="6 4"
                markerEnd="url(#wf-arrow-rework)" opacity="0.8" />
            ))}
          </svg>

          {visualNodes.map((node) => {
            const p = positions.get(node.id);
            if (!p) return null;
            if (node.kind === "loop") {
              const loop = node.loop;
              const meta = STATUS_META[loop.status];
              // 进度条表达“当前这一片的闭环进度”；整体片数/步骤数在标题里。
              const pct = loop.currentGroup
                ? Math.round((loop.currentGroup.done
                    / Math.max(1, loop.currentGroup.total)) * 100)
                : 100;
              const current = loop.currentGroup
                ? `${loop.currentGroup.title} · ${loop.currentPhase}（${loop.currentProgress}）`
                : "全部完成";
              return (
                <button
                  key={node.id}
                  className={`wf-node wf-loop-node ${selected === node.id ? "selected" : ""}`}
                  style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H,
                    borderColor: meta.color, background: meta.bg }}
                  onClick={() => setSelected(selected === node.id ? null : node.id)}
                  title={`切片循环 · ${meta.label} · ${loop.totalSlices} 片 / ${loop.totalSteps} 步`}
                >
                  <span className="wf-node-top">
                    <span className="wf-node-id mono"><Icon name="refresh" size={12} /> 切片循环</span>
                    <span className="wf-node-status" style={{ color: meta.color }}>{meta.label}</span>
                  </span>
                  <span className="wf-node-title">
                    {loop.totalSlices} 片 · {loop.totalSteps} 步 · 完成 {loop.doneSlices}/{loop.totalSlices}
                  </span>
                  <span className="wf-node-role wf-loop-current" title={current}>{current}</span>
                  <span className="wf-loop-progress"
                    title={loop.currentGroup
                      ? `本片进度 ${loop.currentGroup.done}/${loop.currentGroup.total} · 总计 ${loop.doneSteps}/${loop.totalSteps}`
                      : `全部完成 ${loop.doneSteps}/${loop.totalSteps}`}>
                    <i style={{ width: `${pct}%`, background: meta.color }} />
                  </span>
                </button>
              );
            }
            const s = node.step;
            const meta = STATUS_META[s.status];
            return (
              <button
                key={node.id}
                className={`wf-node ${selected === node.id ? "selected" : ""}`}
                style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H,
                  borderColor: meta.color, background: meta.bg }}
                onClick={() => setSelected(selected === node.id ? null : node.id)}
                title={`${s.step_id} ${s.title} · ${meta.label}${s.gate ? " · 审核门" : ""}${s.kind === "decision" ? " · 决策" : ""}`}
              >
                <span className="wf-node-top">
                  <span className="wf-node-id mono">{s.step_id}</span>
                  {s.gate && <span className="wf-node-gate">审核门</span>}
                  {s.kind === "decision" && <span className="wf-node-gate">决策</span>}
                  {s.kind === "plan_slices" && <span className="wf-node-gate">切片计划</span>}
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

      <div ref={detailRef} className="wf-detail-anchor">
      {selectedLoop && activeGroup && (
        <div className="wf-detail card">
          <div className="wf-detail-head">
            <strong><Icon name="refresh" size={12} /> 切片循环 · {activeGroup.title}</strong>
            <span className="muted">
              第 {selectedLoop.groups.findIndex((g) => g.key === activeGroup.key) + 1}/{selectedLoop.totalSlices} 片
              {" · "}本片 {activeGroup.done}/{activeGroup.total}
            </span>
            <span className="status" style={{ color: STATUS_META[activeGroup.status].color }}>
              {STATUS_META[activeGroup.status].label}
            </span>
          </div>
          <div className="muted">
            整体进度：{selectedLoop.doneSlices}/{selectedLoop.totalSlices} 片，
            {" "}{selectedLoop.doneSteps}/{selectedLoop.totalSteps} 步
          </div>
          {selectedLoop.currentGroup && selectedLoop.currentGroup.key !== activeGroup.key && (
            <button className="small" onClick={() => setSelectedSliceKey(selectedLoop.currentGroup!.key)}>
              回到当前片：{selectedLoop.currentGroup.title}
            </button>
          )}

          {/* 单一循环流程：scope -> execute -> verify -> fix -> verify2 -> decide */}
          <div className="wf-loop-flow">
            {activePhases.map((phase, i) => {
              const meta = STATUS_META[phase.status];
              const isCurrent = Boolean(activeStep
                && phase.steps.some((s) => s.step_id === activeStep.step_id));
              return (
                <Fragment key={phase.key}>
                  {i > 0 && <span className="wf-flow-arrow"><Icon name="chevron-right" size={12} /></span>}
                  <div
                    className={`wf-flow-phase ${isCurrent ? "active" : ""}`}
                    style={{ borderColor: meta.color, background: meta.bg }}
                    title={phase.steps.map((s) => s.title).join(" / ")}
                  >
                    <div className="wf-flow-phase-head">
                      <span className="wf-flow-phase-label">{phase.label}</span>
                      <span className="wf-flow-phase-count">{phase.done}/{phase.total}</span>
                    </div>
                    <div className="wf-flow-phase-role">{roleName(phase.roleSlug)}</div>
                    {phase.total > 1 && <div className="wf-flow-phase-sub">{phase.total} 步</div>}
                    {isCurrent && <span className="wf-flow-current">当前</span>}
                  </div>
                </Fragment>
              );
            })}
          </div>

          {activeStep ? (
            <div className="muted wf-loop-current-step">
              当前步骤：{activeStep.step_id} · {activeStep.title}
              {" "}· {roleName(activeStep.role_slug)}
              {activeStep.run_id && (
                <> · <a href={`/runs/${activeStep.run_id}`}>查看会话</a></>
              )}
            </div>
          ) : activeGroup.current ? (
            <div className="muted wf-loop-current-step">
              下一步骤：{activeGroup.current.step_id} · {activeGroup.current.title}
              {" "}· {roleName(activeGroup.current.role_slug)}
            </div>
          ) : null}
          {activeStep
            && (activeStep.status === "failed" || activeStep.status === "rejected") && (
            <button className="small" disabled={busy}
              onClick={() => onRetry(activeStep)}>重试当前步骤</button>
          )}

          <details open className="wf-slice-steps">
            <summary>本片步骤（{activeGroup.done}/{activeGroup.total}）</summary>
            <div className="wf-slice-step-list">
              {activeGroup.steps.map((s) => {
                const meta = STATUS_META[s.status];
                return (
                  <div
                    key={s.step_id}
                    className={`wf-slice-step ${s.step_id === activeStep?.step_id ? "active" : ""}`}
                  >
                    <i className="wf-slice-dot" style={{ background: meta.color }} />
                    <span className="mono wf-slice-step-id">{s.step_id}</span>
                    <span className="wf-slice-step-title" title={s.title}>{s.title}</span>
                    <span className="muted wf-slice-step-role">{roleName(s.role_slug)}</span>
                    <span style={{ color: meta.color }}>{meta.label}</span>
                    {s.run_id && <a href={`/runs/${s.run_id}`}>会话</a>}
                  </div>
                );
              })}
            </div>
          </details>

          <details className="wf-slice-details">
            <summary>切换切片（{selectedLoop.totalSlices}）</summary>
            <div className="wf-slice-list">
              {selectedLoop.groups.map((g, idx) => (
                <button
                  key={g.key}
                  type="button"
                  className={`wf-slice-row wf-slice-row-btn ${g.key === activeGroup.key ? "active" : ""}`}
                  onClick={() => setSelectedSliceKey(g.key)}
                >
                  <i className="wf-slice-dot" style={{ background: STATUS_META[g.status].color }} />
                  <span className="wf-slice-name" title={g.title}>{idx + 1}. {g.title}</span>
                  <span className="muted">{g.done}/{g.total}</span>
                  <span className="muted">{g.phase}</span>
                </button>
              ))}
            </div>
          </details>

          <div className="wf-loop-phases">
            决策打回时 fix/verify2 重做，通过后进入下一片。
          </div>
        </div>
      )}

      {selectedStep && (
        <div className="wf-detail card">
          <div className="wf-detail-head">
            <strong>{selectedStep.step_id} · {selectedStep.title}</strong>
            <span className="muted">{selectedStep.role_slug}</span>
            <span className="status" style={{ color: STATUS_META[selectedStep.status].color }}>
              {STATUS_META[selectedStep.status].label}
            </span>
          </div>
          {selectedStep.reviewer && <div className="muted">验证者（builder ≠ verifier）：{selectedStep.reviewer}</div>}
          {selectedStep.kind === "plan_slices" && (
            <div className="muted">
              切片计划步骤：由该角色盘点项目并产出切片；服务端按 loop 模板为每片生成
              {"execute -> verify -> fix -> verify2 -> decide 闭环。"}
            </div>
          )}
          {selectedStep.kind === "decision" && (
            <div className="muted">
              决策步骤：通过后解锁下一片；打回 fix/verify2 重做
              {typeof selectedStep.rework_count === "number"
                ? `（已重做 ${selectedStep.rework_count}/${selectedStep.max_rework ?? 0}）` : ""}
            </div>
          )}
          {selectedStep.kind === "decision" && selectedStep.on_reject && selectedStep.on_reject.length > 0 && (
            <div className="muted">打回目标：{selectedStep.on_reject.join("、")}</div>
          )}
          {selectedStep.feedback && <div className="muted">最近决策/评审反馈：{selectedStep.feedback}</div>}
          {selectedStep.contract && (
            <div className="muted">契约：<span className="wf-contract">{selectedStep.contract}</span></div>
          )}
          {selectedStep.failure_reason && <div className="muted">失败归因：{selectedStep.failure_reason}</div>}
          {selectedStep.depends_on.length > 0 && (
            <div className="muted">依赖：{selectedStep.depends_on.join("、")}</div>
          )}
          {selectedStep.instructions && <p className="wf-detail-text">{selectedStep.instructions}</p>}
          {selectedStep.acceptance.length > 0 && (
            <div className="muted">验收：{selectedStep.acceptance.join("；")}</div>
          )}
          {selectedStep.summary && <div className="muted">提交摘要：{selectedStep.summary}</div>}
          <div className="wf-actions">
            {selectedStep.run_id && <a href={`/runs/${selectedStep.run_id}`}>查看会话</a>}
            {selectedStep.gate && selectedStep.status === "submitted" && (
              <>
                <button className="small primary" disabled={busy}
                  onClick={() => onReview(selectedStep, true)}>通过</button>
                <button className="small danger" disabled={busy}
                  onClick={() => onReview(selectedStep, false)}>驳回</button>
              </>
            )}
            {(selectedStep.status === "failed" || selectedStep.status === "rejected") && (
              <button className="small" disabled={busy} onClick={() => onRetry(selectedStep)}>重试</button>
            )}
          </div>
        </div>
      )}
      </div>
    </div>
  );
}
