# Anvil 全宽响应式 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Anvil 前端从“顶部导航 + 居中限宽”改为 raft 式全宽应用壳：左侧分组导航、全宽内容区、可拖拽/折叠右栏，并在 1280–3840 CSS px 与 900 以下窄屏都可用。

**Architecture:** 新增 `usePanelLayout` 管理右栏宽度/折叠/localStorage；`App.tsx` 改为固定全高 app shell（侧栏 + 工具条 + 滚动内容区）；`style.css` 用 CSS 变量与少量断点统一尺度；Lobby/TaskDetail 接入面板，表格页加横向滚动容器，列表页宽屏多列。

**Tech Stack:** React 18、TypeScript、Vite 5、纯 CSS（不新增依赖、不引入外部字体/图标库）。

**Design spec:** `docs/superpowers/specs/2026-10-23-ui-fullwidth-responsive-design.md`

**Verification note:** 仓库没有前端单元测试框架。每个任务的门禁是 `npm run build`（`tsc && vite build`）；布局行为用浏览器响应式模式手工验收，验收清单见 Task 9。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `dev/frontend/src/usePanelLayout.ts` | 右栏宽度、折叠、拖拽、键盘调整、localStorage 持久化 |
| `dev/frontend/src/App.tsx` | App shell：侧栏、工具条、内容区、抽屉、路由页面切换 |
| `dev/frontend/src/style.css` | 设计变量、shell、响应式断点、面板、表格/列表宽度 |
| `dev/frontend/src/pages/Lobby.tsx` | 看板 + 右栏面板布局 |
| `dev/frontend/src/pages/TaskDetail.tsx` | 会话主列 + 任务信息面板、全高线程、sticky 输入框 |
| `dev/frontend/src/pages/Roles.tsx` | 角色列表/详情宽度适配（仅 CSS） |
| `dev/frontend/src/pages/Runtimes.tsx` | 表格包 `.table-scroll` |
| `dev/frontend/src/pages/Governance.tsx` | 两个表格包 `.table-scroll` |
| `dev/frontend/src/pages/ActivityHistory.tsx` | 历史表格包 `.table-scroll` |
| `dev/frontend/src/pages/Knowledge.tsx` / `Reminders.tsx` / `Tooling.tsx` / `Channels.tsx` / `Decompose.tsx` | 依赖 `.drafts`/`.toolbar` 的通用宽屏规则，无需 TSX 改动 |

---

### Task 0: 创建 UI 分支

**Files:** 无

- [ ] **Step 1: 从 checkpoint 分支创建 UI 分支**

Run:
```bash
cd /Users/ikaruga/cctest/aliceAgent
git switch -c feat/responsive-ui
git status --short
```
Expected: `Switched to a new branch 'feat/responsive-ui'`；只剩 `docs/superpowers/specs/2026-10-23-ui-fullwidth-*.md/html` 未跟踪。

---

### Task 1: usePanelLayout hook

**Files:**
- Create: `dev/frontend/src/usePanelLayout.ts`

- [ ] **Step 1: 写入 hook**

```ts
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
```

- [ ] **Step 2: 类型检查**

Run:
```bash
cd dev/frontend && npm run build
```
Expected: `tsc` 与 `vite build` 通过。

- [ ] **Step 3: 提交**

```bash
git add dev/frontend/src/usePanelLayout.ts
git commit -m "feat(ui): add persisted resizable panel hook" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 全局 shell 与响应式 CSS

**Files:**
- Modify: `dev/frontend/src/style.css`

- [ ] **Step 1: 替换 `:root`**

把文件顶部的 `:root { ... }` 整块替换为：

```css
:root {
  color-scheme: dark;
  font-family: "Inter", system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  --bg: #0b0e15;
  --bg-soft: #10141f;
  --surface: #151a28;
  --surface-2: #1b2133;
  --border: #232a3d;
  --border-strong: #2e3850;
  --text: #e7ebf4;
  --muted: #8b94a9;
  --faint: #5c6579;
  --accent: #6d7cff;
  --accent-strong: #8a96ff;
  --green: #34d399;
  --amber: #fbbf24;
  --blue: #60a5fa;
  --red: #f87171;
  --radius: 12px;
  --sidebar-w: clamp(200px, 12vw, 248px);
  --sidebar-rail-w: 68px;
  --panel-w: clamp(320px, 20vw, 440px);
  --content-pad: clamp(14px, 1.15vw, 30px);
  --fs-base: clamp(14px, 0.3vw + 9.5px, 16px);
  --fs-h1: clamp(1.35rem, 0.25vw + 1.1rem, 1.8rem);
  --header-h: 56px;
}
```

- [ ] **Step 2: 删除旧顶栏布局**

删除旧顶栏相关规则：`.topbar { ... }`、`.nav { ... }`、`.nav a { ... }`、`.nav a:hover { ... }`、`.topbar .spacer`、`.topbar .chip`。**保留 `.brand` 与 `.brand .dot`** 供新侧栏使用。

- [ ] **Step 3: 解除旧限宽**

把 `.page { max-width: 1500px; margin: 0 auto; padding: 1.25rem; }` 改为：

```css
.page { max-width: none; margin: 0; padding: 0; }
```

把 `.session { max-width: 1100px; }` 改为：

```css
.session { max-width: none; }
```

删除两处旧 `.lobby` 规则（文件前部的三栏规则与后部的两栏覆盖规则），由 Step 5 的新规则接管。

- [ ] **Step 4: 删除旧 `.task-layout` 规则**

删除：

```css
.task-layout { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 1rem; align-items: start; }
@media (max-width: 900px) { .task-layout { grid-template-columns: 1fr; } }
```

- [ ] **Step 5: 在 `style.css` 末尾追加新布局**

```css
/* ══════════ App shell / full-width responsive (2026-10-23) ══════════ */

