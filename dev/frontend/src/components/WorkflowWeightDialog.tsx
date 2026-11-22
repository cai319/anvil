import { useState } from "react";
import { api, type Workflow, type WorkflowWeight } from "../api/client";
import { Icon } from "./Icon";
import { confirmDialog } from "./dialogs";

export const WEIGHT_META: Record<WorkflowWeight, { label: string; hint: string }> = {
  light: { label: "轻量", hint: "小范围、可逆、少量角色" },
  standard: { label: "标准", hint: "默认流程强度" },
  strict: { label: "严格", hint: "高风险、证据必填、关键节点人工确认" },
};

export function WorkflowWeightDialog({ taskId, current, onClose, onApplied }: {
  taskId: string;
  current: Workflow | null;
  onClose: () => void;
  onApplied: (workflow?: Workflow) => void;
}) {
  const [weight, setWeight] = useState<WorkflowWeight>(current?.weight || "standard");
  const [reason, setReason] = useState(current?.weight_reason || "");
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.setTaskWorkflowWeight>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const running = current?.status === "running";

  const save = async () => {
    setBusy(true); setError(null);
    try {
      const out = await api.setTaskWorkflowWeight(taskId, weight, reason);
      setResult(out);
      if (!out.requires_restart) {
        onApplied();
        onClose();
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const restart = async () => {
    const ok = await confirmDialog({
      title: "重新开始工作流执行？",
      message: "将终止当前执行并创建新的执行。旧执行、步骤和产物保留在历史中，"
        + "不会中途替换。",
      confirmLabel: "终止并重跑",
      danger: true,
    });
    if (!ok) return;
    setBusy(true); setError(null);
    try {
      const wf = await api.restartTaskWorkflow(
        taskId, reason.trim() || "人工调整流程强度后重新执行");
      onApplied(wf);
      onClose();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const clear = async () => {
    setBusy(true); setError(null);
    try {
      const out = await api.clearTaskWorkflowWeight(taskId);
      setResult(out);
      if (!out.requires_restart) {
        onApplied();
        onClose();
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wf-weight-modal modal-flex" role="dialog" aria-modal="true">
        <div className="modal-head">
          <h2>调整流程强度</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭">
            <Icon name="x" size={14} />
          </button>
        </div>
        <div className="modal-flex-body">
        {error && <p className="error">{error}</p>}
        <p className="muted">
          当前执行：{current?.weight ? WEIGHT_META[current.weight].label : "未设置"}
          {current?.weight_source ? `（${current.weight_source}）` : ""}
          {running ? " · 运行中，修改只对下一次执行生效" : ""}
        </p>
        <div className="wf-weight-options">
          {(Object.keys(WEIGHT_META) as WorkflowWeight[]).map((key) => (
            <label key={key} className={`wf-weight-option ${weight === key ? "is-active" : ""}`}>
              <input type="radio" name="wf-weight" checked={weight === key}
                onChange={() => setWeight(key)} />
              <strong>{WEIGHT_META[key].label}</strong>
              <span className="muted">{WEIGHT_META[key].hint}</span>
            </label>
          ))}
        </div>
        <label className="field">
          <span>选择依据（可选）</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="例如：仅文档改动，可逆；或涉及支付，必须严格" />
        </label>
        {result?.requires_restart && (
          <div className="wf-weight-restart-note">
            <p>
              已保存为下一次执行使用。当前第 {result.running_execution_no} 次执行仍为
              {result.running_weight ? WEIGHT_META[result.running_weight].label : "原强度"}。
              需要立即生效时，可终止并重新执行。
            </p>
            <button className="small danger" disabled={busy} onClick={restart}>
              终止并重新执行
            </button>
          </div>
        )}
        </div>
        <div className="modal-actions">
          {current?.weight_source === "human" && (
            <button className="small" disabled={busy} onClick={clear}>清除人工设置</button>
          )}
          <button className="small" disabled={busy} onClick={onClose}>取消</button>
          <button className="primary" disabled={busy} onClick={save}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

