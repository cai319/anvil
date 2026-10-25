import { useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { Icon } from "./Icon";

/**
 * Two-step confirmation for resetting the global token counter.
 * The reset only moves the display baseline; token_usage history is kept.
 */
export function TokenResetDialog({
  currentTotal,
  onClose,
  onConfirmed,
}: {
  currentTotal: number | null;
  onClose: () => void;
  onConfirmed: () => void;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.resetTokens();
      onConfirmed();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return createPortal(
    <div className="modal-overlay"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal token-reset-modal" role="dialog" aria-modal="true"
        aria-label="清零 Token 计数">
        <div className="modal-head">
          <h2>{step === 1 ? "清零 Token 计数？" : "再次确认"}</h2>
          <button className="modal-close" onClick={onClose} aria-label="关闭">
            <Icon name="x" size={14} />
          </button>
        </div>

        {error && <p className="error">{error}</p>}

        {step === 1 ? (
          <>
            <p>
              当前右上角累计：
              <strong>{currentTotal == null ? "—" : currentTotal.toLocaleString("en-US")}</strong>
            </p>
            <p className="muted">
              清零只把“累计 Token”显示基线移到当前时刻，不会删除 token_usage 历史；
              任务 Token、执行 Token、性能统计和每日预算闸门不受影响。
            </p>
            <p className="muted">这是一个两步确认操作。</p>
            <div className="modal-actions">
              <button className="small" onClick={onClose}>取消</button>
              <button className="primary" onClick={() => setStep(2)}>继续</button>
            </div>
          </>
        ) : (
          <>
            <p>
              再次确认：清零后右上角计数从 0 开始，历史用量仍保留在数据库中。
            </p>
            <p className="muted">本次操作会写入审计日志（tokens.reset）。</p>
            <div className="modal-actions">
              <button className="small" disabled={busy} onClick={() => setStep(1)}>
                返回
              </button>
              <button className="danger" disabled={busy} onClick={confirm}>
                {busy ? "清零中…" : "确认清零"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