body {
  font-size: var(--fs-base);
  overflow: hidden;
}

.app-shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  height: 100vh;
  height: 100dvh;
}
.app-shell.sidebar-collapsed {
  grid-template-columns: var(--sidebar-rail-w) minmax(0, 1fr);
}

.sidebar {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 12px;
  background: var(--bg-soft);
  border-right: 1px solid var(--border);
}
.sidebar .brand {
  margin-right: 0;
  padding: 4px 8px;
  font-size: 15px;
}
.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.nav-group { display: flex; flex-direction: column; gap: 3px; }
.nav-label {
  padding: 0 9px 4px;
  color: var(--faint);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 38px;
  padding: 0 10px;
  border-radius: 9px;
  color: var(--muted);
  white-space: nowrap;
}
.nav-item:hover {
  background: var(--surface);
  color: var(--text);
  text-decoration: none;
}
.nav-item.active {
  background: var(--surface-2);
  color: var(--text);
}
.nav-item.active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 3px;
  background: var(--accent);
}
.nav-icon {
  flex: none;
  width: 22px;
  text-align: center;
  color: var(--accent-strong);
}
.nav-text { min-width: 0; }
.nav-badge {
  margin-left: auto;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(96, 165, 250, 0.16);
  color: var(--blue);
  font-size: 11px;
  font-weight: 700;
}
.sidebar-foot {
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.rail-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.rail-btn:hover { background: var(--surface); color: var(--text); }

.app-shell.sidebar-collapsed .nav-text,
.app-shell.sidebar-collapsed .nav-label,
.app-shell.sidebar-collapsed .nav-badge,
.app-shell.sidebar-collapsed .brand-name {
  display: none;
}
.app-shell.sidebar-collapsed .nav-item {
  justify-content: center;
  padding: 0;
}
.app-shell.sidebar-collapsed .sidebar .brand {
  justify-content: center;
  padding: 0;
}
.app-shell.sidebar-collapsed .rail-btn { justify-content: center; }
.app-shell.sidebar-collapsed .rail-btn::after {
  content: "→";
  font-size: 13px;
}

.app-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.app-header {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--header-h);
  padding: 0 var(--content-pad);
  border-bottom: 1px solid var(--border);
  background: rgba(11, 14, 21, 0.86);
  backdrop-filter: blur(12px);
}
.page-crumb {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-weight: 650;
}
.page-crumb .muted { font-size: 12px; font-weight: 400; }
.header-spacer { flex: 1; }
.hamburger {
  display: none;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.32rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--content-pad);
}
.content h1 {
  margin: 0 0 14px;
  font-size: var(--fs-h1);
  font-weight: 650;
}
.content h2 { font-size: 1.05rem; }

.drawer-mask {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 50;
  padding: 0;
  border: 0;
  background: rgba(0, 0, 0, 0.5);
  cursor: default;
}

/* Lobby：主区 + 6px 分隔条 + 右栏 */
.lobby {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 6px var(--panel-w);
  gap: 0;
  align-items: start;
}
.lobby.panel-collapsed { grid-template-columns: minmax(0, 1fr); }
.lobby.panel-collapsed > .splitter,
.lobby.panel-collapsed > .lobby-side { display: none; }
.lobby-main { min-width: 0; }
.lobby-side {
  min-width: 0;
  max-height: calc(100dvh - var(--header-h) - (var(--content-pad) * 2));
  overflow-y: auto;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  position: sticky;
  top: 0;
}

/* TaskDetail：主会话 + 6px 分隔条 + 任务信息 */
.task-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 6px var(--panel-w);
  gap: 0;
  align-items: start;
}
.task-layout.panel-collapsed { grid-template-columns: minmax(0, 1fr); }
.task-layout.panel-collapsed > .splitter,
.task-layout.panel-collapsed > .task-side { display: none; }
.task-main { min-width: 0; }
.task-side {
  position: sticky;
  top: 0;
  max-height: calc(100dvh - var(--header-h) - (var(--content-pad) * 2));
  overflow-y: auto;
}

