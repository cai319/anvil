import { useEffect, useRef, useState } from "react";
import { AVATAR_PRESETS, initialFor, resolveAvatar } from "./avatarPresets";
import { Icon } from "./Icon";

type Props = {
  mode: "inline" | "modal";
  slug: string;
  name: string;
  value: string | null;
  onChange: (value: string | null) => void | Promise<void>;
  onClose?: () => void;
};

export function AvatarPicker({
  mode,
  slug,
  name,
  value,
  onChange,
  onClose,
}: Props) {
  const [draft, setDraft] = useState<string | null>(value);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (mode !== "modal") return;
    closeRef.current?.focus();
  }, [mode]);

  useEffect(() => {
    if (mode !== "modal") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, onClose, saving]);

  const resolved = resolveAvatar(draft, slug);
  const currentPreset = resolved.kind === "image" ? resolved.presetId : null;

  const pick = (next: string) => {
    if (saving) return;
    if (mode === "inline") {
      void onChange(next);
    } else {
      setDraft(next);
    }
  };

  const dismiss = () => {
    if (!saving) onClose?.();
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await onChange(draft);
      onClose?.();
    } catch {
      setSaveError("保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  const grid = (
    <div className="avatar-grid">
      {AVATAR_PRESETS.map((id) => {
        const selected = currentPreset === id;
        return (
          <button
            key={id}
            type="button"
            className={`avatar-tile ${selected ? "selected" : ""}`}
            onClick={() => pick(`big-smile:${id}`)}
            aria-pressed={selected}
            aria-label={`头像 ${id}`}
            title={`头像 ${id}`}
          >
            <img src={`/avatars/big-smile/${id}.svg`} alt="" />
          </button>
        );
      })}
      <button
        type="button"
        className={`avatar-tile avatar-tile-initials ${draft === "initials" ? "selected" : ""}`}
        onClick={() => pick("initials")}
        aria-pressed={draft === "initials"}
        aria-label="使用首字母头像"
        title="使用首字母"
      >
        <span>{initialFor(name, slug)}</span>
      </button>
    </div>
  );

  if (mode === "inline") {
    return (
      <div className="avatar-picker-inline">
        <div className="avatar-picker-head">
          <span className="avatar-picker-label">头像</span>
          <small>默认按 slug 自动匹配，点击可更换</small>
        </div>
        {grid}
      </div>
    );
  }

  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => {
        if (event.button === 0 && event.target === event.currentTarget) dismiss();
      }}
    >
      <div
        className="modal avatar-modal"
        role="dialog"
        aria-modal="true"
        aria-label="选择角色头像"
      >
        <div className="modal-head">
          <h2>选择角色头像</h2>
          <button
            ref={closeRef}
            type="button"
            className="modal-close"
            onClick={dismiss}
            aria-label="关闭"
          >
            <Icon name="x" size={12} />
          </button>
        </div>
        <p className="avatar-picker-hint">
          给「{name}」选一个头像。创建后也可以随时更换。
        </p>
        {grid}
        {saveError && <p className="error">{saveError}</p>}
        <div className="modal-actions">
          <button type="button" onClick={dismiss} disabled={saving}>取消</button>
          <button type="button" className="primary" onClick={save} disabled={saving}>
            {saving ? "保存中…" : "保存头像"}
          </button>
        </div>
      </div>
    </div>
  );
}
