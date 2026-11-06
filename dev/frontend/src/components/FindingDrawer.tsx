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

const ENTRY_SEPARATORS = /[；;：:，,。.]/;
const ENTRY_NUMBERS: Record<string, number> = {
  一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5,
  六: 6, 七: 7, 八: 8, 九: 9,
};

function parseEntryNumber(raw: string): number | null {
  if (/^\d+$/.test(raw)) return Number(raw);
  let total = 0;
  let current = 0;
  for (const ch of raw) {
    if (ch === "百") {
      total += (current || 1) * 100;
      current = 0;
    } else if (ch === "十") {
      total += (current || 1) * 10;
      current = 0;
    } else if (ENTRY_NUMBERS[ch]) {
      current = ENTRY_NUMBERS[ch];
    } else {
      return null;
    }
  }
  total += current;
  return total || null;
}

/**
 * 仅用于展示：把「（1）...；（2）...」这类分条内容拆成多行。
 *
 * 严格按序号链识别：只在 1→2→3 的连续编号前拆行，且要求编号前最近的非空白字符
 * 是分隔符（或已有换行）。单条内容内部的普通分隔符、内部子列表不会被当作边界。
 * 原始 finding 字段不做修改。
 */
export function formatFindingEntries(text: string): string {
  if (!text) return text;
  const normalized = text.replace(/\r\n/g, "\n");
  const markerRe = /([（(【\[])\s*(\d+|[一二三四五六七八九十百两]+)\s*([）)】\]])|(\d+|[一二三四五六七八九十百两]+)(\.(?!\d)|[、)])/g;
  type EntryMarker = {
    start: number;
    end: number;
    number: number;
    style: "paren" | "bare";
  };
  const markers: EntryMarker[] = [];
  for (const match of normalized.matchAll(markerRe)) {
    const start = match.index ?? 0;
    const rawNumber = match[2] ?? match[4] ?? "";
    const number = parseEntryNumber(rawNumber);
    if (number === null) continue;
    markers.push({
      start,
      end: start + match[0].length,
      number,
      style: match[1] ? "paren" : "bare",
    });
  }

  const chains: EntryMarker[][] = [];
  for (let i = 0; i < markers.length; i++) {
    if (markers[i].number !== 1) continue;
    const chain = [markers[i]];
    let expected = 2;
    for (let j = i + 1; j < markers.length; j++) {
      const candidate = markers[j];
      if (candidate.style !== chain[0].style || candidate.number !== expected) {
        continue;
      }
      const previous = chain[chain.length - 1];
      const between = normalized.slice(previous.end, candidate.start);
      const tail = between.replace(/[ \t]+$/, "").slice(-1);
      // 条目边界只认编号前最近的分隔符；已有换行也视为边界。
      if (!tail || (!ENTRY_SEPARATORS.test(tail) && tail !== "\n")) continue;
      chain.push(candidate);
      expected += 1;
    }
    if (chain.length >= 2) chains.push(chain);
  }
  const chain = chains.sort(
    (a, b) => b.length - a.length || a[0].start - b[0].start,
  )[0];
  if (!chain) return normalized;

  let out = normalized;
  const cuts = chain
    .map((marker) => marker.start)
    .filter((position) => {
      const prefix = normalized.slice(0, position).replace(/[ \t]+$/, "");
      const tail = prefix.slice(-1);
      // 冒号/分隔符在编号前时，把编号换到下一行；已有换行则不重复插入。
      return Boolean(tail && ENTRY_SEPARATORS.test(tail));
    })
    .sort((a, b) => b - a);
  for (const position of cuts) {
    const before = out.slice(0, position).replace(/[ \t]+$/, "");
    out = `${before}\n${out.slice(position)}`;
  }
  return out.replace(/\n{3,}/g, "\n\n");
}