.splitter {
  position: relative;
  cursor: col-resize;
  touch-action: none;
}
.splitter::before {
  content: "";
  position: absolute;
  left: 2px;
  top: 0;
  bottom: 0;
  width: 2px;
  border-radius: 2px;
  background: var(--border);
}
.splitter:hover::before,
.splitter.dragging::before {
  background: var(--accent);
  box-shadow: 0 0 8px var(--accent);
}
.splitter:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.pane-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 0.6rem;
}
.pane-head .pane-title { flex: 1; margin: 0; }
.icon-btn {
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-2);
  color: var(--muted);
  cursor: pointer;
}
.icon-btn:hover { color: var(--text); }

/* 会话与输入框 */
.session-thread {
  max-height: calc(100dvh - var(--header-h) - (var(--content-pad) * 2) - 210px);
  min-height: 320px;
}
.session-compose {
  position: sticky;
  bottom: 0;
  z-index: 2;
  background: var(--bg);
}
.thread-body { max-width: 1600px; }

/* 角色工坊 */
.roles-layout {
  grid-template-columns: clamp(240px, 18vw, 320px) minmax(0, 1fr);
}
.roles-detail { min-width: 0; }
dl { grid-template-columns: minmax(120px, 180px) minmax(0, 1fr); }

/* 表格页 */
.table-scroll { width: 100%; overflow-x: auto; }
.table-scroll .board { min-width: 720px; }
.table-scroll .history-table { min-width: 760px; }
.board thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-soft);
}

/* 宽屏卡片网格与表单 */
.toolbar input,
.toolbar select { flex: 1 1 260px; max-width: 720px; }
@media (min-width: 1500px) {
  .drafts {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
    gap: 12px;
  }
  .drafts > li { margin-bottom: 0; }
}

