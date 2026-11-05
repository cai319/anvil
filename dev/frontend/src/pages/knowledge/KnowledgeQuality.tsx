import type { QualityReport, RepoCard } from "../../api/client";
import { Icon } from "../../components/Icon";
import { qualityFieldLabel } from "../../knowledgeCard";

export function QualityBadge({ quality }: { quality?: QualityReport | null }) {
  if (!quality) return null;
  const title = [
    `质量分 ${quality.score}（${quality.grade}）`,
    ...quality.warnings,
  ].join("\n");
  return (
    <span className={`quality-badge quality-${quality.grade.toLowerCase()}`} title={title}>
      质量 {quality.score} · {quality.grade}
      {quality.legacy && <span className="quality-legacy">历史</span>}
    </span>
  );
}

export function QualityMissing({ quality }: { quality?: QualityReport | null }) {
  if (!quality || quality.blocking.length === 0) return null;
  return (
    <span className="quality-missing">
      {quality.blocking.map((item) => (
        <span className="review-reason" key={item}>缺{qualityFieldLabel(item)}</span>
      ))}
    </span>
  );
}

export function QualityWarnings({ quality }: { quality?: QualityReport | null }) {
  if (!quality || quality.warnings.length === 0) return null;
  return (
    <ul className="knowledge-quality-warnings">
      {quality.warnings.map((warning) => <li key={warning}>{warning}</li>)}
    </ul>
  );
}

export function KnowledgeCleanPreview({ card, preview, busy, onClose, onApply }: {
  card: RepoCard;
  preview: Record<string, unknown>;
  busy: boolean;
  onClose: () => void;
  onApply: () => void;
}) {
  const fields = (preview.fields || {}) as Record<string, unknown>;
  const quality = preview.quality as QualityReport | undefined;
  const citations = (preview.citations || []) as { field?: string; quote?: string }[];
  const warnings = (preview.warnings || []) as string[];
  const changed = Object.entries(fields).filter(([key, value]) => (
    String(card.fields?.[key] ?? "") !== String(value ?? "")
  ));
  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal knowledge-clean-modal" role="dialog" aria-modal="true"
        aria-label="LLM 清洗预览">
        <div className="modal-head">
          <h2>LLM 清洗预览</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭">
            <Icon name="x" size={12} />
          </button>
        </div>
        <div className="knowledge-clean-body">
          <p className="muted">
            仅填充 proposed 卡，不会自动批准。引用不成立的字段已在服务端丢弃。
          </p>
          <div className="knowledge-clean-titles">
            <div><span className="muted">标题</span><strong>{String(preview.title || card.title)}</strong></div>
            <div><span className="muted">TLDR</span><strong>{String(preview.tldr || card.tldr)}</strong></div>
          </div>
          <h3>字段差异</h3>
          {changed.length === 0 ? (
            <p className="muted">没有新增或修改的字段。</p>
          ) : (
            <div className="knowledge-clean-diff">
              {changed.map(([key, value]) => (
                <div className="knowledge-clean-diff-row" key={key}>
                  <span className="knowledge-clean-key">{qualityFieldLabel(key)}</span>
                  <span className="knowledge-clean-before">{String(card.fields?.[key] ?? "（空）")}</span>
                  <span className="knowledge-clean-arrow">→</span>
                  <span className="knowledge-clean-after">{String(value)}</span>
                </div>
              ))}
            </div>
          )}
          {citations.length > 0 && (
            <details className="knowledge-record-details">
              <summary>引用（{citations.length}）</summary>
              <ul>
                {citations.map((citation, index) => (
                  <li key={`${citation.field}-${index}`}>
                    <span className="mono">{citation.field}</span>：{citation.quote}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {warnings.length > 0 && (
            <ul className="knowledge-quality-warnings">
              {warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          )}
          <QualityBadge quality={quality} />
          {quality && quality.blocking.length > 0 && (
            <p className="error">
              清洗后仍有阻断项：{quality.blocking.map(qualityFieldLabel).join("、")}
            </p>
          )}
        </div>
        <div className="modal-actions">
          <button type="button" className="primary" disabled={busy}
            onClick={onApply}>应用清洗（保持待批）</button>
          <button type="button" className="small" onClick={onClose} disabled={busy}>取消</button>
        </div>
      </div>
    </div>
  );
}