/** 单条 finding 详情与人工决策。Web 侧只读展示；写入由审核者通过 CLI 完成。 */
export function FindingDrawer({ taskId, findingId, schema, trackerConfigured = false,
  onClose, onChanged }: {
  taskId: string;
  findingId: string;
  schema: FindingSchema | null;
  trackerConfigured?: boolean;
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

  const exportFinding = async () => {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.exportFinding(taskId, findingId);
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

  type DrawerField = {
    label: string;
    value: string;
    group: "summary" | "location" | "detail";
    wide?: boolean;
  };
  const fields: DrawerField[] = finding ? [
    // 判定摘要：短值，等宽平铺
    { label: "风险类型", value: riskLabel(schema, finding.risk_type), group: "summary" },
    { label: "严重等级", value: severityLabel(finding.severity), group: "summary" },
    { label: "修复优先级",
      value: schema?.fix_priorities?.[finding.fix_priority] || finding.fix_priority,
      group: "summary" },
    { label: "问题最终置信度", value: `${finding.confidence}%`, group: "summary" },
    { label: "PoC 状态",
      value: schema?.poc_statuses?.[finding.poc_status] || finding.poc_status,
      group: "summary" },
    // 问题定位：模块/函数短，文件独占宽列
    { label: "问题所在模块", value: finding.module, group: "location" },
    { label: "问题函数", value: finding.function_name, group: "location" },
    { label: "文件与行号", value: `${finding.file_path}:${finding.line_numbers}`,
      group: "location", wide: true },
    // 详细信息：长文本整行全宽
    { label: "触发前置条件", value: finding.preconditions, group: "detail" },
    { label: "触发点", value: finding.trigger_point, group: "detail" },
    { label: "问题风险描述", value: finding.risk_description, group: "detail" },
    { label: "推理链（CoT）", value: finding.reasoning_chain, group: "detail" },
    { label: "攻击路径（数据流）", value: finding.attack_path, group: "detail" },
    { label: finding.poc_status === "provided" ? "技术证明（PoC）" : "PoC 豁免原因",
      value: finding.poc, group: "detail" },
    { label: "修复建议", value: finding.fix_suggestion, group: "detail" },
  ] : [];

  const summaryFields = fields.filter((f) => f.group === "summary");
  const locationFields = fields.filter((f) => f.group === "location");
  const detailFields = fields.filter((f) => f.group === "detail");
  const actionable = finding?.status === "submitted";
  const decided = finding?.status === "confirmed" || finding?.status === "false_positive";
  const canExport = finding?.status === "submitted"
    || finding?.status === "confirmed" || finding?.status === "changes_requested";

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
                {finding.external_id && (
                  <span className="chip finding-external-badge">
                    外部 #{finding.external_id}
                  </span>
                )}
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

            <section className="finding-section">
              <div className="finding-section-head">
                <h3>判定摘要</h3>
                <span>5 项短字段 · 一行平铺</span>
              </div>
              <div className="finding-summary-band">
                {summaryFields.map((f) => (
                  <div key={f.label} className="finding-summary-card">
                    <h3>{f.label}</h3>
                    <p>{formatFindingEntries(f.value) || "—"}</p>
                  </div>
                ))}
              </div>
            </section>

            <section className="finding-section">
              <div className="finding-section-head">
                <h3>问题定位</h3>
                <span>模块 / 函数 / 文件</span>
              </div>
              <div className="finding-location-grid">
                {locationFields.map((f) => (
                  <div key={f.label}
                    className={`finding-location-card ${f.wide ? "is-file" : ""}`}>
                    <h3>{f.label}</h3>
                    <p>{formatFindingEntries(f.value) || "—"}</p>
                  </div>
                ))}
              </div>
            </section>

            <section className="finding-section">
              <div className="finding-section-head">
                <h3>详细信息</h3>
                <span>长文本整行展示</span>
              </div>
              <div className="finding-field-list">
                {detailFields.map((f) => (
                  <section key={f.label} className="finding-field">
                    <h3>{f.label}</h3>
                    <pre>{formatFindingEntries(f.value) || "—"}</pre>
                  </section>
                ))}
              </div>
            </section>

            {(finding.decided_by || finding.decision_note) && (
              <section className="finding-decision-record">
                <h3>人工决策记录</h3>
                <p className="muted">
                  {finding.decided_by || "human"} · {finding.decided_at || "—"}
                </p>
                {finding.decision_note && <p className="finding-decision-note">{finding.decision_note}</p>}
              </section>
            )}
          </div>
        )}

        {finding && (actionable || decided || canExport) && (
          <div className="finding-drawer-footer">
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
            {canExport && (
              <div className="finding-decisions finding-drawer-external">
                <button className="primary" disabled={busy || !trackerConfigured}
                  onClick={() => { void exportFinding(); }}>
                  提交到问题跟踪
                </button>
                {finding.external_id ? (
                  <span className="muted">
                    已提交 #{finding.external_id}
                    {finding.external_submitted_at ? ` · ${finding.external_submitted_at}` : ""}
                  </span>
                ) : !trackerConfigured ? (
                  <a className="small button-like" href="/tooling">去工具页配置问题跟踪服务器</a>
                ) : null}
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
