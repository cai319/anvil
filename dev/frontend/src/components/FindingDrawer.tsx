import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type Finding, type FindingSchema } from "../api/client";
import { confirmDialog, promptDialog } from "../components/dialogs";
import { Icon } from "../components/Icon";

export const FINDING_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  submitted: "待确认",
  confirmed: "已确认真实",
  false_positive: "误报",
  changes_requested: "需重新评估",
};

export const SEVERITY_LABEL: Record<string, string> = {
  critical: "严重",
  high: "高",
  medium: "中",
  low: "低",
  info: "提示",
};

const SEVERITY_ORDER: Record<string, number> = {
  critical: 0, high: 1, medium: 2, low: 3, info: 4,
};

export function severityLabel(value: string): string {
  return SEVERITY_LABEL[value] || value || "未分级";
}

export function riskLabel(schema: FindingSchema | null, value: string): string {
  return schema?.risk_types?.[value] || value || "未分类";
}

/** 单条 finding 详情与人工决策。Web 侧只读展示；写入由审核者通过 CLI 完成。 */
export function FindingDrawer({ taskId, findingId, schema, onClose, onChanged }: {
  taskId: string;
  findingId: string;
  schema: FindingSchema | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [finding, setFinding] = useState<Finding | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setFinding(null);
    api.getFinding(taskId, findingId)
      .then((f) => { if (alive) setFinding(f); })
      .catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [taskId, findingId, reloadKey]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const decision = async (fn: () => Promise<Finding>) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await fn();
      setFinding(updated);
      setReloadKey((n) => n + 1);
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    const note = await promptDialog({
      title: "确认真实问题",
      message: "确认后该条 finding 锁定为真实问题；如需推翻，可在详情中重新打开。",
      confirmLabel: "确认真实",
      input: { label: "确认备注（可选）", placeholder: "复现结果、影响判断等" },
    });
    if (note === null) return;
    void decision(() => api.confirmFinding(taskId, findingId, note.trim() || undefined));
  };

  const falsePositive = async () => {
    const note = await promptDialog({
      title: "标记误报",
      message: "标记后该条 finding 不再计入真实问题；请尽量写明误报依据。",
      confirmLabel: "标记误报",
      input: { label: "误报依据（建议填写）", placeholder: "例如：输入已白名单、路径不可达" },
    });
    if (note === null) return;
    void decision(() => api.falsePositiveFinding(taskId, findingId, note.trim() || undefined));
  };

  const requestChanges = async () => {
    const note = await promptDialog({
      title: "要求重新评估",
      message: "审核者会被唤醒并收到你的意见；问题板会回到进行中，任务退回进行中。",
      confirmLabel: "退回重评",
      danger: true,
      inputRequired: true,
      input: { label: "重评意见（必填）", placeholder: "请说明需要补充或复核的内容", multiline: true },
    });
    if (!note) return;
    void decision(() => api.requestFindingChanges(taskId, findingId, note.trim()));
  };

  const reopen = async () => {
    const ok = await confirmDialog({
      title: "推翻已有结论？",
      message: "该条 finding 会回到「需重新评估」，并通知审核者复核。",
      confirmLabel: "重新打开",
      danger: true,
    });
    if (!ok) return;
    const note = await promptDialog({
      title: "重新评估原因",
      confirmLabel: "提交",
      inputRequired: true,
      input: { label: "原因（必填）", placeholder: "为什么需要重新评估？", multiline: true },
    });
    if (!note) return;
    void decision(() => api.reopenFinding(taskId, findingId, note.trim()));
  };

  const fields: { label: string; value: string; pre?: boolean }[] = finding ? [
    { label: "风险类型", value: riskLabel(schema, finding.risk_type) },
    { label: "严重等级", value: severityLabel(finding.severity) },
    { label: "修复优先级", value: schema?.fix_priorities?.[finding.fix_priority] || finding.fix_priority },
    { label: "问题最终置信度", value: `${finding.confidence}%` },
    { label: "问题所在模块", value: finding.module },
    { label: "问题函数", value: finding.function_name },
    { label: "文件与行号", value: `${finding.file_path}:${finding.line_numbers}` },
    { label: "触发前置条件", value: finding.preconditions, pre: true },
    { label: "触发点", value: finding.trigger_point, pre: true },
    { label: "问题风险描述", value: finding.risk_description, pre: true },
    { label: "推理链（CoT）", value: finding.reasoning_chain, pre: true },
    { label: "攻击路径（数据流）", value: finding.attack_path, pre: true },
    { label: "PoC 状态", value: schema?.poc_statuses?.[finding.poc_status] || finding.poc_status },
    { label: finding.poc_status === "provided" ? "技术证明（PoC）" : "PoC 豁免原因",
      value: finding.poc, pre: true },
    { label: "修复建议", value: finding.fix_suggestion, pre: true },
  ] : [];

  const actionable = finding?.status === "submitted";
  const decided = finding?.status === "confirmed" || finding?.status === "false_positive";

  return createPortal(
    <div className="modal-overlay finding-drawer-overlay"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="modal finding-drawer" role="dialog" aria-modal="true"
        aria-label={finding?.title || "Finding 详情"}>
        <div className="modal-head finding-drawer-head">
          <div>
            <span className="muted finding-drawer-kicker">安全 finding</span>
            <h2>{finding?.title || "加载中…"}</h2>
            {finding && (
              <div className="finding-badges">
                <span className={`finding-sev is-${finding.severity}`}>
                  {severityLabel(finding.severity)}
                </span>
                <span className={`finding-status is-${finding.status}`}>
                  {FINDING_STATUS_LABEL[finding.status] || finding.status}
                </span>
                <span className="chip">置信度 {finding.confidence}%</span>
                <span className="chip">{riskLabel(schema, finding.risk_type)}</span>
              </div>
            )}
          </div>
          <button className="modal-close" aria-label="关闭" onClick={onClose}>
            <Icon name="x" size={14} />
          </button>
        </div>

        {error && <p className="error">{error}</p>}
        {!finding && !error && <p className="muted">正在加载 finding…</p>}

        {finding && (
          <div className="finding-drawer-body">
            <div className="finding-meta-line muted">
              <span>作者 {finding.author_role}</span>
              <span>更新 {finding.updated_at}</span>
              {finding.submitted_at && <span>提交 {finding.submitted_at}</span>}
            </div>

            {finding.status === "changes_requested" && (
              <div className="finding-callout is-warn">
                已要求重新评估。等待审核者更新并重新提交后，问题板才能完成。
              </div>
            )}
            {finding.status === "draft" && (
              <div className="finding-callout">
                审核者仍在填写草稿，字段不完整时不能提交。
              </div>
            )}

            <div className="finding-field-list">
              {fields.map((f) => (
                <section key={f.label} className={`finding-field ${f.pre ? "is-long" : ""}`}>
                  <h3>{f.label}</h3>
                  {f.pre ? <pre>{f.value || "—"}</pre> : <p>{f.value || "—"}</p>}
                </section>
              ))}
            </div>

            {(finding.decided_by || finding.decision_note) && (
              <section className="finding-decision-record">
                <h3>人工决策记录</h3>
                <p className="muted">
                  {finding.decided_by || "human"} · {finding.decided_at || "—"}
                </p>
                {finding.decision_note && <p className="finding-decision-note">{finding.decision_note}</p>}
              </section>
            )}

            {actionable && (
              <div className="finding-decisions">
                <button className="primary" disabled={busy} onClick={confirm}>确认真实问题</button>
                <button disabled={busy} onClick={falsePositive}>标记误报</button>
                <button className="danger" disabled={busy} onClick={requestChanges}>要求重新评估</button>
              </div>
            )}
            {decided && (
              <div className="finding-decisions">
                <button className="small" disabled={busy} onClick={reopen}>推翻结论，重新评估</button>
              </div>
            )}
          </div>
        )}
      </aside>
    </div>,
    document.body,
  );
}

export function sortFindings(items: Finding[]): Finding[] {
  return [...items].sort((a, b) => {
    const sev = (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9);
    if (sev !== 0) return sev;
    if (a.confidence !== b.confidence) return b.confidence - a.confidence;
    return (b.updated_at || "").localeCompare(a.updated_at || "");
  });
}
