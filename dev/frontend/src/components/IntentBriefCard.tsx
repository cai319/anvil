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
  taskId, state, onChanged,
}: {
  taskId: string;
  state: ClarificationState | null;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draftText, setDraftText] = useState("{}");
  const [weight, setWeight] = useState<WorkflowWeight>("standard");
  const [planShape, setPlanShape] = useState<ClarificationPlanShape>("auto");
  const [ackOpen, setAckOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const weightTouched = useRef(false);
  const shapeTouched = useRef(false);
  const lastScope = useRef("");

  useEffect(() => {
    if (!state) return;
    setDraftText(JSON.stringify(state.brief ?? {}, null, 2));
    setAckOpen(false);
    setEditing(false);
    setError(null);
  }, [state?.task_id, state?.status, state?.brief_version]);

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

  const thinking = busy || state.thinking;
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
      title: "跳过澄清，直接规划？",
      message: "Cindy 将不产出任务简报，直接按当前任务描述规划工作流。",
      confirmLabel: "跳过并规划",
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
      <div className="ibc-head">
        <strong>任务简报</strong>
        <span className="badge">{STATUS_LABEL[state.status] ?? state.status}</span>
        {state.status === "in_progress" && (
          <span className="muted">第 {state.round}/{state.max_rounds} 轮</span>
        )}
        {state.status === "stale" && (
          <span className="ibc-warn">{state.stale_reason || "任务定义已变更"}</span>
        )}
        <span className="ibc-head-right">
          {thinking && (
            <span className="ibc-thinking" title="Cindy 正在思考"
              aria-label="Cindy 正在思考">
              <Icon name="hourglass" size={15} className="ibc-hourglass" />
            </span>
          )}
          {state.brief_version > 0 && (
            <span className="muted">v{state.brief_version}</span>
          )}
        </span>
      </div>

      <BriefSummary brief={state.brief} weight={weight} planShape={planShape} />

      {openQuestions.length > 0 && (
        <ul className="ibc-open">
          {openQuestions.map((q, i) => <li key={i}>{q}</li>)}
        </ul>
      )}

      {active && (
        <>
          <div className="toolbar ibc-controls">
            <label className="field-inline">
              流程强度
              <select value={weight} disabled={busy}
                onChange={(e) => {
                  weightTouched.current = true;
                  setWeight(e.target.value as WorkflowWeight);
                }}>
                <option value="light">轻量</option>
                <option value="standard">标准</option>
                <option value="strict">严格</option>
              </select>
            </label>
            <label className="field-inline">
              计划形态
              <select value={planShape} disabled={busy}
                onChange={(e) => {
                  shapeTouched.current = true;
                  setPlanShape(e.target.value as ClarificationPlanShape);
                }}>
                <option value="auto">自动判断</option>
                <option value="flat">扁平静态步骤</option>
                <option value="slices">切片 + 闭环</option>
              </select>
            </label>
            {openQuestions.length > 0 && (
              <label className="checkline">
                <input type="checkbox" checked={ackOpen} disabled={busy}
                  onChange={(e) => setAckOpen(e.target.checked)} />
                我已接受这些开放问题
              </label>
            )}
          </div>
          {editing && (
            <textarea className="ibc-editor mono" value={draftText} disabled={busy}
              onChange={(e) => setDraftText(e.target.value)} rows={12} />
          )}
          <div className="toolbar">
            <button className="small" disabled={busy}
              onClick={() => setEditing((v) => !v)}>
              {editing ? "收起编辑" : "编辑简报"}
            </button>
            {state.status === "in_progress" && editing && (
              <button className="small" disabled={busy} onClick={saveDraft}>保存草稿</button>
            )}
            <button className="primary" disabled={busy
              || (!state.ready && !editing)
              || (openQuestions.length > 0 && !ackOpen)} onClick={confirm}>
              确认开始规划
            </button>
            <button className="small danger" disabled={busy} onClick={skip}>
              跳过澄清，直接规划
            </button>
          </div>
        </>
      )}

      {state.status === "confirmed" && (
        <>
          {editing && (
            <textarea className="ibc-editor mono" value={draftText} disabled={busy}
              onChange={(e) => setDraftText(e.target.value)} rows={12} />
          )}
          <div className="toolbar">
            {state.can_edit ? (
              <button className="small" disabled={busy}
                onClick={() => setEditing((v) => !v)}>
                {editing ? "收起编辑" : "编辑简报"}
              </button>
            ) : (
              <span className="muted">
                Cindy 正在规划工作流；完成或终止规划后可编辑简报。
              </span>
            )}
            {editing && state.can_edit && (
              <button className="small primary" disabled={busy}
                onClick={saveConfirmed}>保存修改</button>
            )}
          </div>
        </>
      )}

      {state.status === "skipped" && state.can_start && (
        <div className="toolbar">
          <button className="small" disabled={busy} onClick={start}>重新澄清</button>
        </div>
      )}

      {error && <p className="error">{error}</p>}
    </section>
  );
}
