import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** 字段标题旁的「?」提示控件：悬浮即显示，不占正文版面。 */
export function Hint({ text }: { text: string }) {
  const id = useId();
  const anchorRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ left: 8, top: 0, above: false });

  const show = () => {
    const rect = anchorRef.current?.getBoundingClientRect();
    if (!rect) return;
    const maxWidth = Math.min(340, window.innerWidth - 16);
    const left = Math.min(
      Math.max(8, rect.left),
      Math.max(8, window.innerWidth - maxWidth - 8),
    );
    const above = rect.bottom + 150 > window.innerHeight;
    setPos({ left, top: above ? rect.top - 8 : rect.bottom + 8, above });
    setOpen(true);
  };
  const hide = () => setOpen(false);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => setOpen(false);
    const onResize = () => setOpen(false);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        className="hint-control"
        aria-label={text}
        aria-describedby={open ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onKeyDown={(e) => { if (e.key === "Escape") hide(); }}
        onClick={(e) => {
          // 避免误触外层 label（尤其是 checkbox label）的默认激活行为。
          e.preventDefault();
          e.stopPropagation();
        }}
      >
        ?
      </button>
      {open && createPortal(
        <span
          id={id}
          role="tooltip"
          className={`hint-pop${pos.above ? " is-above" : ""}`}
          style={{ left: pos.left, top: pos.top }}
        >
          {text}
        </span>,
        document.body,
      )}
    </>
  );
}
