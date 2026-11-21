import { useEffect, type ReactNode } from "react";
import type { KnowledgeKind, QualityReport } from "../../api/client";
import { Icon } from "../../components/Icon";
import {
  KNOWLEDGE_CARD_FIELDS,
  KNOWLEDGE_QUALITY_FIELDS,
  qualityFieldLabel,
} from "../../knowledgeCard";
import {
  QualityBadge,
  QualityMissing,
  QualityWarnings,
} from "./KnowledgeQuality";

function hasValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value as object).length > 0;
  return true;
}

function FieldValue({ value }: { value: unknown }) {
  if (typeof value === "string" || typeof value === "number"
    || typeof value === "boolean") {
    return <span className="knowledge-field-text">{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    return (
      <ul className="knowledge-field-list">
        {value.map((item, index) => (
          <li key={index}>{String(item)}</li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    return <pre className="knowledge-field-json">{JSON.stringify(value, null, 2)}</pre>;
  }
  return <span className="knowledge-field-text">{String(value ?? "")}</span>;
}

/** 完整知识字段：期望字段全部列出（未填写也显示），额外字段排在后面。 */
export function KnowledgeFieldList({ kind, fields }: {
  kind?: string | null;
  fields?: Record<string, unknown> | null;
}) {
  const values = { ...(fields || {}) };
  if (kind === "insight") {
    if (!hasValue(values.counterexamples) && hasValue(values.exclusions)) {
      values.counterexamples = values.exclusions;
    }
    delete values.exclusions;
  }
  const expected: { key: string; label: string }[] = [
    ...KNOWLEDGE_QUALITY_FIELDS.map(({ key, label }) => ({ key, label })),
    ...((kind && KNOWLEDGE_CARD_FIELDS[kind as KnowledgeKind]) || []),
  ];
  const seen = new Set<string>();
  const rows = expected.filter(({ key }) => {
    if (kind === "insight" && key === "exclusions") return false;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  for (const key of Object.keys(values)) {
    if (kind === "insight" && key === "exclusions") continue;
    if (!seen.has(key)) rows.push({ key, label: qualityFieldLabel(key) });
  }
  if (rows.length === 0) return <p className="muted">没有结构化字段。</p>;
  return (
    <dl className="knowledge-fields">
      {rows.map(({ key, label }) => {
        const value = values[key];
        const filled = hasValue(value);
        return (
          <div className={`knowledge-field ${filled ? "" : "is-empty"}`} key={key}>
            <dt>{label}</dt>
            <dd>
              {filled
                ? <FieldValue value={value} />
                : <span className="muted">未填写</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export function KnowledgeQualityDetail({ quality }: {
  quality?: QualityReport | null;
}) {
  if (!quality) return null;
  return (
    <div className="knowledge-detail-quality">
      <QualityMissing quality={quality} />
      <QualityWarnings quality={quality} />
    </div>
  );
}

/** 所有知识详情共用的弹窗：完整字段 + 正文 + 质量 + 元数据。 */
export function KnowledgeDetailModal({
  title,
  kind,
  tldr,
  fields,
  body,
  quality,
  meta,
  error,
  gate,
  onClose,
  actions,
  heading = "知识详情",
  children,
}: {
  title: string;
  kind?: string | null;
  tldr?: string;
  fields?: Record<string, unknown> | null;
  body?: string;
  quality?: QualityReport | null;
  meta?: { label: string; value: ReactNode }[];
  error?: string | null;
  gate?: { ready?: boolean; errors?: string[] } | null;
  onClose: () => void;
  actions?: ReactNode;
  heading?: string;
  children?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div className="modal knowledge-detail-modal" role="dialog" aria-modal="true"
        aria-label="知识详情">
        <div className="modal-head">
          <h2>{heading}</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭" autoFocus>
            <Icon name="x" size={12} />
          </button>
        </div>
        <div className="knowledge-detail-body">
          <div className="knowledge-detail-title-row">
            <h3 className="knowledge-detail-title">{title}</h3>
            <QualityBadge quality={quality} />
            {gate && (
              <span className={`knowledge-gate ${gate.ready ? "is-ready" : "is-blocked"}`}>
                {gate.ready ? "已满足入库标准" : "未达入库标准"}
              </span>
            )}
          </div>
          {gate?.ready === false && gate.errors && gate.errors.length > 0 && (
            <p className="knowledge-gate-reasons">
              待补全：{gate.errors.join("；")}
            </p>
          )}
          {tldr && <p className="knowledge-detail-tldr">{tldr}</p>}
          <KnowledgeQualityDetail quality={quality} />
          {error && <p className="error">{error}</p>}
          {children}
          {fields !== undefined && (
            <>
              <h4>完整知识信息</h4>
              <KnowledgeFieldList kind={kind} fields={fields} />
            </>
          )}
          {body && (
            <section className="knowledge-detail-section">
              <h4>知识卡正文</h4>
              <pre className="knowledge-detail-pre">{body}</pre>
            </section>
          )}
          {meta && meta.length > 0 && (
            <dl className="knowledge-detail-meta">
              {meta.map((item) => (
                <div key={item.label} className="knowledge-detail-meta-row">
                  <dt>{item.label}</dt>
                  <dd>{item.value}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}
