import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  api,
  type BoardDraft,
  type Role,
  type TemplateLoopPhase,
  type TemplateRoleSlot,
  type TemplateStep,
  type TemplateValidationIssue,
  type WorkflowTemplate,
  type WorkflowTemplateDefinition,
} from "../api/client";
import { PageHeader } from "../components/ui";
import { Icon } from "../components/Icon";
import {
  addLoop,
  addStep,
  ancestorsOf,
  autoLayout,
  blankDefinition,
  cloneDefinition,
  compileTemplate,
  connectStep,
  definitionFromConcrete,
  disconnectStep,
  ensureLayout,
  meepleKey,
  removeStep,
  renameMeepleKey,
  roleNameOf,
  ROLE_NAME_RE,
  type TemplateLayout,
  usedSlots,
} from "../templates/editorModel";
import { diffTemplate, type TemplateDiffLine } from "../templates/templateDiff";

type Snap = { definition: WorkflowTemplateDefinition; layout: TemplateLayout };
type TemplateNodeData = Record<string, unknown> & {
  step: TemplateStep;
  slotLabel: string;
  loop: boolean;
  errors: string[];
  warnings: string[];
};
type FlowNode = Node<TemplateNodeData, "template">;
type DraftMeta = { name: string; description: string; use_mode: "seed" | "manual" | "both" };

const PHASES = ["scope", "execute", "verify", "fix", "verify2", "decide"] as const;
const PHASE_LABEL: Record<(typeof PHASES)[number], string> = {
  scope: "范围确认",
  execute: "执行",
  verify: "验证",
  fix: "修复",
  verify2: "复验",
  decide: "决策",
};

function slotLabel(slots: TemplateRoleSlot[], key?: string | null): string {
  const slot = slots.find((item) => item.key === key);
  return slot ? `${slot.label} · ${slot.key}` : key || "未设置";
}

