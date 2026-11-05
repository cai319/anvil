import { useEffect, useMemo, useState } from "react";
import {
  api,
  type KnowledgeKind,
  type QualityReport,
  type RepoCard,
  type TaskKnowledge,
} from "../../api/client";
import { Icon } from "../../components/Icon";
import {
  KNOWLEDGE_CARD_FIELDS,
  KNOWLEDGE_QUALITY_FIELDS,
  qualityFieldLabel,
} from "../../knowledgeCard";
import { QualityBadge } from "./KnowledgeQuality";

type EditableCard = Pick<
  RepoCard,
  "id" | "kind" | "title" | "tldr" | "fields" | "tags" | "authority" |
  "quality" | "body_md"
> | Pick<
  TaskKnowledge,
  "id" | "kind" | "title" | "tldr" | "fields" | "tags" | "quality" | "body_md"
>;

export function KnowledgeCardEditor({
  card, mode, taskId, onClose, onSaved,
}: {
  card: EditableCard;
  mode: "repo" | "task";
  taskId?: string;
  onClose: () => void;
  onSaved: (card: RepoCard | TaskKnowledge) => void;
}) {
  const [title, setTitle] = useState(card.title);
  const [tldr, setTldr] = useState(card.tldr);
  const [fields, setFields] = useState<Record<string, unknown>>({ ...(card.fields || {}) });
  const [tags, setTags] = useState((card.tags || []).join(", "));
  const [authority, setAuthority] = useState<string>(
    "authority" in card ? (card.authority || "reference") : "reference");
  const [quality, setQuality] = useState<QualityReport | undefined>(card.quality);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kind = card.kind as KnowledgeKind;
  const allFields = useMemo(() => {
    const qualityFields = KNOWLEDGE_QUALITY_FIELDS.map((item) => ({
      key: item.key, label: item.label, hint: item.hint,
    }));
    const kindFields = (KNOWLEDGE_CARD_FIELDS[kind] || []).map((item) => ({
      key: item.key, label: item.label, hint: "",
    }));
    const seen = new Set(qualityFields.map((item) => item.key));
    return [...qualityFields, ...kindFields.filter((item) => !seen.has(item.key))];
  }, [kind]);

  useEffect(() => {
    let alive = true;
    const timer = window.setTimeout(() => {
      setChecking(true);
      api.validateKnowledgeCard({
        kind, title, tldr, fields, authority,
      }).then((result) => {
        if (alive && result.quality) setQuality(result.quality);
      }).catch(() => {}).finally(() => {
        if (alive) setChecking(false);
      });
    }, 350);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [kind, title, tldr, fields, authority]);

  const updateField = (key: string, value: string) => {
    setFields((prev) => ({ ...prev, [key]: value }));
  };

  const save = async (approve = false) => {
    setBusy(true);
    setError(null);
    try {
      const payload = {
        title, tldr, fields,
        tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
        ...(mode === "repo" ? { authority } : {}),
      };
      const updated = mode === "repo"
        ? await api.updateRepoCard(card.id, payload)
        : await api.updateTaskKnowledge(taskId || "", card.id, payload);
      onSaved(updated);
      if (approve) {
        if (mode === "repo") await api.approveRepoCard(card.id);
        else await api.approveTaskKnowledge(taskId || "", card.id);
        onSaved(updated);
      }
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal knowledge-editor" role="dialog" aria-modal="true"
        aria-label="编辑知识卡">
        <div className="modal-head">
          <h2>补全知识卡</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭">
            <Icon name="x" size={12} />
          </button>
        </div>
        <div className="knowledge-editor-body">
          <div className="knowledge-editor-quality">
            <QualityBadge quality={quality} />
            {quality && quality.blocking.length > 0 && (
              <span className="review-reason">
                阻止批准：{quality.blocking.map(qualityFieldLabel).join("、")}
              </span>
            )}
            {checking && <span className="muted">检查中…</span>}
          </div>
          {quality && quality.warnings.length > 0 && (
            <ul className="knowledge-quality-warnings">
              {quality.warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          )}
          <label>标题
            <input value={title} maxLength={80}
              onChange={(e) => setTitle(e.target.value)} />
          </label>
          <label>TLDR
            <textarea rows={2} value={tldr} maxLength={200}
              onChange={(e) => setTldr(e.target.value)} />
          </label>
          <div className="knowledge-editor-fields">
            {allFields.map((field) => (
              <label key={field.key} title={field.hint || undefined}>
                {field.label}
                {field.key === "effect_type" ? (
                  <select value={String(fields[field.key] || "")}
                    onChange={(e) => updateField(field.key, e.target.value)}>
                    <option value="">（可选）</option>
                    {["avoid_error", "reduce_rework", "speed", "compliance",
                      "quality", "other"].map((value) => (
                      <option key={value} value={value}>{value}</option>
                    ))}
                  </select>
                ) : (
                  <textarea rows={2} value={String(fields[field.key] ?? "")}
                    onChange={(e) => updateField(field.key, e.target.value)} />
                )}
              </label>
            ))}
          </div>
          <label>标签（逗号分隔）
            <input value={tags} onChange={(e) => setTags(e.target.value)} />
          </label>
          {mode === "repo" && (
            <label>约束力
              <select value={authority} onChange={(e) => setAuthority(e.target.value)}>
                <option value="reference">参考</option>
                <option value="normative">约束（必须遵守）</option>
              </select>
            </label>
          )}
          {card.body_md && (
            <details className="knowledge-record-details">
              <summary>当前正文预览</summary>
              <pre>{card.body_md}</pre>
            </details>
          )}
          {error && <p className="error">{error}</p>}
        </div>
        <div className="modal-actions">
          <button type="button" className="primary" disabled={busy}
            onClick={() => { void save(false); }}>保存</button>
          {quality && quality.blocking.length === 0 && (
            <button type="button" className="primary" disabled={busy}
              onClick={() => { void save(true); }}>
              保存并批准
            </button>
          )}
          <button type="button" className="small" onClick={onClose} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
