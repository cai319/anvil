import { useEffect, useMemo, useState } from "react";
import {
  api,
  type DecisionRecord,
  type StepPacketResponse,
  type Workflow,
} from "../api/client";
import { formatDateTime } from "../time";
import { Icon } from "./Icon";

/**
 * Human review drawer for a gate or a decision packet.
 *
 * The packet is the unit of review: question, options, acceptance criteria,
 * evidence, upstream conclusions, risks, AI recommendation and decision history.
 * A reject requires a written reason; a human override of an AI decision
 * requires an explicit override reason.
 */

const AUTHORITY_LABEL: Record<string, string> = {
  ai: "AI 决策",
  human: "人工决策",
  joint: "联合决策",
};

const DECISION_LABEL: Record<string, string> = {
  approve: "通过",
  reject: "打回",
  escalate: "升级人工",
  recommend_approve: "建议通过",
  recommend_reject: "建议打回",
};

const EVIDENCE_KIND_LABEL: Record<string, string> = {
  file: "文件",
  test: "测试",
  command: "命令",
  artifact: "产物",
  url: "链接",
  note: "说明",
  summary: "摘要",
};

export function GateReviewDrawer({
  wid,
  sid,
  onClose,
  onDone,
}: {
  wid: string;
  sid: string;
  onClose: () => void;
  onDone: (workflow: Workflow) => void;
}) {
  const [data, setData] = useState<StepPacketResponse | null>(null);
  const [feedback, setFeedback] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [acknowledgeStale, setAcknowledgeStale] = useState(false);
  const [reworkTargets, setReworkTargets] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      setData(await api.getStepPacket(wid, sid));
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [wid, sid]);

  const step = data?.step ?? null;
  const packet = data?.packet ?? null;
  const decisions = data?.decisions ?? [];
  const authority = packet?.authority ?? step?.decision_authority ?? (step?.gate ? "human" : "ai");
  const stale = Boolean(data?.can_decide.stale_evidence);
  const isDecision = step?.kind === "decision";
  const reworkChoices = useMemo(() => {
    if (!step || isDecision) return step?.on_reject ?? [];
    return [...new Set([step.step_id, ...(step.depends_on ?? [])])];
  }, [step, isDecision]);

  useEffect(() => {
    // Rejecting always resets the current step; decision steps default to the
    // full on_reject closure so the human can narrow it deliberately.
    if (step) {
      setReworkTargets([step.step_id, ...(isDecision ? step.on_reject ?? [] : [])]);
    }
  }, [step, isDecision]);

  const submit = async (approve: boolean) => {
    if (!step) return;
    if (!approve && !feedback.trim()) {
      setError("打回必须填写具体原因与修改要求");
      return;
    }
    if (approve && isDecision && authority === "ai" && !overrideReason.trim()) {
      setError("人工覆盖 AI 决策时必须填写覆盖理由");
      return;
    }
    if (approve && stale && !acknowledgeStale) {
      setError("证据已过期，请先确认接受过期证据或刷新证据");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body = {
        approve,
        feedback: feedback.trim() || undefined,
        packet_id: packet?.id,
        rework_targets: approve ? undefined : reworkTargets,
        override_reason: overrideReason.trim() || undefined,
        acknowledge_stale: acknowledgeStale,
      };
      const wf = isDecision
        ? await api.decideWorkflowStep(wid, sid, body)
        : await api.reviewWorkflowStep(
          wid, sid, approve, body.feedback,
          {
            rework_targets: body.rework_targets,
            packet_id: body.packet_id,
            override_reason: body.override_reason,
          });
      onDone(wf);
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const refreshPacket = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await api.refreshStepPacket(wid, sid);
      setData((prev) => prev
        ? { ...prev, packet: out.packet, decisions: out.decisions }
        : prev);
      setAcknowledgeStale(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay gate-drawer-overlay"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal gate-drawer" role="dialog" aria-modal="true"
        aria-label={`审查 ${sid}`}>
        <div className="modal-head">
          <div className="gate-drawer-title">
            <span className="page-kicker">REVIEW PACKET</span>
            <h2>{isDecision ? "人工决策" : "步骤审核"} · {step?.title || sid}</h2>
            <span className="muted mono">{sid}</span>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="关闭">
            <Icon name="x" size={14} />
          </button>
        </div>

        {error && <p className="error gate-drawer-error">{error}</p>}
        {!data && !error && <p className="muted">正在读取审查材料…</p>}

        {data && !packet && (
          <div className="gate-drawer-empty">
            <p className="muted">该步骤还没有待审 packet。只有已提交的审核门或运行中的决策步骤可以审查。</p>
            <div className="modal-actions">
              <button onClick={onClose}>关闭</button>
            </div>
          </div>
        )}

        {packet && step && (
          <>
            <div className="gate-drawer-scroll">
              <div className="gate-question">
                <div className="gate-badges">
                  <span className={`gate-authority is-${authority}`}>
                    {AUTHORITY_LABEL[authority] || authority}
                  </span>
                  <span className="badge">第 {packet.revision} 版</span>
                  <span className={`badge ${packet.status === "pending_human" ? "badge-in_progress" : "badge-todo"}`}>
                    {packet.status === "pending_human" ? "等待人工" : packet.status === "pending_ai" ? "等待 AI" : "已决策"}
                  </span>
                  {stale && <span className="badge gate-stale">证据过期</span>}
                </div>
                <h3>{packet.question}</h3>
                {packet.recommendation?.decision && (
                  <p className="gate-recommendation">
                    AI 建议：{DECISION_LABEL[packet.recommendation.decision] || packet.recommendation.decision}
                    {packet.recommendation.feedback ? ` · ${packet.recommendation.feedback}` : ""}
                    {packet.recommendation.escalated ? " · 已升级" : ""}
                  </p>
                )}
              </div>

              <section className="gate-section">
                <h4>证据 <span className="muted">{packet.evidence.length}</span></h4>
                {packet.evidence.length === 0 && (
                  <p className="muted">未提交可核验证据。{step.evidence_required ? "本步骤要求证据。" : ""}</p>
                )}
                <div className="gate-evidence-list">
                  {packet.evidence.map((e) => (
                    <div key={e.id} className={`gate-evidence ${e.fresh ? "" : "is-stale"}`}>
                      <span className="gate-evidence-kind">{EVIDENCE_KIND_LABEL[e.kind] || e.kind}</span>
                      <div className="gate-evidence-main">
                        <span className="gate-evidence-ref mono" title={e.ref}>{e.ref}</span>
                        {e.label && e.label !== e.ref && <span className="muted">{e.label}</span>}
                      </div>
                      <span className={`badge ${e.fresh ? "badge-done" : "gate-stale"}`}>
                        {e.fresh ? "fresh" : "stale"}
                      </span>
                      {e.captured_at && (
                        <span className="muted gate-evidence-time">{formatDateTime(e.captured_at)}</span>
                      )}
                      {e.digest && (
                        <span className="muted mono gate-evidence-digest" title={e.digest}>
                          {e.digest.slice(0, 12)}…
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </section>

              <section className="gate-section">
                <h4>验收清单</h4>
                {packet.acceptance.length === 0 && <p className="muted">未定义验收标准。</p>}
                <ul className="gate-acceptance">
                  {packet.acceptance.map((a, i) => (
                    <li key={`${a.criterion}-${i}`}>
                      <span className={`gate-check is-${a.status}`}>{a.status === "met" ? "✓" : "?"}</span>
                      <span>{a.criterion}</span>
                      {a.evidence_refs.length > 0 && (
                        <span className="muted">证据 {a.evidence_refs.join("、")}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>

              {packet.upstream.length > 0 && (
                <section className="gate-section">
                  <h4>上游结论</h4>
                  {packet.upstream.map((u) => (
                    <div key={u.step_id} className="gate-upstream">
                      <span className="mono">{u.step_id}</span>
                      <strong>{u.title || u.step_id}</strong>
                      <span className="muted">{u.decision}</span>
                      {u.summary && <p className="muted">{u.summary}</p>}
                    </div>
                  ))}
                </section>
              )}

              {packet.risks.length > 0 && (
                <section className="gate-section">
                  <h4>风险与升级触发</h4>
                  <div className="gate-tags">
                    {packet.risks.map((r) => <span key={r} className="badge gate-risk">{r}</span>)}
                  </div>
                </section>
              )}

              <section className="gate-section">
                <h4>决策记录 <span className="muted">{decisions.length}</span></h4>
                {decisions.length === 0 && <p className="muted">暂无历史决策。</p>}
                <div className="gate-decision-list">
                  {decisions.map((d: DecisionRecord) => (
                    <div key={d.id} className="gate-decision">
                      <span className={`badge ${d.decision === "reject" ? "badge-in_review" : "badge-done"}`}>
                        {DECISION_LABEL[d.decision] || d.decision}
                      </span>
                      <span className="muted">{d.decider_type === "human" ? "自然人" : d.decider}</span>
                      <span className="muted">{formatDateTime(d.created_at)}</span>
                      {d.feedback && <span className="gate-decision-feedback">{d.feedback}</span>}
                      {d.rework_targets.length > 0 && (
                        <span className="muted">返工：{d.rework_targets.join("、")}</span>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            </div>

            <div className="gate-drawer-actions">
              <label className="gate-field">
                <span>
                  决策说明
                  {isDecision && authority === "ai" ? "（打回或人工覆盖 AI 时必填）" : "（打回时必填）"}
                </span>
                <textarea rows={3} value={feedback}
                  placeholder={isDecision ? "写明批准依据；打回时列出必须修改的问题" : "写明通过依据；打回时列出修改要求"}
                  onChange={(e) => setFeedback(e.target.value)} />
              </label>

              {isDecision && authority === "ai" && (
                <label className="gate-field">
                  <span>覆盖理由（人工覆盖 AI 决策时必填）</span>
                  <input value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)}
                    placeholder="为什么人类判断与 AI 不同" />
                </label>
              )}

              {stale && (
                <label className="gate-check-line">
                  <input type="checkbox" checked={acknowledgeStale}
                    onChange={(e) => setAcknowledgeStale(e.target.checked)} />
                  我已确认证据可能过期，仍按当前材料决策
                </label>
              )}

              {reworkChoices.length > 0 && (
                <details className="gate-rework">
                  <summary>打回返工范围 · {reworkTargets.length} 步</summary>
                  <div className="gate-rework-list">
                    {reworkChoices.map((target) => (
                      <label key={target} className="gate-check-line">
                        <input type="checkbox"
                          checked={reworkTargets.includes(target)}
                          disabled={isDecision ? false : target === step.step_id}
                          onChange={(e) => setReworkTargets((prev) =>
                            e.target.checked
                              ? [...new Set([...prev, target])]
                              : prev.filter((x) => x !== target))} />
                        <span className="mono">{target}</span>
                        {target === step.step_id ? "（当前审核步骤，总是重做）" : ""}
                      </label>
                    ))}
                  </div>
                </details>
              )}

              <div className="gate-actions-row">
                <button className="small" disabled={busy} onClick={refreshPacket}>刷新证据</button>
                <span className="gate-actions-spacer" />
                <button className="small danger" disabled={busy} onClick={() => submit(false)}>
                  打回重做
                </button>
                <button className="primary" disabled={busy} onClick={() => submit(true)}>
                  {busy ? "提交中…" : isDecision ? "批准决策" : "通过并放行"}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
