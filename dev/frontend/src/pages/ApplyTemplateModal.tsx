import { useEffect, useMemo, useState } from "react";
import { api, type Role, type Task, type WorkflowTemplate, type WorkflowTemplateDefinition } from "../api/client";
import { Icon } from "../components/Icon";
import { roleNameOf, usedSlots } from "../templates/editorModel";

type Preview = {
  issues?: { severity: string; message: string; slot_key?: string | null }[];
  uncovered_assignees?: string[];
  conflicts?: {
    running_workflow?: boolean;
    running_workflow_id?: string | null;
    running_execution_no?: number | null;
    pending_drafts?: number;
    task_terminal?: boolean;
  };
};


export function ApplyTemplateModal({ task, roles, onClose, onApplied }: {
  task: Task;
  roles: Role[];
  onClose: () => void;
  onApplied: (workflow: Record<string, unknown>) => void;
}) {
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [template, setTemplate] = useState<WorkflowTemplate | null>(null);
  const [templateId, setTemplateId] = useState("");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<Preview | null>(null);
  const [restartRunning, setRestartRunning] = useState(false);
  const [restartReason, setRestartReason] = useState("");
  const [rejectPending, setRejectPending] = useState(false);
  const [ackUncovered, setAckUncovered] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listWorkflowTemplates(undefined, "active").then((list) => {
      const usable = list.filter((item) => item.published_version
        && (item.use_mode === "manual" || item.use_mode === "both"));
      setTemplates(usable);
      if (usable[0]) setTemplateId(usable[0].id);
    }).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!templateId) return;
    api.getWorkflowTemplate(templateId).then((data) => {
      setTemplate(data);
      const def = data.published?.definition as WorkflowTemplateDefinition | undefined;
      if (!def) return;
      const next: Record<string, string> = {};
      for (const key of usedSlots(def)) {
        const slot = def.role_slots.find((item) => item.key === key);
        const bound = slot?.bound_role
          && roles.some((role) => role.enabled && role.slug === slot.bound_role)
          ? slot.bound_role : "";
        const suggested = slot?.suggested_roles?.find((slug) =>
          task.assignees.includes(slug));
        const validSuggested = slot?.suggested_roles?.find((slug) =>
          roles.some((role) => role.enabled && role.slug === slug));
        next[key] = bound || suggested || task.assignees[0] || validSuggested
          || roles.find((role) => role.enabled)?.slug || "";
      }
      setMapping(next);
      setPreview(null);
      setRestartRunning(false);
      setRestartReason("");
      setRejectPending(false);
      setAckUncovered(false);
    }).catch((e) => setError(String(e)));
  }, [templateId, task.assignees, roles]);

  const slots = useMemo(() => {
    const def = template?.published?.definition;
    if (!def) return [];
    return usedSlots(def).map((key) => {
      const slot = def.role_slots.find((item) => item.key === key);
      return { key, slot, roleName: slot ? roleNameOf(slot) : "" };
    });
  }, [template]);

  const check = async () => {
    if (!template?.published_version) return;
    setBusy(true); setError(null);
    try {
      const result = await api.previewApplyTemplate(template.id, {
        task_id: task.id,
        version: template.published_version,
        role_mapping: mapping,
      });
      setPreview(result as Preview);
      const conflicts = (result as Preview).conflicts;
      if (!conflicts?.running_workflow) {
        setRestartRunning(false);
        setRestartReason("");
      }
      if ((conflicts?.pending_drafts || 0) > 0) setRejectPending(true);
      if (((result as Preview).uncovered_assignees || []).length) setAckUncovered(true);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const apply = async () => {
    if (!template?.published_version) return;
    setBusy(true); setError(null);
    try {
      const result = await api.applyTemplateToTask(task.id, {
        template_id: template.id,
        version: template.published_version,
        role_mapping: mapping,
        restart_running: restartRunning,
        restart_reason: restartReason.trim() || undefined,
        reject_pending_drafts: rejectPending,
        acknowledge_uncovered: ackUncovered,
        mode: "direct",
      });
      onApplied(result.workflow as Record<string, unknown>);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const errors = (preview?.issues || []).filter((issue) => issue.severity === "error");
  const warnings = (preview?.issues || []).filter((issue) => issue.severity !== "error");

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wft-apply-modal" role="dialog" aria-modal="true">
        <div className="modal-head">
          <h2>套用工作流模板</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭"><Icon name="x" size={12} /></button>
        </div>
        {error && <p className="error">{error}</p>}
        <div className="field">
          <span>已发布模板</span>
          <select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
            {templates.length === 0 && <option value="">暂无可套用模板</option>}
            {templates.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · v{item.published_version}
              </option>
            ))}
          </select>
        </div>

        {slots.length > 0 && (
          <div className="wft-apply-mapping">
            <h3>Meeple 映射</h3>
            {slots.map(({ key, slot, roleName }) => (
              <div className="wft-apply-slot" key={key}>
                <div className="wft-apply-slot-info">
                  <div>
                    <strong>{slot?.label || key}</strong>
                    <span className="muted"> role: {roleName}</span>
                    <span className="muted"> · {key}</span>
                  </div>
                  {slot?.bound_role && (
                    <p className="muted wft-apply-slot-desc">
                      优先绑定预置角色：{roles.find((role) => role.slug === slot.bound_role)?.display_name
                        || slot.bound_role}
                    </p>
                  )}
                </div>
                <select value={mapping[key] || ""} onChange={(e) =>
                  setMapping((prev) => ({ ...prev, [key]: e.target.value }))}>
                  <option value="">选择角色…</option>
                  {roles.filter((role) => role.enabled).map((role) => (
                    <option key={role.slug} value={role.slug}>
                      {role.display_name}{task.assignees.includes(role.slug) ? "（已参与）" : ""}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        )}

        {preview && (
          <div className="wft-apply-preview">
            {errors.length > 0 && (
              <div className="error">
                {errors.map((issue, i) => <div key={i}>{issue.message}</div>)}
              </div>
            )}
            {warnings.map((issue, i) => (
              <div className="muted" key={i}>提示：{issue.message}</div>
            ))}
            {preview.conflicts?.running_workflow && (
              <div className="wft-restart-conflict">
                <label className="wft-check">
                  <input type="checkbox" checked={restartRunning}
                    onChange={(e) => setRestartRunning(e.target.checked)} />
                  终止第 {preview.conflicts.running_execution_no || 1} 次执行并重新开始
                </label>
                <p className="muted wft-panel-note">
                  旧执行会标记为「已终止」并保留在任务历史中；新执行从第一步开始，不会中途替换。
                </p>
                <div className="field">
                  <span>重新执行原因</span>
                  <input value={restartReason} onChange={(e) => setRestartReason(e.target.value)}
                    placeholder="为什么需要终止当前执行？" />
                </div>
              </div>
            )}
            {(preview.conflicts?.pending_drafts || 0) > 0 && (
              <label className="wft-check">
                <input type="checkbox" checked={rejectPending}
                  onChange={(e) => setRejectPending(e.target.checked)} />
                确认拒绝当前任务的 {preview.conflicts?.pending_drafts} 个待审 workflow 草案
              </label>
            )}
            {(preview.uncovered_assignees?.length || 0) > 0 && (
              <label className="wft-check">
                <input type="checkbox" checked={ackUncovered}
                  onChange={(e) => setAckUncovered(e.target.checked)} />
                确认任务角色未被模板覆盖：{preview.uncovered_assignees?.join("、")}
              </label>
            )}
            {errors.length === 0 && warnings.length === 0 && (
              <p className="muted">校验通过，可以套用。</p>
            )}
          </div>
        )}

        <div className="modal-actions">
          <button className="small" onClick={onClose} disabled={busy}>取消</button>
          <button className="small" onClick={check} disabled={busy || !template}>检查冲突</button>
          <button className="primary small" onClick={apply}
            disabled={busy || !template || errors.length > 0
              || Boolean(preview?.conflicts?.running_workflow && !restartRunning)}
            title={errors.length ? "请先修复校验错误"
              : preview?.conflicts?.running_workflow && !restartRunning
                ? "请先确认终止当前执行并重新开始"
                : "人类确认后直接创建 workflow"}>
            确认套用并启动
          </button>
        </div>
      </div>
    </div>
  );
}
