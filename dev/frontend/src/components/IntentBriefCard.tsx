import { useEffect, useRef, useState } from "react";
import {
  api,
  type ClarificationPlanShape,
  type ClarificationState,
  type IntentBrief,
  type WorkflowWeight,
} from "../api/client";
import { confirmDialog } from "./dialogs";
import { Icon } from "./Icon";

const STATUS_LABEL: Record<string, string> = {
  none: "待澄清",
  in_progress: "澄清中",
  confirmed: "简报已确认",
  skipped: "已跳过",
  stale: "简报已过期",
};

const WEIGHT_LABEL: Record<WorkflowWeight, string> = {
  light: "轻量",
  standard: "标准",
  strict: "严格",
};

const SHAPE_LABEL: Record<ClarificationPlanShape, string> = {
  flat: "扁平静态步骤",
  slices: "切片 + 闭环",
  auto: "自动判断",
};

function listText(values?: string[]): string {
  return (values ?? []).filter(Boolean).join("；");
}

function BriefSummary({
  brief, weight, planShape,
}: {
  brief: IntentBrief | null;
  weight: WorkflowWeight;
  planShape: ClarificationPlanShape;
}) {
  if (!brief) {
    return <p className="muted">尚无简报草稿；Cindy 会在澄清轮次中逐步补齐。</p>;
  }
  const rows: [string, string][] = [
    ["目标", brief.goal || "—"],
    ["真实意图", brief.true_intent || "—"],
    ["非目标", listText(brief.non_goals) || "—"],
    ["约束", listText(brief.constraints) || "—"],
    ["验收标准", listText(brief.acceptance) || "—"],
  ];
  return (
    <dl className="ibc-summary">
      {rows.map(([label, value]) => (
        <div key={label} style={{ display: "contents" }}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
      <dt>流程强度</dt>
      <dd>
        {WEIGHT_LABEL[weight]}
        {brief.weight === weight && brief.weight_rationale
          ? `（${brief.weight_rationale}）` : ""}
      </dd>
      <dt>计划形态</dt>
      <dd>
        {SHAPE_LABEL[planShape]}
        {brief.plan_shape === planShape && brief.plan_shape_rationale
          ? `（${brief.plan_shape_rationale}）` : ""}
      </dd>
    </dl>
  );
}

export function IntentBriefCard({
  taskId, state, planningActive, draftPending, hasWorkflow, pendingQuestions,
  onChanged,
}: {
  taskId: string;
  state: ClarificationState | null;
  planningActive: boolean;
  draftPending: boolean;
  hasWorkflow: boolean;
  /** Cindy 已发出问题，但问题消息尚未进入时间线：保持沙漏，避免误以为已轮到我方。 */
  pendingQuestions?: boolean;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draftText, setDraftText] = useState("{}");
  const [weight, setWeight] = useState<WorkflowWeight>("standard");
  const [planShape, setPlanShape] = useState<ClarificationPlanShape>("auto");
  const [ackOpen, setAckOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workspacePreview, setWorkspacePreview] = useState<{
    exists: boolean;
    relative_path: string | null;
    brief: IntentBrief | null;
    error: string | null;
  } | null>(null);
  const weightTouched = useRef(false);
  const shapeTouched = useRef(false);
  const lastScope = useRef("");

  useEffect(() => {
    if (!state) return;
    setDraftText(JSON.stringify(state.brief ?? {}, null, 2));
    setAckOpen(false);
    setEditing(false);
    setError(null);
    setWorkspacePreview(null);
  }, [state?.task_id, state?.status, state?.brief_version]);

  // 换任务或状态变化时回到紧凑栏；用户主动展开/编辑才占用会话高度。
  useEffect(() => {
    setExpanded(false);
  }, [state?.task_id, state?.status]);

  // 用户改过的强度/形态不能被 Cindy 的简报更新重置；只有切换任务/状态时才重新同步。
  useEffect(() => {
    if (!state) return;
    const scope = `${state.task_id}:${state.status}`;
    if (lastScope.current !== scope) {
      lastScope.current = scope;
      weightTouched.current = false;
      shapeTouched.current = false;
    }
    if (!weightTouched.current) {
      setWeight((state.weight as WorkflowWeight)
        || state.brief?.weight || "standard");
    }
    if (!shapeTouched.current) {
      setPlanShape((state.plan_shape as ClarificationPlanShape)
        || state.brief?.plan_shape || "auto");
    }
  }, [state?.task_id, state?.status, state?.brief_version,
      state?.weight, state?.plan_shape]);

  if (!state) return null;
  // 无简报时不显示卡片：任务启动按钮就是「Cindy 先澄清再规划」的唯一入口。
  if (state.status === "none") return null;

  const thinking = busy || state.thinking || Boolean(pendingQuestions);
  const thinkingLabel = pendingQuestions ? "Cindy 正在整理澄清问题" : "Cindy 正在思考";
  const active = state.status === "in_progress" || state.status === "stale";
  const openQuestions = state.brief?.open_questions ?? [];

  const parseDraft = (): IntentBrief | null => {
    try {
      const parsed = JSON.parse(draftText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("简报必须是 JSON 对象");
      }
      return parsed as IntentBrief;
    } catch (e) {
      setError(`简报 JSON 无法解析：${String(e)}`);
      return null;
    }
  };

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const start = () => run(() => api.startClarification(taskId));

  const saveDraft = () => {
    const brief = parseDraft();
    if (!brief) return;
    void run(() => api.saveClarificationDraft(taskId, brief));
  };

  // 工作区 brief.json 只做“查看 → 人工导入为草稿”，绝不会自动进入 DB。
  const previewWorkspaceBrief = () => {
    setBusy(true);
    setError(null);
    void api.previewWorkspaceBrief(taskId)
      .then((preview) => {
        setWorkspacePreview(preview);
        if (preview.error) setError(preview.error);
        setExpanded(true);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  const importWorkspaceBrief = () => {
    const brief = workspacePreview?.brief;
    if (!brief) return;
    void run(async () => {
      await api.importWorkspaceBrief(taskId, brief);
      setWorkspacePreview(null);
    });
  };

  const confirm = () => {
    let brief: IntentBrief | undefined;
    if (editing) {
      const parsed = parseDraft();
      if (!parsed) return;
      brief = parsed;
    }
    void run(() => api.confirmClarification(taskId, {
      brief,
      weight,
      plan_shape: planShape,
      acknowledge_open_questions: ackOpen,
    }));
  };

  const skip = async () => {
    const ok = await confirmDialog({
      title: "不等澄清，直接规划？",
      message: "Cindy 会停止提问，不生成任务简报，直接按当前任务描述规划工作流。",
      confirmLabel: "直接规划",
      danger: false,
    });
    if (!ok) return;
    void run(() => api.skipClarification(taskId));
  };

  const saveConfirmed = () => {
    const brief = parseDraft();
    if (!brief) return;
    void run(() => api.updateClarificationBrief(taskId, brief));
  };

  return (
    <section className={`intent-brief-card is-${state.status}`}>
      <div className="ibc-bar">
        <div className="ibc-bar-main">
          <strong>任务简报</strong>
          <span className="badge">{STATUS_LABEL[state.status] ?? state.status}</span>
          {state.status === "in_progress" && (
            <span className="muted"
              title={`${state.max_rounds} 轮是 Cindy 主动提问的软上限，不是固定议程：`
                + "每轮最多 3 个问题；简报足够完整会提前结束；"
                + "已达上限后只收束、不再开新问题。human 的追问与修正不消耗轮次。"}>
              第 {Math.max(1, state.round)} 轮（上限 {state.max_rounds}）
            </span>
          )}
          {state.status === "stale" && (
            <span className="ibc-warn">{state.stale_reason || "任务定义已变更"}</span>
          )}
          {active && openQuestions.length > 0 && !ackOpen && (
            <span className="ibc-warn-dot" title="有开放问题需展开简报接受" />
          )}
          {thinking && (
            <span className="ibc-thinking" title={thinkingLabel}
              aria-label={thinkingLabel}>
              <Icon name="hourglass" size={15} className="ibc-hourglass" />
            </span>
          )}
          {state.brief_version > 0 && (
            <span className="muted ibc-version">v{state.brief_version}</span>
          )}
        </div>

        {(active || state.status === "confirmed") && (
          <div className="ibc-bar-options">
            {active ? (
              <>
                <label className="field-inline" title="流程强度">
                  强度
                  <select className="ibc-control" value={weight} disabled={busy}
                    onChange={(e) => {
                      weightTouched.current = true;
                      setWeight(e.target.value as WorkflowWeight);
                    }}>
                    <option value="light">轻量</option>
                    <option value="standard">标准</option>
                    <option value="strict">严格</option>
                  </select>
                </label>
                <label className="field-inline" title="计划形态">
                  形态
                  <select className="ibc-control" value={planShape} disabled={busy}
                    onChange={(e) => {
                      shapeTouched.current = true;
                      setPlanShape(e.target.value as ClarificationPlanShape);
                    }}>
                    <option value="auto">自动判断</option>
                    <option value="flat">扁平静态步骤</option>
                    <option value="slices">切片 + 闭环</option>
                  </select>
                </label>
              </>
            ) : (
              <>
                <span className="muted">强度 {WEIGHT_LABEL[weight]}</span>
                <span className="muted">形态 {SHAPE_LABEL[planShape]}</span>
              </>
            )}
          </div>
        )}

        <div className="ibc-bar-actions">
          <button className="small ibc-control" disabled={busy}
            onClick={() => setExpanded((v) => {
              const next = !v;
              if (!next) setEditing(false);
              return next;
            })}>
            {expanded ? "收起" : "展开"}
          </button>

          {active && (
            <>
              <button className="small ibc-control" disabled={busy}
                onClick={() => { setExpanded(true); setEditing((v) => !v); }}>
                {editing ? "完成编辑" : "编辑"}
              </button>
              <button className="small ibc-control" disabled={busy}
                title="只读查看工作区里的 brief.json；导入后仍需结构化确认"
                onClick={previewWorkspaceBrief}>
                查看工作区简报
              </button>
              {state.status === "in_progress" && editing && (
                <button className="small ibc-control" disabled={busy} onClick={saveDraft}>
                  保存草稿
                </button>
              )}
              <button className="primary ibc-control" disabled={busy
                || (!state.ready && !editing)
                || (openQuestions.length > 0 && !ackOpen)} onClick={confirm}
                title={openQuestions.length > 0 && !ackOpen
                  ? "请展开简报并接受开放问题后再确认"
                  : undefined}>
                确认规划
              </button>
              <button className="small danger ibc-control" disabled={busy} onClick={skip}
                title="不等 Cindy 继续提问，不生成任务简报，直接启动 planner 规划工作流">
                直接规划
              </button>
            </>
          )}

          {state.status === "confirmed" && (
            state.can_edit ? (
              <>
                <button className="small ibc-control" disabled={busy}
                  onClick={() => { setExpanded(true); setEditing((v) => !v); }}>
                  {editing ? "完成编辑" : "编辑"}
                </button>
                {editing && (
                  <button className="small primary ibc-control" disabled={busy}
                    onClick={saveConfirmed}>保存修改</button>
                )}
              </>
            ) : planningActive ? (
              <span className="muted ibc-note">Cindy 正在规划工作流…</span>
            ) : draftPending ? (
              <span className="muted ibc-note">规划草案已提交，等待审批</span>
            ) : hasWorkflow ? null : (
              <span className="muted ibc-note">简报已确认，等待启动规划</span>
            )
          )}

          {state.status === "skipped" && state.can_start && (
            <button className="small ibc-control" disabled={busy} onClick={start}>
              重新澄清
            </button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="ibc-body">
          <BriefSummary brief={state.brief} weight={weight} planShape={planShape} />

          {openQuestions.length > 0 && (
            <ul className="ibc-open">
              {openQuestions.map((q, i) => <li key={i}>{q}</li>)}
            </ul>
          )}

          {active && openQuestions.length > 0 && (
            <label className="checkline ibc-ack">
              <input type="checkbox" checked={ackOpen} disabled={busy}
                onChange={(e) => setAckOpen(e.target.checked)} />
              我已接受这些开放问题
            </label>
          )}

          {editing && (active || state.status === "confirmed") && (
            <textarea className="ibc-editor mono" value={draftText} disabled={busy}
              onChange={(e) => setDraftText(e.target.value)} rows={12} />
          )}
        </div>
      )}

      {expanded && workspacePreview && (
        <div className="ibc-body">
          <p className="muted">
            工作区简报
            {workspacePreview.relative_path
              ? `（${workspacePreview.relative_path}，只读预览）` : ""}
          </p>
          {workspacePreview.exists && workspacePreview.brief ? (
            <>
              <textarea className="ibc-editor mono" readOnly rows={12}
                value={JSON.stringify(workspacePreview.brief, null, 2)} />
              {state.status === "in_progress" ? (
                <button className="small ibc-control" disabled={busy}
                  onClick={importWorkspaceBrief}>
                  导入为草稿（仍需确认）
                </button>
              ) : (
                <p className="muted">
                  先启动澄清，才能把工作区简报导入为草稿。
                </p>
              )}
            </>
          ) : (
            <p className="muted">
              {workspacePreview.error || "工作区没有 brief.json。"}
            </p>
          )}
        </div>
      )}

      {error && <p className="error">{error}</p>}
    </section>
  );
}
