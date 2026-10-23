import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";

export type PanelLayoutOptions = {
  storageKey: string;
  min?: number;
  max?: number;
};

export type PanelLayout = {
  width: number;
  collapsed: boolean;
  dragging: boolean;
  toggle: () => void;
  reset: () => void;
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
  style: CSSProperties;
};

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value));

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // localStorage 被禁用时静默降级为当前会话状态
  }
}

function defaultWidth(min: number, max: number): number {
  if (typeof window === "undefined") return clamp(360, min, max);
  return clamp(Math.round(window.innerWidth * 0.2), min, max);
}

export function usePanelLayout({
  storageKey,
  min = 300,
  max = 560,
}: PanelLayoutOptions): PanelLayout {
  const widthKey = `anvil.panel.${storageKey}.width`;
  const collapsedKey = `anvil.panel.${storageKey}.collapsed`;

  const [width, setWidth] = useState(() => {
    const parsed = Number(readStored(widthKey));
    const initial = Number.isFinite(parsed) && parsed > 0
      ? parsed
      : defaultWidth(min, max);
    return clamp(initial, min, max);
  });
  const [collapsed, setCollapsed] = useState(
    () => readStored(collapsedKey) === "1",
  );
  const [dragging, setDragging] = useState(false);

  const widthRef = useRef(width);
  const dragRef = useRef<{ x: number; width: number } | null>(null);

  useEffect(() => {
    widthRef.current = width;
  }, [width]);

  const applyWidth = useCallback((next: number) => {
    const value = clamp(next, min, max);
    setWidth(value);
    widthRef.current = value;
    writeStored(widthKey, String(value));
  }, [max, min, widthKey]);

  const toggle = useCallback(() => {
    setCollapsed((previous) => {
      const next = !previous;
      writeStored(collapsedKey, next ? "1" : "0");
      return next;
    });
  }, [collapsedKey]);

  const reset = useCallback(() => {
    applyWidth(defaultWidth(min, max));
  }, [applyWidth, max, min]);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (collapsed || window.innerWidth < 1100) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      dragRef.current = { x: event.clientX, width: widthRef.current };
      setDragging(true);
    },
    [collapsed],
  );

  useEffect(() => {
    if (!dragging) return;

    const onMove = (event: PointerEvent) => {
      const start = dragRef.current;
      if (!start) return;
      const next = clamp(
        Math.round(start.width + start.x - event.clientX),
        min,
        max,
      );
      setWidth(next);
      widthRef.current = next;
    };
    const onUp = () => {
      dragRef.current = null;
      setDragging(false);
      writeStored(widthKey, String(widthRef.current));
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [dragging, max, min, widthKey]);

  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const delta = event.key === "ArrowLeft" ? 20 : -20;
      applyWidth(widthRef.current + delta);
    },
    [applyWidth],
  );

  return {
    width,
    collapsed,
    dragging,
    toggle,
    reset,
    onPointerDown,
    onKeyDown,
    style: { "--panel-w": `${width}px` } as CSSProperties,
  };
}
