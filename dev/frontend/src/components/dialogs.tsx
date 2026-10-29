import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

/**
 * In-app confirmation / prompt dialogs.
 *
 * HITL decisions must not use browser confirm()/prompt() windows (owner rule):
 * approvals, deletions and resets stay inside the app UI.
 */

export interface DialogOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  /** When set, renders an input/textarea and resolves with its value. */
  input?: {
    label?: string;
    placeholder?: string;
    initial?: string;
    multiline?: boolean;
  };
  /** Reject confirm while the input is empty (prompt dialogs only). */
  inputRequired?: boolean;
}

type Request =
  | { kind: "confirm"; opts: DialogOptions; resolve: (value: boolean) => void }
  | { kind: "prompt"; opts: DialogOptions; resolve: (value: string | null) => void };

let pushRequest: ((req: Request) => void) | null = null;

function enqueue(req: Request): Promise<boolean | string | null> {
  return new Promise((resolve) => {
    req.resolve = resolve as never;
    pushRequest?.(req);
  });
}

export function confirmDialog(opts: DialogOptions): Promise<boolean> {
  return enqueue({ kind: "confirm", opts, resolve: () => {} }) as Promise<boolean>;
}

export function promptDialog(opts: DialogOptions): Promise<string | null> {
  return enqueue({ kind: "prompt", opts, resolve: () => {} }) as Promise<string | null>;
}

export function DialogHost() {
  const [queue, setQueue] = useState<Request[]>([]);
  const [value, setValue] = useState("");
  const req = queue[0] ?? null;
  const valueRef = useRef("");
  const doneRef = useRef(false);

  useEffect(() => {
    pushRequest = (next) => setQueue((prev) => [...prev, next]);
    return () => { pushRequest = null; };
  }, []);

  useEffect(() => {
    const initial = req?.kind === "prompt" ? (req.opts.input?.initial ?? "") : "";
    valueRef.current = initial;
    doneRef.current = false;
    setValue(initial);
  }, [req]);

  useEffect(() => {
    if (!req) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finish(req.kind === "prompt" ? null : false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [req]);

  if (!req) return null;

  const opts = req.opts;
  const isPrompt = req.kind === "prompt";
  const disabled = Boolean(isPrompt && opts.inputRequired && !value.trim());

  const finish = (result: boolean | string | null) => {
    if (doneRef.current || !queue.length) return;
    doneRef.current = true;
    req.resolve(result as never);
    setQueue((prev) => prev.slice(1));
  };

  return createPortal(
    <div className="modal-overlay"
      onMouseDown={(e) => e.target === e.currentTarget
        && finish(req.kind === "prompt" ? null : false)}>
      <div className="modal confirm-dialog" role="dialog" aria-modal="true"
        aria-label={opts.title}>
        <div className="modal-head">
          <h2>{opts.title}</h2>
          <button className="modal-close" aria-label="关闭"
            onClick={() => finish(req.kind === "prompt" ? null : false)}>
            <Icon name="x" size={14} />
          </button>
        </div>
        {opts.message && <p className="confirm-message">{opts.message}</p>}
        {isPrompt && (
          <label className="field">
            {opts.input?.label && <span>{opts.input.label}</span>}
            {opts.input?.multiline ? (
              <textarea autoFocus value={value} placeholder={opts.input?.placeholder}
                onChange={(e) => { valueRef.current = e.target.value; setValue(e.target.value); }} />
            ) : (
              <input autoFocus value={value} placeholder={opts.input?.placeholder}
                onChange={(e) => { valueRef.current = e.target.value; setValue(e.target.value); }} />
            )}
          </label>
        )}
        <div className="modal-actions">
          <button className="small"
            onClick={() => finish(req.kind === "prompt" ? null : false)}>
            {opts.cancelLabel ?? "取消"}
          </button>
          <button className={opts.danger ? "danger" : "primary"} disabled={disabled}
            onClick={() => finish(isPrompt ? valueRef.current : true)}>
            {opts.confirmLabel ?? (isPrompt ? "确定" : "确认")}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