function TemplateNode({ data, selected }: NodeProps) {
  const d = data as unknown as TemplateNodeData;
  const step = d.step;
  const kind = step.kind || "task";
  const issueTitle = [...d.errors, ...d.warnings].join("\n");
  return (
    <div className={`wft-node ${selected ? "selected" : ""} ${d.errors.length ? "has-error" : ""} ${d.warnings.length ? "has-warning" : ""}`}
      title={issueTitle || undefined}>
      <Handle type="target" position={Position.Left} />
      <div className="wft-node-top">
        <span className="wft-node-id mono">{step.id}</span>
        {kind === "decision" && <span className="wft-node-kind">决策</span>}
        {kind === "plan_slices" && <span className="wft-node-kind">切片循环</span>}
        {step.gate && <span className="wft-node-gate">审核门</span>}
      </div>
      <strong className="wft-node-title" title={step.title}>{step.title || "未命名步骤"}</strong>
      <span className="wft-node-role">{d.slotLabel}</span>
      {d.loop && <span className="wft-node-loop"><Icon name="refresh" size={11} /> 六阶段闭环</span>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { template: TemplateNode };

/** 打回边从节点下方绕行，避免与同排的依赖边重叠而无法点选。 */
function RejectEdge({ id, sourceX, sourceY, targetX, targetY, markerEnd, style, data }: EdgeProps) {
  const lane = Math.max(0, Number((data as { lane?: number } | undefined)?.lane) || 0);
  const span = Math.abs(sourceX - targetX);
  // 同一决策节点的多条打回边分道绕行，避免曲线重叠后无法点选。
  const drop = 46 + Math.min(120, span * 0.1) + lane * 30;
  const c1x = sourceX + Math.min(90, span * 0.3);
  const c2x = targetX - Math.min(90, span * 0.3);
  const path = `M ${sourceX},${sourceY} C ${c1x},${sourceY + drop} ${c2x},${targetY + drop} ${targetX},${targetY}`;
  return (
    <>
      <path id={id} className="react-flow__edge-path" d={path} fill="none"
        markerEnd={markerEnd} style={style} />
      <path d={path} fill="none" strokeWidth={24} strokeOpacity={0}
        className="react-flow__edge-interaction" />
      <EdgeLabelRenderer>
        <div className="wft-reject-edge-label nodrag nopan"
          style={{
            transform: `translate(-50%, -50%) translate(${(sourceX + targetX) / 2}px, ${Math.max(sourceY, targetY) + drop * 0.78}px)`,
          }}>
          打回
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const edgeTypes = { reject: RejectEdge };

function makeNodes(definition: WorkflowTemplateDefinition,
                   layout: TemplateLayout,
                   selectedId: string | null,
                   issues: TemplateValidationIssue[]): FlowNode[] {
  const positions = ensureLayout(definition, layout);
  return (definition.steps || []).map((step) => {
    const nodeIssues = issues.filter((issue) => issue.node_id === step.id);
    return {
      id: step.id,
      type: "template",
      position: positions[step.id] || { x: 0, y: 0 },
      selected: step.id === selectedId,
      data: {
        step,
        slotLabel: slotLabel(definition.role_slots || [], step.role_slot),
        loop: step.kind === "plan_slices" && definition.loop?.from === step.id,
        errors: nodeIssues.filter((issue) => issue.severity === "error").map((issue) => issue.message),
        warnings: nodeIssues.filter((issue) => issue.severity !== "error").map((issue) => issue.message),
      },
    };
  });
}

function makeEdges(definition: WorkflowTemplateDefinition, selectedEdgeId?: string | null): Edge[] {
  const edges: Edge[] = [];
  for (const step of definition.steps || []) {
    for (const dep of step.depends_on || []) {
      const id = `dep:${dep}->${step.id}`;
      edges.push({
        id,
        source: dep,
        target: step.id,
        type: "smoothstep",
        deletable: true,
        selectable: true,
        selected: id === selectedEdgeId,
        reconnectable: true,
        markerEnd: { type: MarkerType.ArrowClosed },
        data: { kind: "dependency", source: dep, target: step.id },
        style: { stroke: "#7c849c", strokeWidth: 1.5 },
      });
    }
    for (const [lane, target] of (step.on_reject || []).entries()) {
      const id = `reject:${step.id}->${target}`;
      edges.push({
        id,
        source: step.id,
        target,
        type: "reject",
        deletable: true,
        selectable: true,
        selected: id === selectedEdgeId,
        reconnectable: true,
        animated: false,
        data: { kind: "reject", source: step.id, target, lane },
        zIndex: 4,
        style: { stroke: "#fb923c", strokeWidth: 1.5, strokeDasharray: "6 4" },
      });
    }
  }
  return edges;
}

function phaseSpec(loop: WorkflowTemplateDefinition["loop"], phase: string): TemplateLoopPhase | undefined {
  if (!loop || !(phase in loop)) return undefined;
  return (loop as unknown as Record<string, TemplateLoopPhase | string>)[phase] as TemplateLoopPhase | undefined;
}

export function WorkflowTemplates() {
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [template, setTemplate] = useState<WorkflowTemplate | null>(null);
  const [definition, setDefinition] = useState<WorkflowTemplateDefinition>(blankDefinition());
  const [layout, setLayout] = useState<TemplateLayout>({});
  const [meta, setMeta] = useState<DraftMeta>({ name: "", description: "", use_mode: "both" });
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<Edge | null>(null);
  const [issues, setIssues] = useState<TemplateValidationIssue[]>([]);
  const [past, setPast] = useState<Snap[]>([]);
  const [future, setFuture] = useState<Snap[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [draftSource, setDraftSource] = useState<BoardDraft | null>(null);
  const [draftMapping, setDraftMapping] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [draftOrigin, setDraftOrigin] = useState<WorkflowTemplateDefinition | null>(null);
  const [showDraftDiff, setShowDraftDiff] = useState(false);
  const flowRef = useRef<ReactFlowInstance<FlowNode, Edge> | null>(null);

  const selectedEdgeId = selectedEdge?.id ?? null;
  const generated = useMemo(() => ({
    nodes: makeNodes(definition, layout, selectedNodeId, issues),
    edges: makeEdges(definition, selectedEdgeId),
  }), [definition, layout, selectedNodeId, issues, selectedEdgeId]);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(generated.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(generated.edges);

  useEffect(() => {
    setNodes(generated.nodes);
    setEdges(generated.edges);
  }, [generated, setNodes, setEdges]);

  const refresh = async (select?: string | null) => {
    const list = await api.listWorkflowTemplates(q || undefined, "active");
    setTemplates(list);
    const target = select === undefined ? template?.id : select;
    if (target && list.some((item) => item.id === target)) {
      await loadTemplate(target);
    } else if (list.length && !select) {
      await loadTemplate(list[0].id);
    } else if (!list.length) {
      setTemplate(null);
      setDefinition(blankDefinition());
      setLayout({});
      setMeta({ name: "", description: "", use_mode: "both" });
    }
  };

  const loadTemplate = async (id: string) => {
    const data = await api.getWorkflowTemplate(id);
    const version = data.draft || data.published;
    setTemplate(data);
    setDefinition(version?.definition || blankDefinition());
    setLayout(version?.layout || {});
    setIssues(version?.validation || []);
    setMeta({
      name: data.name || "",
      description: data.description || "",
      use_mode: data.use_mode || "both",
    });
    setSelectedNodeId(null);
    setDraftSource(null);
    setDraftMapping({});
    setPast([]);
    setFuture([]);
    setDirty(false);
    setError(null);
    setNotice(null);
  };

  const loadDraft = async (draftId: string) => {
    const draft = await api.getDraft(draftId);
    if ((draft.kind || "tasks") !== "workflow") {
      throw new Error("该草案不是 workflow 草案");
    }
    const converted = definitionFromConcrete(draft.tree as unknown as Record<string, unknown>);
    setTemplate(null);
    setDraftSource(draft);
    setDraftOrigin(converted.definition);
    setDraftMapping(converted.roleMapping);
    setDefinition(converted.definition);
    setLayout(autoLayout(converted.definition));
    setIssues([]);
    setMeta({ name: draft.goal || "工作流草案", description: "", use_mode: "both" });
    setSelectedNodeId(null);
    setPast([]);
    setFuture([]);
    setDirty(false);
    setError(null);
    setNotice("草案模式：保存只更新待审草案，不会直接启动。");
  };

  useEffect(() => {
    api.listRoles().then(setRoles).catch(() => setRoles([]));
    const draftId = new URLSearchParams(window.location.search).get("draft");
    if (draftId) {
      loadDraft(draftId).catch((e) => setError(String(e)));
    } else {
      refresh().catch((e) => setError(String(e)));
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const saveDraftSource = async () => {
    if (!draftSource) return null;
    const tree = compileTemplate(definition, draftMapping);
    const updated = await api.updateDraft(draftSource.id, { tree });
    setDraftSource(updated);
    setDirty(false);
    setNotice("草案已更新；仍在收件箱等待人工审批，不会自动启动。");
    return updated;
  };

  const saveAsTemplate = async () => {
    setBusy(true); setError(null);
    try {
      const created = await api.createWorkflowTemplate({
        name: `${meta.name || "工作流草案"} · 模板`,
        description: meta.description || "",
        definition, layout, use_mode: meta.use_mode,
      });
      await refresh(created.id);
      setNotice("已另存为模板草稿。");
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const snapshot = (): Snap => ({ definition: cloneDefinition(definition), layout: { ...layout } });
  const pushHistory = () => {
    setPast((items) => [...items.slice(-99), snapshot()]);
    setFuture([]);
  };
  const commit = (nextDefinition: WorkflowTemplateDefinition,
                  nextLayout: TemplateLayout = layout) => {
    pushHistory();
    setDefinition(nextDefinition);
    setLayout(nextLayout);
    setSelectedEdge(null);
    setDirty(true);
  };
  const changeDefinition = (next: WorkflowTemplateDefinition) => {
    setDefinition(next);
    setDirty(true);
  };
  const undo = () => {
    const prev = past[past.length - 1];
    if (!prev) return;
    setFuture((items) => [snapshot(), ...items]);
    setPast((items) => items.slice(0, -1));
    setDefinition(prev.definition);
    setLayout(prev.layout);
    setSelectedEdge(null);
    setNotice(null);
    setDirty(true);
  };
  const redo = () => {
    const next = future[0];
    if (!next) return;
    setPast((items) => [...items, snapshot()]);
    setFuture((items) => items.slice(1));
    setDefinition(next.definition);
    setLayout(next.layout);
    setSelectedEdge(null);
    setNotice(null);
    setDirty(true);
  };

  const selectedStep = definition.steps?.find((step) => step.id === selectedNodeId) || null;
  const selectedLoop = selectedStep?.kind === "plan_slices" && definition.loop?.from === selectedStep.id
    ? definition.loop : null;

  const onConnect = (connection: Connection) => {
    if (!connection.source || !connection.target) return;
    connectNodes(connection.source, connection.target);
  };
  const onEdgesDelete = (deleted: Edge[]) => {
    let next = cloneDefinition(definition);
    for (const edge of deleted) {
      const data = edge.data as { kind?: string; source?: string; target?: string } | undefined;
      if (data?.kind === "dependency" && data.source && data.target) {
        next = disconnectStep(next, data.source, data.target);
      } else if (data?.kind === "reject" && data.source && data.target) {
        const step = next.steps.find((item) => item.id === data.source);
        if (step) step.on_reject = (step.on_reject || []).filter((id) => id !== data.target);
      }
    }
    commit(next);
  };

  const addNewStep = (kind: TemplateStep["kind"] = "task") => {
    const next = addStep(definition, kind);
    const nextLayout = autoLayout(next);
    commit(next, nextLayout);
    setSelectedNodeId(next.steps[next.steps.length - 1]?.id || null);
  };
  const addNewLoop = () => {
    try {
      const next = addLoop(definition);
      const plan = next.steps.find((step) => step.kind === "plan_slices");
      commit(next, autoLayout(next));
      setSelectedNodeId(plan?.id || null);
    } catch (e) {
      setError(String(e));
    }
  };
  const deleteEdge = (edge: Edge) => {
    const data = edge.data as { kind?: string; source?: string; target?: string } | undefined;
    if (data?.kind === "dependency" && data.source && data.target) {
      commit(disconnectStep(definition, data.source, data.target));
      return;
    }
    if (data?.kind === "reject" && data.source && data.target) {
      const next = cloneDefinition(definition);
      const step = next.steps.find((item) => item.id === data.source);
      if (step) step.on_reject = (step.on_reject || []).filter((id) => id !== data.target);
      commit(next);
    }
  };

  const deleteSelected = () => {
    if (!selectedNodeId) return;
    const dependents = (definition.steps || []).filter((step) =>
      (step.depends_on || []).includes(selectedNodeId)
      || (step.on_reject || []).includes(selectedNodeId));
    if (dependents.length && !window.confirm(`该节点被 ${dependents.length} 个节点依赖，确定删除？`)) return;
    const next = removeStep(definition, selectedNodeId);
    commit(next, autoLayout(next));
    setSelectedNodeId(null);
  };

  const connectNodes = (source: string, target: string) => {
    const targetStep = definition.steps.find((step) => step.id === target);
    if ((targetStep?.depends_on || []).includes(source)) {
      setNotice(`${target} 已经依赖 ${source}，无需重复连接`);
      return;
    }
    try {
      const next = connectStep(definition, source, target);
      commit(next);
      setNotice(`已建立依赖：${source} → ${target}`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const changeEdgeEndpoints = (edge: Edge, patch: { source?: string; target?: string }) => {
    const data = edge.data as { kind?: string; source?: string; target?: string } | undefined;
    if (!data?.source || !data.target) return;
    const source = patch.source || data.source;
    const target = patch.target || data.target;
    if (source === target) {
      setError("边的起点和终点不能相同");
      return;
    }
    try {
      if (data.kind === "dependency") {
        let next = disconnectStep(definition, data.source, data.target);
        next = connectStep(next, source, target);
        commit(next);
      } else {
        const next = cloneDefinition(definition);
        const oldSource = next.steps.find((step) => step.id === data.source);
        if (oldSource) oldSource.on_reject = (oldSource.on_reject || []).filter((id) => id !== data.target);
        const newSource = next.steps.find((step) => step.id === source);
        if (!newSource) throw new Error("打回边来源节点不存在");
        newSource.on_reject = [...new Set([...(newSource.on_reject || []), target])];
        commit(next);
      }
      setNotice(`已更新连线：${source} → ${target}`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const removeDependency = (source: string, target: string) => {
    commit(disconnectStep(definition, source, target));
  };

  const applyLayout = () => {
    commit(definition, autoLayout(definition));
  };

  // React Flow 的删除键只能整体禁用，否则删除节点会绕过 definition/撤销栈；
  // 这里只接管画布上的 Delete/Backspace，并避开输入控件。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Delete" && event.key !== "Backspace") return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (selectedEdge) {
        event.preventDefault();
        deleteEdge(selectedEdge);
      } else if (selectedNodeId) {
        event.preventDefault();
        deleteSelected();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  const saveDraft = async (): Promise<WorkflowTemplate | null> => {
    if (!template) return null;
    const saved = await api.saveWorkflowTemplateDraft(template.id, {
      definition, layout, name: meta.name, description: meta.description,
      use_mode: meta.use_mode, base_revision: template.draft?.revision,
    });
    setTemplate(saved);
    setIssues(saved.draft?.validation || []);
    setDirty(false);
    setTemplates((items) => items.map((item) => item.id === saved.id ? { ...item, name: saved.name, updated_at: saved.updated_at } : item));
    return saved;
  };
  const onSave = async () => {
    setBusy(true); setError(null);
    try {
      if (draftSource) await saveDraftSource();
      else await saveDraft();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const onValidate = async () => {
    if (!template) return;
    setBusy(true); setError(null);
    try {
      const result = await api.validateWorkflowTemplate(template.id, { definition, layout });
      setIssues(result.issues || []);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const onPublish = async () => {
    if (!template) return;
    setBusy(true); setError(null);
    try {
      if (dirty) await saveDraft();
      const published = await api.publishWorkflowTemplate(template.id);
      setTemplate(published);
      setIssues(published.published?.validation || []);
      setDirty(false);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const createBlank = async () => {
    setBusy(true); setError(null);
    try {
      const created = await api.createWorkflowTemplate({ name: "新工作流模板", definition: blankDefinition() });
      await refresh(created.id);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const filtered = templates.filter((item) => {
    if (!q.trim()) return true;
    const text = `${item.name} ${item.description}`.toLowerCase();
    return text.includes(q.trim().toLowerCase());
  });
  const editable = Boolean(template || draftSource);

  return (
    <div className="templates-page">
      <PageHeader
        kicker="TEMPLATES"
        title="工作流模板"
        subtitle="从 Cindy 的 workflow 或草案导入，编辑步骤/依赖/loop，发布后用于任务规划或人工套用。"
      />
      {error && <p className="error">{error}</p>}
      <div className="templates-studio">
        <aside className="wft-library">
          <div className="wft-library-head">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索模板…" />
            <button className="small" onClick={createBlank} disabled={busy}>新建</button>
            <button className="small" onClick={() => setShowImport(true)} disabled={busy}>导入</button>
          </div>
          <div className="wft-template-list">
            {filtered.length === 0 && <p className="muted">暂无模板。新建空白模板，或从任务 workflow / 草案导入。</p>}
            {filtered.map((item) => (
              <button
                key={item.id}
                className={`wft-template-item ${template?.id === item.id ? "active" : ""}`}
                title={item.description ? `${item.name}\n${item.description}` : item.name}
                onClick={() => loadTemplate(item.id).catch((e) => setError(String(e)))}
              >
                <span className="wft-template-name">{item.name}</span>
                <span className="wft-template-meta">
                  {item.published_version ? `v${item.published_version}` : "未发布"}
                  {item.has_draft ? " · 草稿" : ""}
                  {item.use_mode === "seed" ? " · 规划种子" : item.use_mode === "manual" ? " · 人工套用" : " · 通用"}
                </span>
                <span className="muted wft-template-desc">{item.description || "无描述"}</span>
              </button>
            ))}
          </div>
        </aside>

        <section className="wft-canvas-wrap">
          <div className="wft-toolbar">
            <div className="wft-toolbar-row wft-toolbar-main">
              <input className="wft-name" value={meta.name} disabled={!editable}
                title={meta.name || "模板名称"}
                onChange={(e) => { setMeta((m) => ({ ...m, name: e.target.value })); setDirty(true); }}
                placeholder="模板名称" />
              <span className="wft-dirty">
                {draftSource
                  ? "草案模式"
                  : dirty ? "未保存" : template?.published_version ? `已发布 v${template.published_version}` : "草稿"}
              </span>
              <span className="wft-toolbar-spacer" />
              <button className="small" onClick={onSave} disabled={!editable || busy}>
                {draftSource ? "保存草案" : "保存草稿"}
              </button>
              {draftSource ? (
                <button className="primary small" onClick={saveAsTemplate} disabled={busy}>另存为模板</button>
              ) : (
                <button className="primary small" onClick={onPublish} disabled={!template || busy}>发布</button>
              )}
            </div>
            <div className="wft-toolbar-row wft-toolbar-tools">
              <button className="small" onClick={undo} disabled={!past.length} title="撤销"><Icon name="refresh" size={12} />撤销</button>
              <button className="small" onClick={redo} disabled={!future.length} title="重做"><Icon name="refresh" size={12} />重做</button>
              <button className="small" onClick={() => addNewStep("task")} disabled={!editable}>+ 步骤</button>
              <button className="small" onClick={addNewLoop} disabled={!editable}>+ 循环</button>
              <button className="small" onClick={applyLayout} disabled={!editable}>自动布局</button>
              <button className="small" onClick={() => flowRef.current?.fitView({
                padding: 0.12, maxZoom: 1, minZoom: 0.25, duration: 220,
              })} disabled={!editable}>适应视图</button>
              <button className="small" onClick={onValidate} disabled={!template || busy}>校验</button>
              {draftSource && (
                <button className="small" onClick={() => setShowDraftDiff(true)} disabled={!draftOrigin}>
                  对比原稿
                </button>
              )}
              <button className="small" onClick={() => setShowVersions(true)} disabled={!template}>版本</button>
            </div>
          </div>
          {notice && <p className="muted wft-notice">{notice}</p>}
          {editable && (
            <p className="muted wft-canvas-hint">
              拖节点右侧锚点到目标左侧锚点新建依赖；单击连线选中，按 Delete/Backspace 或使用右侧检查器删除；
              拖动选中连线的端点可改接。橙色虚线是决策节点的打回边。
            </p>
          )}
          <div className="wft-canvas">
            {!editable ? (
              <p className="muted wft-canvas-empty">选择或新建一个模板。</p>
            ) : (
              <ReactFlow
                key={template?.id || draftSource?.id}
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                onInit={(instance) => { flowRef.current = instance; }}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onReconnect={(oldEdge, connection) => {
                  if (!connection.source || !connection.target) return;
                  changeEdgeEndpoints(oldEdge, { source: connection.source, target: connection.target });
                }}
                onNodesDelete={(deleted) => {
                  const first = deleted[0];
                  if (first) { setSelectedNodeId(first.id); }
                }}
                onEdgesDelete={onEdgesDelete}
                onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedEdge(null); }}
                onEdgeClick={(_, edge) => { setSelectedNodeId(null); setSelectedEdge(edge); }}
                onPaneClick={() => { setSelectedNodeId(null); setSelectedEdge(null); }}
                onNodeDragStop={(_, node) => {
                  setLayout((prev) => ({ ...prev, [node.id]: { x: node.position.x, y: node.position.y } }));
                  setDirty(true);
                }}
                fitView
                fitViewOptions={{ padding: 0.08, maxZoom: 1, minZoom: 0.2 }}
                defaultViewport={{ x: 20, y: 20, zoom: 1 }}
                minZoom={0.2}
                maxZoom={1.6}
                deleteKeyCode={null}
                proOptions={{ hideAttribution: true }}
              >
                <Background gap={22} size={1} color="#3a435c" />
                <Controls showInteractive={false} />
                <MiniMap pannable zoomable nodeColor="#60a5fa" maskColor="rgba(10,10,16,.6)" />
              </ReactFlow>
            )}
          </div>
        </section>

        <aside className="wft-inspector">
          <TemplateInspector
            definition={definition}
            selectedStep={selectedStep}
            selectedEdge={selectedEdge}
            selectedLoop={selectedLoop}
            issues={issues}
            meta={meta}
            template={template}
            roles={roles}
            draftSource={draftSource}
            draftMapping={draftMapping}
            onDraftMapping={setDraftMapping}
            onMeta={(patch) => { setMeta((m) => ({ ...m, ...patch })); setDirty(true); }}
            onDefinition={changeDefinition}
            onSelectStep={setSelectedNodeId}
            onDeleteStep={deleteSelected}
            onDeleteEdge={deleteEdge}
            onUpdateEdge={changeEdgeEndpoints}
            onConnectNodes={connectNodes}
            onRemoveDependency={removeDependency}
            onDeleteSlot={(key) => {
              if (usedSlots(definition).includes(key)) {
                setError(`Meeple ${key} 仍被步骤使用，不能删除`);
                return;
              }
              changeDefinition({
                ...definition,
                role_slots: definition.role_slots.filter((slot) => slot.key !== key),
              });
            }}
            onAddSlot={(label, role) => {
              changeDefinition({
                ...definition,
                role_slots: [...definition.role_slots, {
                  key: meepleKey(role), label, role,
                }],
              });
            }}
          />
        </aside>
      </div>

      {showImport && (
        <ImportDialog
          onClose={() => setShowImport(false)}
          onImported={async (id, warnings) => {
            setShowImport(false);
            await refresh(id);
            if (warnings?.length) setNotice(`导入提示：${warnings.join("；")}`);
          }}
        />
      )}
      {showVersions && template && (
        <VersionDialog
          template={template}
          onClose={() => setShowVersions(false)}
          onRollback={async (version) => {
            setBusy(true); setError(null);
            try {
              await api.rollbackWorkflowTemplate(template.id, version);
              await loadTemplate(template.id);
              setShowVersions(false);
              setNotice(`已从 v${version} 创建回滚草稿，发布前仍可继续调整。`);
            } catch (e) { setError(String(e)); } finally { setBusy(false); }
          }}
        />
      )}
      {showDraftDiff && draftOrigin && (
        <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && setShowDraftDiff(false)}>
          <div className="modal wft-version-modal" role="dialog" aria-modal="true">
            <div className="modal-head">
              <h2>草案 · 原稿与当前修改</h2>
              <button className="modal-close" onClick={() => setShowDraftDiff(false)} aria-label="关闭">
                <Icon name="x" size={12} />
              </button>
            </div>
            <div className="wft-diff">
              {diffTemplate(draftOrigin, definition).length === 0
                ? <p className="muted">尚未修改原稿。</p>
                : (
                  <ul>
                    {diffTemplate(draftOrigin, definition).map((line, i) => (
                      <li key={i} className={line.kind}>{line.text}</li>
                    ))}
                  </ul>
                )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function EdgeDetails({ definition, edge }: {
  definition: WorkflowTemplateDefinition;
  edge: Edge;
}) {
  const data = edge.data as { kind?: string; source?: string; target?: string } | undefined;
  const source = definition.steps.find((step) => step.id === data?.source);
  const target = definition.steps.find((step) => step.id === data?.target);
  const isReject = data?.kind === "reject";
  return (
    <div className="wft-edge-details">
      <span className={`wft-edge-kind ${isReject ? "reject" : ""}`}>
        {isReject ? "决策打回" : "依赖"}
      </span>
      <div>{data?.source} · {source?.title || "未知节点"}</div>
      <div className="muted">→ {data?.target} · {target?.title || "未知节点"}</div>
    </div>
  );
}

function MeepleRoleInput({ definition, slot, onDefinition }: {
  definition: WorkflowTemplateDefinition;
  slot: TemplateRoleSlot;
  onDefinition: (next: WorkflowTemplateDefinition) => void;
}) {
  const current = roleNameOf(slot);
  const [value, setValue] = useState(current);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(current);
    setError(null);
  }, [slot.key, current]);

  const commit = () => {
    const role = value.trim().toLowerCase();
    if (!ROLE_NAME_RE.test(role)) {
      setError("role 名需为 2–32 位小写英文、数字、- 或 _");
      return;
    }
    const nextKey = meepleKey(role);
    if (nextKey !== slot.key && definition.role_slots.some((item) => item.key === nextKey)) {
      setError("该 role 名已被其他 Meeple 使用");
      return;
    }
    if (nextKey === slot.key && role === current) {
      setError(null);
      return;
    }
    let next = definition;
    if (nextKey !== slot.key) next = renameMeepleKey(next, slot.key, nextKey);
    const target = next.role_slots.find((item) => item.key === nextKey);
    if (target) target.role = role;
    onDefinition(next);
    setError(null);
  };

  return (
    <label className="wft-slot-field">
      <input value={value} aria-label="Role" maxLength={32}
        placeholder="role 名，如 tester"
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
      {error && <span className="wft-inline-warning">{error}</span>}
    </label>
  );
}

function TemplateInspector({ definition, selectedStep, selectedEdge, selectedLoop, issues, meta, template,
  roles, draftSource, draftMapping, onDraftMapping,
  onMeta, onDefinition, onSelectStep, onDeleteStep, onDeleteEdge,
  onUpdateEdge, onConnectNodes, onRemoveDependency, onDeleteSlot, onAddSlot }: {
  definition: WorkflowTemplateDefinition;
  selectedStep: TemplateStep | null;
  selectedEdge: Edge | null;
  selectedLoop: WorkflowTemplateDefinition["loop"];
  issues: TemplateValidationIssue[];
  meta: DraftMeta;
  template: WorkflowTemplate | null;
  roles: Role[];
  draftSource: BoardDraft | null;
  draftMapping: Record<string, string>;
  onDraftMapping: (mapping: Record<string, string>) => void;
  onMeta: (patch: Partial<DraftMeta>) => void;
  onDefinition: (next: WorkflowTemplateDefinition) => void;
  onSelectStep: (id: string | null) => void;
  onDeleteStep: () => void;
  onDeleteEdge: (edge: Edge) => void;
  onUpdateEdge: (edge: Edge, patch: { source?: string; target?: string }) => void;
  onConnectNodes: (source: string, target: string) => void;
  onRemoveDependency: (source: string, target: string) => void;
  onDeleteSlot: (key: string) => void;
  onAddSlot: (label: string, role: string) => void;
}) {
  const [meepleLabelInput, setMeepleLabelInput] = useState("");
  const [meepleRoleInput, setMeepleRoleInput] = useState("");
  const [meepleError, setMeepleError] = useState<string | null>(null);
  const slots = definition.role_slots || [];
  const used = usedSlots(definition);
  const edgeData = selectedEdge?.data as { kind?: string; source?: string; target?: string } | undefined;
  const isRejectEdge = edgeData?.kind === "reject";
  const edgeSource = edgeData?.source || selectedEdge?.source || "";
  const edgeTarget = edgeData?.target || selectedEdge?.target || "";

  const updateStep = (patch: Partial<TemplateStep>) => {
    if (!selectedStep) return;
    const next = cloneDefinition(definition);
    const step = next.steps.find((item) => item.id === selectedStep.id);
    if (step) Object.assign(step, patch);
    onDefinition(next);
  };
  const updateLoopPhase = (phase: string, patch: Partial<TemplateLoopPhase>) => {
    if (!selectedLoop) return;
    const next = cloneDefinition(definition);
    if (!next.loop) return;
    const spec = (next.loop as unknown as Record<string, TemplateLoopPhase | string>)[phase] as TemplateLoopPhase | undefined;
    if (!spec) return;
    Object.assign(spec, patch);
    onDefinition(next);
  };

  return (
    <div className={`wft-inspector-body ${selectedStep || selectedEdge ? "has-context" : ""}`}>
      <section className="wft-panel wft-template-panel">
        <h3>模板信息</h3>
        <div className="field">
          <span>使用方式</span>
          <select value={meta.use_mode} onChange={(e) => onMeta({ use_mode: e.target.value as DraftMeta["use_mode"] })}>
            <option value="both">Cindy 种子 + 人工套用</option>
            <option value="seed">仅 Cindy 规划种子</option>
            <option value="manual">仅人工套用</option>
          </select>
        </div>
        <div className="field">
          <span>描述</span>
          <textarea rows={2} value={meta.description} onChange={(e) => onMeta({ description: e.target.value })}
            placeholder="这个模板适用于什么任务…" />
        </div>
        <div className="muted wft-panel-note">
          {template?.published_version ? `已发布 v${template.published_version}` : "尚未发布"}
          {template?.draft ? ` · 草稿 rev ${template.draft.revision}` : ""}
        </div>
      </section>

      {draftSource && (
        <section className="wft-panel">
          <h3>草案角色映射</h3>
          <p className="muted wft-panel-note">
            保存草案时会把 Meeple 编译回具体角色；必须映射到来源任务中的角色。
          </p>
          {used.map((key) => {
            const slot = slots.find((item) => item.key === key);
            return (
              <div className="field" key={key}>
                <span>{slot?.label || key} · {key}</span>
                <select value={draftMapping[key] || ""}
                  onChange={(e) => onDraftMapping({ ...draftMapping, [key]: e.target.value })}>
                  <option value="">选择角色…</option>
                  {roles.filter((role) => role.enabled).map((role) => (
                    <option key={role.slug} value={role.slug}>{role.display_name} · {role.slug}</option>
                  ))}
                </select>
              </div>
            );
          })}
        </section>
      )}

      <section className="wft-panel wft-meeple-panel">
        <h3>Meeple · {slots.length}</h3>
        <div className="wft-slots">
          {slots.map((slot) => (
            <div className="wft-slot" key={slot.key}>
              <div className="wft-slot-head">
                <strong>{slot.label}</strong>
                <code title="内部引用 ID；修改 Role 时同步更新">{slot.key}</code>
                <span className={used.includes(slot.key) ? "wft-slot-used" : "muted"}>
                  {used.includes(slot.key) ? "使用中" : "未使用"}
                </span>
                <button className="icon-btn wft-slot-delete" title="删除 Meeple"
                  onClick={() => onDeleteSlot(slot.key)}><Icon name="x" size={11} /></button>
              </div>
              <div className="wft-slot-fields">
                <label className="wft-slot-field">
                  <input value={slot.label} aria-label="Meeple 名称" placeholder="Meeple 名称"
                    onChange={(e) => {
                      const next = cloneDefinition(definition);
                      const target = next.role_slots.find((item) => item.key === slot.key);
                      if (target) target.label = e.target.value;
                      onDefinition(next);
                    }} />
                </label>
                <MeepleRoleInput definition={definition} slot={slot}
                  onDefinition={onDefinition} />
                <label className="wft-slot-field">
                  <select value={slot.bound_role || ""}
                    title="绑定角色（可选）：有绑定时 Cindy 优先使用"
                    onChange={(e) => {
                      const next = cloneDefinition(definition);
                      const target = next.role_slots.find((item) => item.key === slot.key);
                      if (target) target.bound_role = e.target.value || null;
                      onDefinition(next);
                    }}>
                    <option value="">不绑定（由 Cindy 匹配）</option>
                    {slot.bound_role && !roles.some(
                      (role) => role.enabled && role.slug === slot.bound_role,
                    ) && (
                      <option value={slot.bound_role}>{slot.bound_role}（不可用）</option>
                    )}
                    {roles.filter((role) => role.enabled).map((role) => (
                      <option key={role.slug} value={role.slug}>
                        {role.display_name} · {role.slug}
                      </option>
                    ))}
                  </select>

                </label>
              </div>
            </div>
          ))}
        </div>
        <p className="muted wft-panel-note">
          Meeple 是一类角色的代表。绑定角色优先；未绑定时由 Cindy 按 role 与步骤上下文匹配。
        </p>
        <div className="wft-add-meeple">
          <input value={meepleLabelInput} placeholder="中文名，如 测试工程师"
            onChange={(e) => setMeepleLabelInput(e.target.value)} />
          <input value={meepleRoleInput} placeholder="role，如 tester" maxLength={32}
            onChange={(e) => setMeepleRoleInput(e.target.value)} />
          <button className="small" onClick={() => {
            const label = meepleLabelInput.trim();
            const role = meepleRoleInput.trim().toLowerCase();
            if (!label) {
              setMeepleError("请填写 Meeple 中文名");
              return;
            }
            if (!ROLE_NAME_RE.test(role)) {
              setMeepleError("role 名需为 2–32 位小写英文、数字、- 或 _");
              return;
            }
            if (slots.some((slot) => slot.key === meepleKey(role))) {
              setMeepleError(`role_${role} 已存在`);
              return;
            }
            onAddSlot(label, role);
            setMeepleLabelInput("");
            setMeepleRoleInput("");
            setMeepleError(null);
          }}>添加</button>
        </div>
        {meepleError && <p className="error">{meepleError}</p>}
      </section>

      {selectedEdge && (
        <section className="wft-panel">
          <h3>{isRejectEdge ? "打回边" : "依赖边"}</h3>
          <EdgeDetails definition={definition} edge={selectedEdge} />
          <div className="field">
            <span>{isRejectEdge ? "决策节点（来源）" : "来源节点（上游）"}</span>
            <select value={edgeSource}
              onChange={(e) => onUpdateEdge(selectedEdge, { source: e.target.value })}>
              {definition.steps.filter((step) => step.id !== edgeTarget).map((step) => (
                <option key={step.id} value={step.id}>{step.id} · {step.title}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <span>{isRejectEdge ? "被打回节点（目标）" : "目标节点（下游）"}</span>
            <select value={edgeTarget}
              onChange={(e) => onUpdateEdge(selectedEdge, { target: e.target.value })}>
              {definition.steps.filter((step) => step.id !== edgeSource).map((step) => (
                <option key={step.id} value={step.id}>{step.id} · {step.title}</option>
              ))}
            </select>
          </div>
          <p className="muted wft-panel-note">
            {isRejectEdge
              ? "这是决策打回边；删除后决策节点不再自动重置该目标。"
              : "这是依赖边；删除后目标节点不再等待来源节点。也可以拖动连线端点改接。"}
          </p>
          <div className="wft-edge-actions">
            <button className="small danger" onClick={() => onDeleteEdge(selectedEdge)}>
              删除这条边
            </button>
            <span className="muted">或按 Delete / Backspace</span>
          </div>
        </section>
      )}

      {selectedStep ? (
        <section className="wft-panel">
          <div className="wft-panel-title">
            <h3>步骤 {selectedStep.id}</h3>
            <button className="small danger" onClick={onDeleteStep}>删除</button>
          </div>
          <div className="field"><span>标题</span>
            <input value={selectedStep.title} onChange={(e) => updateStep({ title: e.target.value })} />
          </div>
          <div className="wft-field-row">
            <div className="field"><span>类型</span>
              <select value={selectedStep.kind || "task"} onChange={(e) => {
                const kind = e.target.value as TemplateStep["kind"];
                if (kind !== "decision" && (selectedStep.on_reject || []).length
                  && !window.confirm("改为普通步骤会删除该决策节点现有的打回边，确定？")) return;
                updateStep(kind === "decision"
                  ? { kind, max_rework: selectedStep.max_rework ?? 2 }
                  : { kind, on_reject: [] });
              }}>
                <option value="task">普通步骤</option>
                <option value="decision">决策（可配置打回边）</option>
                <option value="plan_slices">切片计划</option>
              </select>
            </div>
            <div className="field"><span>Meeple</span>
              <select value={selectedStep.role_slot} onChange={(e) => updateStep({ role_slot: e.target.value })}>
                {slots.map((slot) => <option key={slot.key} value={slot.key}>{slot.label} · {slot.key}</option>)}
              </select>
            </div>
          </div>
          {(selectedStep.kind || "task") !== "decision" && (
            <p className="muted wft-panel-note">
              非决策步骤没有打回边；需要返工语义时，把类型改为「决策」并配置打回目标。
            </p>
          )}
          <label className="wft-check"><input type="checkbox" checked={Boolean(selectedStep.gate)}
            onChange={(e) => updateStep({ gate: e.target.checked })} /> 人工审核门</label>
          {selectedStep.gate && (
            <div className="field"><span>审核人 Meeple</span>
              <select value={selectedStep.reviewer_slot || ""} onChange={(e) => updateStep({ reviewer_slot: e.target.value || null })}>
                <option value="">未指定</option>
                {slots.map((slot) => <option key={slot.key} value={slot.key}>{slot.label} · {slot.key}</option>)}
              </select>
            </div>
          )}
          <div className="field"><span>指令</span>
            <textarea rows={4} value={selectedStep.instructions || ""}
              onChange={(e) => updateStep({ instructions: e.target.value })} />
          </div>
          <div className="field"><span>验收（每行一条）</span>
            <textarea rows={3} value={(selectedStep.acceptance || []).join("\n")}
              onChange={(e) => updateStep({ acceptance: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) })} />
          </div>
          <div className="field"><span>上游依赖</span>
            <div className="wft-chips">
              {(selectedStep.depends_on || []).length === 0 && <span className="muted">无</span>}
              {(selectedStep.depends_on || []).map((dep) => (
                <span className="wft-chip" key={dep}>
                  {dep}
                  <button className="wft-chip-x" title={`移除依赖 ${dep}`}
                    onClick={() => onRemoveDependency(dep, selectedStep.id)}>
                    <Icon name="x" size={10} />
                  </button>
                </span>
              ))}
            </div>
            <select value="" onChange={(e) => {
              if (e.target.value) onConnectNodes(e.target.value, selectedStep.id);
            }}>
              <option value="">+ 添加上游依赖…</option>
              {definition.steps
                .filter((step) => step.id !== selectedStep.id
                  && !(selectedStep.depends_on || []).includes(step.id))
                .map((step) => <option key={step.id} value={step.id}>{step.id} · {step.title}</option>)}
            </select>
            <span className="muted wft-panel-note">
              也可以从画布节点右侧锚点拖到另一个节点左侧锚点；成环会被拒绝。
            </span>
          </div>
          {selectedStep.kind === "decision" && (() => {
            const ancestors = ancestorsOf(definition, selectedStep.id);
            const candidates = definition.steps
              .filter((step) => step.id !== selectedStep.id)
              .map((step) => {
                const checked = (selectedStep.on_reject || []).includes(step.id);
                const reason = step.gate ? "审核门不可打回"
                  : !ancestors.has(step.id) ? "不是上游节点" : "";
                return { step, checked, reason, disabled: Boolean(reason) && !checked };
              })
              .sort((a, b) => Number(Boolean(a.reason)) - Number(Boolean(b.reason))
                || Number(b.checked) - Number(a.checked));
            return (
              <>
                <div className="field"><span>打回目标（仅上游节点）</span>
                  <div className="wft-check-list">
                    {candidates.map(({ step, checked, reason, disabled }) => (
                      <label key={step.id} className={`wft-check ${disabled ? "is-disabled" : ""}`}
                        title={reason || "打回时重做该节点"}>
                        <input type="checkbox" checked={checked} disabled={disabled}
                          onChange={(e) => {
                            const current = new Set(selectedStep.on_reject || []);
                            if (e.target.checked) current.add(step.id); else current.delete(step.id);
                            updateStep({ on_reject: [...current] });
                          }} />
                        <span>{step.id} · {step.title}</span>
                        {reason && <span className="wft-check-reason">{reason}</span>}
                      </label>
                    ))}
                  </div>
                  <span className="muted wft-panel-note">
                    只有决策节点有打回边。打回目标必须是当前节点的直接/间接上游；运行时还会把目标到本决策之间需要一起重做的中间步骤重置。
                  </span>
                  {!candidates.some((item) => !item.reason) && (
                    <p className="wft-inline-warning">
                      当前节点没有可打回的上游节点；请先从画布把上游节点连到它。
                    </p>
                  )}
                  {(selectedStep.on_reject || []).length === 0 && (
                    <p className="wft-inline-warning">
                      决策节点至少需要一个打回目标，否则无法通过校验。
                    </p>
                  )}
                </div>
                <div className="field"><span>最大重做次数</span>
                  <input type="number" min={0} value={selectedStep.max_rework ?? 2}
                    onChange={(e) => updateStep({ max_rework: Number(e.target.value) })} />
                </div>
              </>
            );
          })()}
          {selectedStep.kind === "plan_slices" && !selectedLoop && (
            <p className="wft-inline-warning">切片计划还没有 loop 模板；请用顶部「+ 循环」添加。</p>
          )}
        </section>
      ) : (
        <section className="wft-panel"><p className="muted">选择画布节点编辑属性。</p></section>
      )}

      {selectedLoop && selectedStep?.kind === "plan_slices" && (
        <section className="wft-panel">
          <h3>Loop 模板</h3>
          {PHASES.map((phase) => {
            const spec = phaseSpec(selectedLoop, phase);
            if (!spec) return null;
            return (
              <details className="wft-loop-phase" key={phase} open={phase === "execute"}>
                <summary>{PHASE_LABEL[phase]} <span className="muted">{spec.title || ""}</span></summary>
                <div className="field"><span>标题</span>
                  <input value={spec.title || ""} onChange={(e) => updateLoopPhase(phase, { title: e.target.value })} />
                </div>
                <div className="field"><span>Meeple</span>
                  <select value={spec.role_slot} onChange={(e) => updateLoopPhase(phase, { role_slot: e.target.value })}>
                    {slots.map((slot) => <option key={slot.key} value={slot.key}>{slot.label} · {slot.key}</option>)}
                  </select>
                </div>
                <div className="field"><span>指令</span>
                  <textarea rows={3} value={spec.instructions || ""}
                    onChange={(e) => updateLoopPhase(phase, { instructions: e.target.value })} />
                </div>
                {phase === "decide" && (
                  <div className="field"><span>最大重做次数</span>
                    <input type="number" min={0} value={spec.max_rework ?? 2}
                      onChange={(e) => updateLoopPhase(phase, { max_rework: Number(e.target.value) })} />
                  </div>
                )}
              </details>
            );
          })}
        </section>
      )}

      <section className="wft-panel">
        <h3>校验 · {issues.filter((i) => i.severity === "error").length} 错误</h3>
        {issues.length === 0 && <p className="muted">暂无问题。</p>}
        <ul className="wft-issues">
          {issues.map((issue, index) => (
            <li key={`${issue.code}-${index}`} className={`wft-issue ${issue.severity}`}
              onClick={() => issue.node_id && onSelectStep(issue.node_id)}>
              <span>{issue.severity === "error" ? "错误" : "提示"}</span>
              {issue.message}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function VersionDialog({ template, onClose, onRollback }: {
  template: WorkflowTemplate;
  onClose: () => void;
  onRollback: (version: number) => Promise<void>;
}) {
  const [diff, setDiff] = useState<{ title: string; lines: TemplateDiffLine[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = template.draft || template.published;
  const origin = current?.origin_definition || template.published?.origin_definition
    || template.draft?.origin_definition || null;

  const showOrigin = () => {
    if (!current) return;
    setDiff({
      title: "AI/导入原稿 → 当前定义",
      lines: diffTemplate(origin, current.definition),
    });
  };
  const showVersion = async (version: number) => {
    if (!current) return;
    setBusy(true); setError(null);
    try {
      const data = await api.getWorkflowTemplateVersion(template.id, version);
      setDiff({
        title: `v${version} → 当前${template.draft ? "草稿" : "发布版"}`,
        lines: diffTemplate(data.definition, current.definition),
      });
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const rollback = async (version: number) => {
    if (!window.confirm(`从 v${version} 创建回滚草稿？当前草稿会被替换，历史版本不会删除。`)) return;
    setBusy(true); setError(null);
    try { await onRollback(version); } catch (e) { setError(String(e)); setBusy(false); }
  };

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wft-version-modal" role="dialog" aria-modal="true">
        <div className="modal-head">
          <h2>版本与差异</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭"><Icon name="x" size={12} /></button>
        </div>
        {error && <p className="error">{error}</p>}
        <div className="wft-version-actions">
          <button className="small" onClick={showOrigin} disabled={!origin || !current}>
            与 AI/导入原稿对比
          </button>
          {template.published_version && (
            <button className="small" onClick={() => showVersion(template.published_version!)} disabled={busy || !current}>
              与当前发布版 v{template.published_version} 对比
            </button>
          )}
        </div>
        {diff ? (
          <div className="wft-diff">
            <div className="wft-diff-head">
              <strong>{diff.title}</strong>
              <button className="small" onClick={() => setDiff(null)}>返回版本列表</button>
            </div>
            {diff.lines.length === 0 && <p className="muted">没有差异。</p>}
            <ul>
              {diff.lines.map((line, i) => (
                <li key={i} className={line.kind}>{line.text}</li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="wft-version-list">
            {(template.versions || []).length === 0 && <p className="muted">暂无版本。</p>}
            {(template.versions || []).map((version) => (
              <div className="wft-version-row" key={version.version}>
                <span className="mono">v{version.version}</span>
                <span className={`status status-${version.state === "published" ? "done" : "todo"}`}>
                  {version.state === "published" ? "已发布" : "草稿"}
                </span>
                <span className="muted">rev {version.revision}</span>
                <span className="muted">{version.published_at || version.created_at}</span>
                <button className="small" onClick={() => showVersion(version.version)} disabled={busy}>差异</button>
                <button className="small" onClick={() => rollback(version.version)} disabled={busy}>回滚到此版</button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ImportDialog({ onClose, onImported }: {
  onClose: () => void;
  onImported: (id: string, warnings?: string[]) => Promise<void>;
}) {
  const [sources, setSources] = useState<{
    workflows: { id: string; task_title: string; status: string }[];
    drafts: { id: string; task_title?: string | null; goal: string }[];
    sops: { name: string; steps: string[] }[];
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.listTemplateImportSources().then(setSources).catch((e) => setError(String(e)));
  }, []);
  const importSource = async (kind: "workflow" | "draft", id: string, name: string) => {
    setBusy(true); setError(null);
    try {
      const created = await api.importWorkflowTemplate(kind, id, `${name} · 模板`);
      await onImported(created.id, created.warnings);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wft-import-modal" role="dialog" aria-modal="true">
        <div className="modal-head"><h2>导入工作流</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭"><Icon name="x" size={12} /></button>
        </div>
        {error && <p className="error">{error}</p>}
        {!sources && <p className="muted">加载中…</p>}
        {sources && (
          <>
            <h3 className="wft-import-heading">已落 workflow</h3>
            {sources.workflows.length === 0 && <p className="muted">暂无。</p>}
            {sources.workflows.map((wf) => (
              <div className="wft-import-row" key={wf.id}>
                <span>{wf.task_title}</span><span className="muted">{wf.status}</span>
                <button className="small" disabled={busy}
                  onClick={() => importSource("workflow", wf.id, wf.task_title)}>导入</button>
              </div>
            ))}
            <h3 className="wft-import-heading">待审 workflow 草案</h3>
            {sources.drafts.length === 0 && <p className="muted">暂无。</p>}
            {sources.drafts.map((draft) => (
              <div className="wft-import-row" key={draft.id}>
                <span>{draft.task_title || draft.goal}</span><span className="muted">pending</span>
                <button className="small" disabled={busy}
                  onClick={() => importSource("draft", draft.id, draft.task_title || draft.goal)}>导入</button>
              </div>
            ))}
            <p className="muted wft-import-note">旧 SOP：{sources.sops.map((sop) => sop.name).join(" / ")}（后续支持直接导入）</p>
          </>
        )}
      </div>
    </div>
  );
}