/* 响应式断点 */
@media (max-width: 1199px) {
  .app-shell,
  .app-shell.sidebar-collapsed {
    grid-template-columns: var(--sidebar-rail-w) minmax(0, 1fr);
  }
  .sidebar .nav-text,
  .sidebar .nav-label,
  .sidebar .nav-badge,
  .sidebar .brand-name { display: none; }
  .sidebar .nav-item { justify-content: center; padding: 0; }
  .sidebar .brand { justify-content: center; padding: 0; }
  .sidebar .rail-btn { justify-content: center; }
  .sidebar .rail-btn::after { content: "→"; font-size: 13px; }
}
@media (max-width: 1099px) {
  .lobby,
  .lobby.panel-collapsed,
  .task-layout,
  .task-layout.panel-collapsed { grid-template-columns: minmax(0, 1fr); }
  .lobby > .splitter,
  .task-layout > .splitter { display: none; }
  .lobby-side,
  .task-side {
    position: static;
    max-height: none;
    margin-top: 12px;
  }
  .kanban-col { min-width: 230px; }
}
@media (max-width: 899px) {
  .app-shell,
  .app-shell.sidebar-collapsed { grid-template-columns: minmax(0, 1fr); }
  :root { --header-h: 52px; }
  .sidebar {
    position: fixed;
    z-index: 60;
    inset: 0 auto 0 0;
    width: 248px;
    transform: translateX(-105%);
    transition: transform 0.18s ease;
    box-shadow: 20px 0 50px rgba(0, 0, 0, 0.45);
  }
  .app-shell.drawer-open .sidebar { transform: none; }
  .hamburger { display: inline-flex; }
  .app-shell.drawer-open .drawer-mask { display: block; }
  .app-shell .nav-text,
  .app-shell .nav-label,
  .app-shell .nav-badge,
  .app-shell .brand-name { display: initial !important; }
  .app-shell .nav-item {
    justify-content: flex-start !important;
    padding: 0 10px !important;
  }
  .app-shell .sidebar .brand { justify-content: flex-start !important; }
  .app-shell .rail-btn .nav-text { display: inline !important; }
  .app-shell .rail-btn::after { content: none; }
  .session-thread { max-height: none; }
  .roles-layout { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar,
  .splitter { transition: none !important; }
}
```

- [ ] **Step 6: 构建**

Run:
```bash
cd dev/frontend && npm run build
```
Expected: PASS。此时 App.tsx 仍是旧顶栏，但新 CSS 不影响构建。

- [ ] **Step 7: 提交**

```bash
git add dev/frontend/src/style.css
git commit -m "feat(ui): add full-width app shell and responsive scale" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: App.tsx 应用壳

**Files:**
- Modify: `dev/frontend/src/App.tsx`（整文件替换）

- [ ] **Step 1: 替换 App.tsx**

```tsx
import { useEffect, useRef, useState } from "react";
import { api, subscribeEvents } from "./api/client";
import { installLinkInterceptor, setRouteListener } from "./router";
import { ActivityHistory } from "./pages/ActivityHistory";
import { AgentSessions } from "./pages/AgentSessions";
import { Channels } from "./pages/Channels";
import { Decompose } from "./pages/Decompose";
import { Governance } from "./pages/Governance";
import { Inbox } from "./pages/Inbox";
import { Knowledge } from "./pages/Knowledge";
import { Lobby } from "./pages/Lobby";
import { Reminders } from "./pages/Reminders";
import { Roles } from "./pages/Roles";
import { Runtimes } from "./pages/Runtimes";
import { RunSession } from "./pages/RunSession";
import { TaskDetail } from "./pages/TaskDetail";
import { Tooling } from "./pages/Tooling";

type Route =
  | { page: "lobby" }
  | { page: "inbox" }
  | { page: "activity" }
  | { page: "decompose" }
  | { page: "channels" }
  | { page: "roles" }
  | { page: "runtimes" }
  | { page: "governance" }
  | { page: "reminders" }
  | { page: "knowledge" }
  | { page: "tooling" }
  | { page: "task"; id: string }
  | { page: "agent"; slug: string }
  | { page: "run"; id: string };

const NAV_GROUPS = [
  {
    label: "工作台",
    items: [
      { href: "/inbox", label: "收件箱", icon: "✉" },
      { href: "/", label: "任务大厅", icon: "▦" },
      { href: "/decompose", label: "目标拆解", icon: "⑂" },
      { href: "/channels", label: "频道", icon: "#" },
    ],
  },
  {
    label: "角色与知识",
    items: [
      { href: "/roles", label: "角色工坊", icon: "◉" },
      { href: "/runtimes", label: "运行时", icon: "⚙" },
      { href: "/knowledge", label: "知识库", icon: "◇" },
      { href: "/reminders", label: "提醒", icon: "◷" },
    ],
  },
  {
    label: "系统",
    items: [
      { href: "/tooling", label: "工具", icon: "⌘" },
      { href: "/governance", label: "治理", icon: "◈" },
    ],
  },
] as const;

const PAGE_TITLE: Record<Route["page"], string> = {
  lobby: "任务大厅",
  inbox: "收件箱",
  activity: "活动历史",
  decompose: "目标拆解",
  channels: "频道",
  roles: "角色工坊",
  runtimes: "运行时",
  governance: "治理",
  reminders: "提醒",
  knowledge: "知识库",
  tooling: "工具",
  task: "任务详情",
  agent: "角色会话",
  run: "运行详情",
};

const SIDEBAR_KEY = "anvil.sidebar.collapsed";

function initialSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false;
  }
}

function parseRoute(path: string): Route {
  if (path.startsWith("/inbox")) return { page: "inbox" };
  if (path.startsWith("/activity")) return { page: "activity" };
  if (path.startsWith("/decompose")) return { page: "decompose" };
  if (path.startsWith("/channels")) return { page: "channels" };
  if (path.startsWith("/roles")) return { page: "roles" };
  if (path.startsWith("/runtimes")) return { page: "runtimes" };
  if (path.startsWith("/governance")) return { page: "governance" };
  if (path.startsWith("/reminders")) return { page: "reminders" };
  if (path.startsWith("/knowledge")) return { page: "knowledge" };
  if (path.startsWith("/tooling")) return { page: "tooling" };
  const m = path.match(/^\/tasks\/([^/]+)$/);
  if (m) return { page: "task", id: decodeURIComponent(m[1]) };
  const a = path.match(/^\/agents\/([^/]+)$/);
  if (a) return { page: "agent", slug: decodeURIComponent(a[1]) };
  const r = path.match(/^\/runs\/([^/]+)$/);
  if (r) return { page: "run", id: decodeURIComponent(r[1]) };
  return { page: "lobby" };
}

function activeHref(route: Route): string {
  if (route.page === "task" || route.page === "run") return "/";
  if (route.page === "agent") return "/roles";
  return route.page === "lobby" ? "/" : `/${route.page}`;
}

function useRoute(): { route: Route; navKey: number } {
  const [loc, setLoc] = useState({ path: window.location.pathname, key: 0 });

  useEffect(() => {
    setRouteListener((path: string) => setLoc((l) => ({ path, key: l.key + 1 })));
    const onPop = () => setLoc((l) => ({ path: window.location.pathname, key: l.key + 1 }));
    window.addEventListener("popstate", onPop);
    const uninstall = installLinkInterceptor();
    return () => {
      setRouteListener(null);
      window.removeEventListener("popstate", onPop);
      uninstall();
    };
  }, []);

  return { route: parseRoute(loc.path), navKey: loc.key };
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function TopStatus() {
  const [frozen, setFrozen] = useState(false);
  const [tokens, setTokens] = useState<{ total: number; input: number; output: number } | null>(null);

  const refresh = () => {
    api.getFreeze().then((f) => setFrozen(f.frozen)).catch(() => {});
    api.getTokens().then((t) => setTokens({
      total: t.total_tokens, input: t.input_tokens, output: t.output_tokens,
    })).catch(() => {});
  };

  useEffect(() => {
    refresh();
    return subscribeEvents(() => refresh());
  }, []);

  const toggle = async () => {
    try {
      const r = frozen ? await api.resumeAll() : await api.freezeAll();
      setFrozen(r.frozen);
    } catch {
      // ignore
    }
  };

  return (
    <>
      <button
        onClick={toggle}
        className={frozen ? "danger small" : "small"}
        title={frozen ? "解除全场冻结" : "全场冻结"}
      >
        {frozen ? "已冻结 · 点击解除" : "全场冻结"}
      </button>
      <span className="chip" title="累计 Token 消耗（输入含 cache；来自 CLI result.usage）">
        Token {tokens == null ? "—" : fmtTokens(tokens.total)}
        {tokens && (
          <span className="muted">
            （入 {fmtTokens(tokens.input)} / 出 {fmtTokens(tokens.output)}）
          </span>
        )}
      </span>
    </>
  );
}

function InboxBadge() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    const refresh = () => api.getInbox().then((b) => setCount(b.count)).catch(() => {});
    refresh();
    return subscribeEvents(() => refresh());
  }, []);
  if (!count) return null;
  return <span className="nav-badge">{count}</span>;
}

export default function App() {
  const { route, navKey } = useRoute();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(initialSidebarCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed ? "1" : "0");
    } catch {
      // ignore
    }
  }, [sidebarCollapsed]);

  useEffect(() => {
    setDrawerOpen(false);
  }, [navKey]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setDrawerOpen(false);
      menuRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const active = activeHref(route);
  const pageKey = `${navKey}`;

  return (
    <div
      className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${drawerOpen ? "drawer-open" : ""}`}
    >
      <aside id="app-sidebar" className="sidebar" aria-label="主导航">
        <a href="/" className="brand" onClick={() => setDrawerOpen(false)}>
          <span className="dot" />
          <span className="brand-name">Anvil</span>
        </a>

        <nav className="sidebar-nav">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-label">{group.label}</div>
              {group.items.map((item) => (
                <a
                  key={item.href}
                  href={item.href}
                  className={`nav-item ${active === item.href ? "active" : ""}`}
                  title={item.label}
                  aria-current={active === item.href ? "page" : undefined}
                  onClick={() => setDrawerOpen(false)}
                >
                  <span className="nav-icon" aria-hidden="true">{item.icon}</span>
                  <span className="nav-text">{item.label}</span>
                  {item.href === "/inbox" && <InboxBadge />}
                </a>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <button
            type="button"
            className="rail-btn"
            onClick={() => setSidebarCollapsed((v) => !v)}
            aria-expanded={!sidebarCollapsed}
            aria-label={sidebarCollapsed ? "展开侧栏" : "收起侧栏"}
          >
            <span aria-hidden="true">{sidebarCollapsed ? "→" : "←"}</span>
            <span className="nav-text">{sidebarCollapsed ? "展开侧栏" : "收起侧栏"}</span>
          </button>
        </div>
      </aside>

      <div className="app-main">
        <header className="app-header">
          <button
            ref={menuRef}
            type="button"
            className="hamburger"
            onClick={() => setDrawerOpen(true)}
            aria-label="打开导航"
            aria-expanded={drawerOpen}
            aria-controls="app-sidebar"
          >
            ☰
          </button>
          <div className="page-crumb">
            <span>{PAGE_TITLE[route.page]}</span>
            <span className="muted">/ Anvil</span>
          </div>
          <span className="header-spacer" />
          <TopStatus />
        </header>

        <main className="content">
          {route.page === "inbox" ? (
            <Inbox key={`inbox-${pageKey}`} />
          ) : route.page === "activity" ? (
            <ActivityHistory key={`activity-${pageKey}`} />
          ) : route.page === "task" ? (
            <TaskDetail key={`task-${route.id}-${pageKey}`} id={route.id} />
          ) : route.page === "agent" ? (
            <AgentSessions key={`agent-${route.slug}-${pageKey}`} slug={route.slug} />
          ) : route.page === "run" ? (
            <RunSession key={`run-${route.id}-${pageKey}`} id={route.id} />
          ) : route.page === "decompose" ? (
            <Decompose key={`decompose-${pageKey}`} />
          ) : route.page === "channels" ? (
            <Channels key={`channels-${pageKey}`} />
          ) : route.page === "roles" ? (
            <Roles key={`roles-${pageKey}`} />
          ) : route.page === "runtimes" ? (
            <Runtimes key={`runtimes-${pageKey}`} />
          ) : route.page === "knowledge" ? (
            <Knowledge key={`knowledge-${pageKey}`} />
          ) : route.page === "reminders" ? (
            <Reminders key={`reminders-${pageKey}`} />
          ) : route.page === "tooling" ? (
            <Tooling key={`tooling-${pageKey}`} />
          ) : route.page === "governance" ? (
            <Governance key={`governance-${pageKey}`} />
          ) : (
            <Lobby key={`lobby-${pageKey}`} />
          )}
        </main>
      </div>

      <button
        type="button"
        className="drawer-mask"
        aria-label="关闭导航"
        onClick={() => setDrawerOpen(false)}
      />
    </div>
  );
}
```

- [ ] **Step 2: 构建**

Run:
```bash
cd dev/frontend && npm run build
```
Expected: PASS。

- [ ] **Step 3: 手工冒烟**

Run:
```bash
cd dev/frontend && npm run dev
```
打开 `http://127.0.0.1:5173/`。确认：左侧分组导航、右侧 Token/冻结、内容区滚动、无顶栏旧导航。停止 dev server。

- [ ] **Step 4: 提交**

```bash
git add dev/frontend/src/App.tsx
git commit -m "feat(ui): replace topbar with full-height sidebar shell" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: Lobby 接入可调宽右栏

**Files:**
- Modify: `dev/frontend/src/pages/Lobby.tsx`

- [ ] **Step 1: 引入 hook**

在文件顶部 import 区加入：

```tsx
import { usePanelLayout } from "../usePanelLayout";
```

在 `export function Lobby() {` 内、`const now = useNow(30_000);` 之前加入：

```tsx
const panel = usePanelLayout({ storageKey: "lobby" });
```

- [ ] **Step 2: 工具栏增加“显示右栏”按钮**

在 `<div className="toolbar">` 的 `</button>`（新建任务）之后加入：

```tsx
{panel.collapsed && (
  <button className="small" onClick={panel.toggle}>显示右栏</button>
)}
```

- [ ] **Step 3: 改造 lobby 容器与右栏**

把：

```tsx
<div className="lobby">
```

改为：

```tsx
<div
  className={`lobby ${panel.collapsed ? "panel-collapsed" : ""}`}
  style={panel.style}
>
```

把看板 `<section>` 改为：

```tsx
<section className="lobby-main">
```

在 看板 `</section>` 与 `{/* 右栏：实时会话 + 最近活动（历史见 /activity） */}` 之间插入：

```tsx
<div
  className={`splitter ${panel.dragging ? "dragging" : ""}`}
  role="separator"
  aria-orientation="vertical"
  aria-label="调整右栏宽度"
  aria-valuemin={300}
  aria-valuemax={560}
  aria-valuenow={panel.width}
  tabIndex={0}
  onPointerDown={panel.onPointerDown}
  onKeyDown={panel.onKeyDown}
  onDoubleClick={panel.reset}
/>
```

把右栏的 `<section>` 改为：

```tsx
<section className="lobby-side" aria-hidden={panel.collapsed || undefined}>
```

把：

```tsx
<h2 className="pane-title">实时会话 · {runs.length}</h2>
```

改为：

```tsx
<div className="pane-head">
  <h2 className="pane-title">实时会话 · {runs.length}</h2>
  <button
    type="button"
    className="icon-btn"
    onClick={panel.toggle}
    aria-label="折叠右栏"
    title="折叠右栏"
  >
    ⇥
  </button>
</div>
```

- [ ] **Step 4: 构建**

Run:
```bash
cd dev/frontend && npm run build
```
Expected: PASS。

- [ ] **Step 5: 手工验证**

`npm run dev`，打开大厅：
- 右栏可拖拽，宽度在 300–560 之间；
- 折叠按钮隐藏右栏，看板占满；刷新后状态保留；
- 窗口 <1100px 时右栏移到看板下方，分隔条消失。

停止 dev server。

- [ ] **Step 6: 提交**

```bash
git add dev/frontend/src/pages/Lobby.tsx
git commit -m "feat(ui): make lobby panel resizable and collapsible" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: TaskDetail 接入面板与全高会话

**Files:**
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`

- [ ] **Step 1: 引入 hook**

在 import 区加入：

```tsx
import { usePanelLayout } from "../usePanelLayout";
```

在 `const { slugs: liveSlugs, runtimes: roleRuntimes } = useRoleRuntimes(task);` 之后加入：

```tsx
const panel = usePanelLayout({ storageKey: "task" });
```

- [ ] **Step 2: 改造 tabs 与 task-layout**

在 `</div>`（`.detail-tabs` 结束）之前加入：

```tsx
{panel.collapsed && (
  <button
    type="button"
    className="small"
    style={{ marginLeft: "auto", alignSelf: "center" }}
    onClick={panel.toggle}
  >
    显示任务信息
  </button>
)}
```

把：

```tsx
      {tab === "chat" && (
      <div className="task-layout">
```

改为：

```tsx
      {tab === "chat" && (
      <div
        className={`task-layout ${panel.collapsed ? "panel-collapsed" : ""}`}
        style={panel.style}
      >
```

- [ ] **Step 3: 插入分隔条并改造右栏标题**

在 `{/* 右：任务信息面板 */}` 之后、`<aside className="task-side">` 之前插入：

```tsx
        <div
          className={`splitter ${panel.dragging ? "dragging" : ""}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整任务信息面板宽度"
          aria-valuemin={300}
          aria-valuemax={560}
          aria-valuenow={panel.width}
          tabIndex={0}
          onPointerDown={panel.onPointerDown}
          onKeyDown={panel.onKeyDown}
          onDoubleClick={panel.reset}
        />
```

把：

```tsx
        <aside className="task-side">
          <h2 className="pane-title">任务信息</h2>
```

改为：

```tsx
        <aside className="task-side">
          <div className="pane-head">
            <h2 className="pane-title">任务信息</h2>
            <button
              type="button"
              className="icon-btn"
              onClick={panel.toggle}
              aria-label="折叠任务信息"
              title="折叠任务信息"
            >
              ⇥
            </button>
          </div>
```

- [ ] **Step 4: 构建**

Run:
```bash
cd dev/frontend && npm run build
```
Expected: PASS。

- [ ] **Step 5: 手工验证**

`npm run dev`，打开任一任务详情：
- 主会话 + 任务信息面板吃满宽度；
- 右栏可拖拽/折叠；
- 会话区在 1440/2160 高度下用满剩余空间，内部滚动与“↓ 最新”仍工作；
- 输入框滚动时贴底可见；
- <1100px 时右栏移到会话下方。

停止 dev server。

- [ ] **Step 6: 提交**

```bash
git add dev/frontend/src/pages/TaskDetail.tsx
git commit -m "feat(ui): make task detail panel resizable and use full height" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 角色工坊与表格页宽度适配

**Files:**
- Modify: `dev/frontend/src/pages/Runtimes.tsx`
- Modify: `dev/frontend/src/pages/Governance.tsx`
- Modify: `dev/frontend/src/pages/ActivityHistory.tsx`

- [ ] **Step 1: 包裹 Runtimes 表格**

把 `<table className="board">` 与其 `</table>` 包在：

```tsx
      <div className="table-scroll">
        <table className="board">
          {/* 原有 thead/tbody 保持不变 */}
        </table>
      </div>
```

- [ ] **Step 2: 包裹 Governance 两个表格**

对 `Governance.tsx` 中两处 `<table className="board"> ... </table>` 做同样包裹：

```tsx
        <div className="table-scroll">
          <table className="board">
            {/* 原有内容保持不变 */}
          </table>
        </div>
```

- [ ] **Step 3: 包裹 ActivityHistory 表格**

把 `<table className="history-table"> ... </table>` 包在：

```tsx
      <div className="table-scroll">
        <table className="history-table">
          {/* 原有内容保持不变 */}
        </table>
      </div>
```

- [ ] **Step 4: 构建**

Run:
```bash
cd dev/frontend && npm run build
```
Expected: PASS。

- [ ] **Step 5: 手工验证**

窄屏（<900px）打开运行时/治理/活动历史：表格在容器内横向滚动，整页不横滚；宽屏表格吃满内容区。

- [ ] **Step 6: 提交**

```bash
git add dev/frontend/src/pages/Runtimes.tsx dev/frontend/src/pages/Governance.tsx dev/frontend/src/pages/ActivityHistory.tsx
git commit -m "feat(ui): make data tables full-width with local horizontal scroll" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 列表/卡片页宽屏多列

**Files:** 仅 CSS（`dev/frontend/src/style.css` 已在 Task 2 包含规则）；无需 TSX 修改。

- [ ] **Step 1: 确认通用规则存在**

检查 `style.css` 末尾包含：

```css
.toolbar input,
.toolbar select { flex: 1 1 260px; max-width: 720px; }

@media (min-width: 1500px) {
  .drafts {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
    gap: 12px;
  }
  .drafts > li { margin-bottom: 0; }
}
```

- [ ] **Step 2: 构建并手工检查**

Run:
```bash
cd dev/frontend && npm run build
```
在 1920/2560 宽度打开知识库、提醒、工具、频道、目标拆解：卡片在宽屏自动多列，工具栏输入框不会被挤成一条细线；<1500px 恢复单列。

- [ ] **Step 3: 无需单独提交**

该规则已随 Task 2 的 `style.css` 提交；若在本任务中调整了数值，执行：

```bash
git add dev/frontend/src/style.css
git commit -m "fix(ui): tune responsive card grid and toolbar widths" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 可访问性收尾

**Files:**
- Modify: `dev/frontend/src/App.tsx`
- Modify: `dev/frontend/src/pages/Lobby.tsx`
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`

- [ ] **Step 1: 检查键盘/ARIA**

确认以下行为已在 Task 3–5 的代码中成立：
- 侧栏折叠按钮有 `aria-expanded` 与 `aria-label`；
- 汉堡按钮有 `aria-expanded`、`aria-controls="app-sidebar"`；Esc 关闭并回焦；
- 导航项有 `aria-current="page"`；
- 两个分隔条有 `role="separator"`、`aria-orientation="vertical"`、`aria-valuemin/max/now`、`tabIndex=0`；
- 左右方向键可调整右栏宽度；
- 所有折叠/显示按钮有 `aria-label`/`title`。

若发现遗漏，按 Task 3–5 的代码补齐。

- [ ] **Step 2: 键盘手工验证**

`npm run dev`：
- Tab 到侧栏折叠按钮，Enter 切换；
- Tab 到分隔条，←/→ 调整宽度；
- <900px 打开汉堡，Esc 关闭，焦点回到汉堡。

- [ ] **Step 3: 构建**

```bash
cd dev/frontend && npm run build
```
Expected: PASS。

- [ ] **Step 4: 提交（如有修改）**

```bash
git add dev/frontend/src/App.tsx dev/frontend/src/pages/Lobby.tsx dev/frontend/src/pages/TaskDetail.tsx
git commit -m "fix(ui): complete keyboard and aria support for shell panels" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: 最终验收与文档

**Files:**
- Modify: `docs/superpowers/specs/2026-10-23-ui-fullwidth-responsive-design.md`（状态改为已实现）
- Add: `docs/superpowers/specs/2026-10-23-ui-fullwidth-mockup.html`（一并提交）

- [ ] **Step 1: 全量构建**

```bash
cd dev/frontend && npm run build
```
Expected: `tsc` 无错误，`vite build` 成功。

- [ ] **Step 2: 启动前端做响应式验收**

```bash
cd dev/frontend && npm run dev
```
打开 `http://127.0.0.1:5173/`，在 DevTools 响应式模式依次检查：

| 宽度 | 预期 |
|---|---|
| 3840 | 侧栏 248、右栏约 440、内容吃满、无整页横滚 |
| 2560 | 侧栏约 248、右栏 440 上限、看板 5 列 |
| 1920 | 侧栏约 230、右栏约 384、字号约 15px |
| 1440 | 侧栏 220、右栏约 340、内容仍无 1500 限宽 |
| 1280 | 侧栏 200、看板列可压缩/横向滚动 |
| 1024 | 侧栏 68px 轨道；大厅/任务详情右栏上下堆叠 |
| 860 | 侧栏抽屉、单列、表格本地横滚 |

检查项：
1. `document.documentElement.scrollWidth <= document.documentElement.clientWidth`。
2. 大厅右栏拖拽/折叠/刷新持久化。
3. 任务详情会话用满剩余高度，输入框 sticky。
4. 任务详情右栏拖拽/折叠。
5. 窄屏汉堡/Esc/焦点返回。
6. 表格页只在 `.table-scroll` 内横滚。
7. 知识库/提醒/工具等宽屏卡片多列。
8. 深色主题、状态徽章语义不变。

停止 dev server。

- [ ] **Step 3: 更新 spec 状态**

把 spec 顶部：

```markdown
- 状态：待实现（已确认方向：raft 式全宽布局，保留深色主题）
```

改为：

```markdown
- 状态：已实现（2026-10-23）
```

- [ ] **Step 4: 提交 UI 实现与设计文档**

```bash
git add dev/frontend/src dev/frontend/index.html \
  docs/superpowers/specs/2026-10-23-ui-fullwidth-responsive-design.md \
  docs/superpowers/specs/2026-10-23-ui-fullwidth-mockup.html
git commit -m "feat(ui): full-width responsive layout for 2k/4k and narrow screens" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 5: 最终检查**

```bash
git status --short
git log --oneline -5
```
Expected: 工作区干净（或只剩与本计划无关的文件），最近提交包含 UI 实现。

---

## Self-Review

**Spec coverage:**
- 应用壳/侧栏/工具条/内容区 → Task 2 + Task 3。
- 响应式变量与断点 → Task 2。
- 右侧面板拖拽/折叠/持久化 → Task 1 + Task 4 + Task 5。
- 大厅看板全宽 → Task 4。
- 任务详情全宽、全高会话、sticky 输入框 → Task 5。
- 角色工坊、表格页、卡片页 → Task 2 + Task 6 + Task 7。
- 可访问性 → Task 8。
- 验收与文档 → Task 9。

**Placeholder scan:** 无 TBD/TODO；所有改动均给出可执行代码或精确命令。

**Type consistency:** `usePanelLayout` 的 `PanelLayout` 字段（`width/collapsed/dragging/toggle/reset/onPointerDown/onKeyDown/style`）在 Lobby/TaskDetail 中按同名使用；`--panel-w` 自定义属性通过 `CSSProperties` 断言；`grid-template-columns` 统一在 CSS 中定义，避免内联样式覆盖媒体查询。
