# Anvil 全宽响应式 UI 设计

- 日期：2026-10-23
- 状态：已实现（2026-10-23）
- 范围：前端应用壳、页面布局与响应式尺度
- 非范围：后端/API、浅色品牌重做、外部字体与新依赖

## 1. 背景与目标

当前前端是固定顶栏 + 居中内容区：

- `.page { max-width: 1500px }`（`src/style.css:86`）在 2K/4K 下留下大量空白；
- `.session { max-width: 1100px }`（`src/style.css:286`）让任务详情、收件箱、run 详情尤其窄；
- `.task-layout` 右栏固定 300px（`src/style.css:339`），`.lobby` 右栏固定 320px；
- `.session-thread { max-height: 62vh }`（`src/style.css:730`）在 4K 垂直空间下浪费高度；
- 顶栏 10 个导航项没有断点，窗口变窄时容易拥挤；
- CSS 中 `.lobby` 有一条过时的三栏规则，被后面的两栏规则覆盖。

目标：

1. 2K/4K（Windows 125% / 150% / 200% 缩放）下主内容吃满窗口。
2. 参考 raft.build 的 app shell：**固定全高、左侧导航、主区流体、右侧可调宽面板**。
3. 1280–3840 CSS px 宽度内保持可用；900 以下退化为单列 + 抽屉侧栏。
4. 保留 Anvil 现有深色主题、配色与信息架构，不引入新依赖。

## 2. 参考对象的取舍

从 raft.build 的 app shell 提取的是结构，不是品牌：

- 固定 `100dvh` 应用壳，侧栏与主区各自滚动；
- 左侧竖向导航，宽度约 200–250px；
- 顶部工具条高度 48–56px；
- 主内容不居中限宽，右栏由独立宽度变量控制，可拖拽/折叠；
- 聊天/线程主列保持可读行宽，外层容器仍吃满。

不采用 raft 的浅奶油底、黄色强调、Space Grotesk 字体和粗边框贴纸风格。

## 3. 布局架构

### 3.1 应用壳

目标 DOM：

```tsx
<div className="app-shell">
  <aside className="sidebar" aria-label="主导航">...</aside>
  <div className="app-main">
    <header className="app-header">...</header>
    <main className="content">...</main>
  </div>
</div>
```

关键 CSS：

```css
html, body, #root { height: 100%; }
body { overflow: hidden; }
.app-shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  height: 100dvh;
}
.app-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.app-header { flex: none; height: 56px; }
.content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--content-pad);
}
```

- 页面滚动只在 `.content` 内发生；侧栏与工具条固定。
- 移除 `.topbar`、`.page` 的旧布局职责；`.page` 如保留只作为兼容类，不再限宽。
- `100dvh` 在移动端浏览器地址栏变化时仍正确；不支持时回退 `100vh`。

### 3.2 左侧导航

导航分组：

| 组 | 项 |
|---|---|
| 工作台 | 收件箱、任务大厅、目标拆解、频道 |
| 角色与知识 | 角色工坊、运行时、知识库、提醒 |
| 系统 | 工具、治理 |

- 展开态：`--sidebar-w`，显示品牌、分组标题、图标位、文字、收件箱徽章。
- 折叠态（<1200px 或手动折叠）：68px 轨道，只显示单字/图标 + `title`/`aria-label`。
- <900px：侧栏变为抽屉；顶栏出现汉堡按钮，点击打开，Esc/遮罩关闭，焦点返回汉堡。
- 当前项高亮：左侧 3px 强调条 + `surface-2` 背景。
- 侧栏底部放“折叠/展开”按钮；状态存 `localStorage`（键 `anvil.sidebar.collapsed`）。

### 3.3 顶部工具条

- 高度 56px（<900px 为 52px），固定在 `.app-main` 顶部。
- 左侧：面包屑/当前页面名（如 `任务大厅`）；窄屏时显示汉堡 + 页面名。
- 右侧：Token 统计、全场冻结按钮，保持现有 `TopStatus` 行为。
- 不再承载 10 个导航项；导航全部移入侧栏。

### 3.4 右侧面板

新增 `src/usePanelLayout.ts`，供任务大厅与任务详情复用：

```ts
type PanelLayoutOptions = {
  storageKey: string;
  min?: number; // 默认 300
  max?: number; // 默认 560
};

usePanelLayout(opts): {
  width: number;
  collapsed: boolean;
  dragging: boolean;
  toggle: () => void;
  reset: () => void;
  onPointerDown: (e: React.PointerEvent) => void;
  style: React.CSSProperties;
}
```

- 宽度初始值：`localStorage` 有效值 → `clamp(min, round(innerWidth * 0.2), max)`。
- Pointer 拖拽更新宽度；`pointerup` 后写回 `localStorage`；双击分隔条 `reset()`。
- 宽度范围 300–560；<1100px 时禁用拖拽，布局自动上下堆叠。
- 折叠状态存 `localStorage`（`anvil.panel.<key>.collapsed`）。
- 分隔条：`role="separator"`、`aria-orientation="vertical"`、`tabIndex=0`，左右方向键调整 20px。

## 4. 响应式尺度

使用 CSS 变量 + 少量断点，不做逐页魔法数字：

```css
:root {
  --sidebar-w: clamp(200px, 12vw, 248px);
  --sidebar-rail-w: 68px;
  --side-panel-w: clamp(320px, 20vw, 440px);
  --content-pad: clamp(14px, 1.15vw, 30px);
  --fs-base: clamp(14px, 0.3vw + 9.5px, 16px);
  --fs-h1: clamp(1.35rem, 0.25vw + 1.1rem, 1.8rem);
  --radius: 12px;
}
```

| CSS 视口宽度 | 侧栏 | 内容内边距 | 右侧面板 | 基础字号 |
|---|---|---|---|---|
| ≥1920 | 230–248px | 28–30px | 384–440px | 15–16px |
| 1440–1919 | 220px | 22px | 340–380px | 14–15px |
| 1200–1439 | 200px | 18px | 320px | 14px |
| 900–1199 | 68px 轨道 | 16px | <1100 时上下堆叠 | 14px |
| <900 | 抽屉 | 14–16px | 堆叠 | 14px |

- 基础字号只影响正文与控件；聊天正文最大宽度 1600px，避免 4K 下单行过长。
- 大屏按钮/输入框最小高度 36–40px；表格行高随字号自动增加。
- 保留 `prefers-reduced-motion`，新增动画只用于侧栏/抽屉过渡，可被该媒体查询关闭。

## 5. 页面级布局

### 5.1 任务大厅（Lobby）

- `.lobby` 改为 `grid-template-columns: minmax(0, 1fr) var(--lobby-panel-w)`，右栏宽度由 `usePanelLayout` 提供。
- 看板 5 列 `flex: 1 1 0; min-width: 220px`；≥1280px 每列均分，<1280px 允许横向滚动。
- 右栏“实时会话 + 最近活动”可折叠；折叠后看板占满。
- 窄屏 <1100px：单列，右栏移到看板下方，分隔条隐藏。

### 5.2 任务详情（TaskDetail）

- 去掉 `.session` 的 1100px 限宽。
- `.task-layout` 改为 `minmax(0, 1fr) var(--task-panel-w)`；右栏 360–440，可拖拽/折叠。
- 保留线程**内部滚动**（现有 pinned/自动滚底逻辑依赖 thread 元素作为滚动容器），
  把 `.session-thread` 的 `max-height: 62vh` 改为
  `max-height: calc(100dvh - 56px - (var(--content-pad) * 2) - 210px)`，并设
  `min-height: 320px`；这样 1440/2160 高度下都能用满剩余空间。
- 输入框 `.session-compose` 使用 `position: sticky; bottom: 0`，背景与内容区一致。
- 右栏 `.task-side` 使用 `position: sticky; top: 12px; max-height: calc(100dvh - 56px - 24px); overflow-y: auto`。
- 会话正文（`.thread-text`、消息卡片）最大宽度 1600px，左对齐；元数据/操作按钮仍可用全宽。

### 5.3 角色工坊（Roles）

- `.roles-layout` 左列 `clamp(240px, 18vw, 320px)`，右侧详情 `minmax(0, 1fr)`。
- 详情中的 `dl` 改为 `grid-template-columns: minmax(120px, 180px) minmax(0, 1fr)`。
- 工具/技能列表在 ≥1600px 时用 `repeat(auto-fill, minmax(360px, 1fr))` 两列以上。

### 5.4 表格页（Runtimes / Governance / ActivityHistory）

- 新增 `.table-scroll { width: 100%; overflow-x: auto; }` 包裹 `.board`。
- 表格 `width: 100%`；窄屏最小宽度 720px，在容器内横向滚动，不让整页横向滚动。
- 表头 `position: sticky; top: 0`，长表滚动时保持可见。

### 5.5 列表/卡片页（Knowledge / Reminders / Tooling / Channels / Decompose）

- `.drafts` 在 ≥1500px 使用 `grid-template-columns: repeat(auto-fill, minmax(480px, 1fr))`，卡片间距 12px；<1500px 保持单列。
- `.card`、`.drafts > li` 不再固定窄宽，随网格拉伸。
- 表单工具栏 `flex-wrap: wrap`；输入框 `flex: 1 1 260px`，按钮不换行到单独一行。
- 频道/工具等已有表格或列表保持结构，只做宽度与间距适配。

### 5.6 收件箱 / Run 详情 / Agent 会话

- `.session` 去掉限宽后自然吃满。
- 收件箱卡片单列全宽；长文本保持现有换行规则。
- Run 日志 `.run-log` 保持 `max-height`（日志需要内部滚动），但宽度吃满。

## 6. 组件与文件

| 文件 | 变更 |
|---|---|
| `src/App.tsx` | 用 `.app-shell` / `.sidebar` / `.app-header` / `.content` 替换顶栏布局；导航分组；抽屉状态；保留 `TopStatus` 与 `InboxBadge` |
| `src/style.css` | 新 shell/导航/响应式变量；移除 `.page`/`.session` 限宽；清理重复 `.lobby` 规则；新增 `.table-scroll`、分隔条、抽屉样式 |
| `src/usePanelLayout.ts` | 新增：宽度/折叠/拖拽/localStorage/键盘分隔条 |
| `src/pages/Lobby.tsx` | 接入面板布局；看板列宽；右栏折叠 |
| `src/pages/TaskDetail.tsx` | 接入面板布局；移除 62vh；sticky 输入框；正文可读宽度 |
| `src/pages/Roles.tsx` | 2 列宽度变量；详情 `dl` 自适应 |
| `src/pages/Runtimes.tsx` / `Governance.tsx` / `ActivityHistory.tsx` | 表格包 `.table-scroll` |
| `src/pages/Knowledge.tsx` / `Reminders.tsx` / `Tooling.tsx` / `Channels.tsx` / `Decompose.tsx` | 列表/工具栏响应式类 |
| `index.html` | 无需新增依赖；保留现有 viewport |

## 7. 可访问性

- 侧栏 `<nav aria-label="主导航">`；折叠按钮 `aria-expanded`；抽屉按钮 `aria-controls`。
- 抽屉打开时 Esc 关闭、遮罩点击关闭；关闭后焦点回到汉堡按钮。
- 分隔条 `role="separator"`、`aria-valuemin/max/now`、方向键调整。
- 所有图标按钮有 `aria-label`/`title`；颜色对比不低于现有深色主题。
- 拖拽使用 Pointer Events + `setPointerCapture`，触屏不会误选文本。
- 尊重 `prefers-reduced-motion`。

## 8. 测试与验收

### 自动检查

- `cd dev/frontend && npm run build` 通过（`tsc && vite build`）。
- 不新增 npm 依赖；`package.json` 不变。
- 后端无改动；如需回归可跑 `cd dev && python3 -m pytest -q`（预期全绿）。

### 手工检查

在浏览器 DevTools 的响应式模式检查 CSS 宽度：

- 1280 / 1440 / 1920 / 2560 / 3840；
- Windows 125% / 150% / 200% 缩放对应 CSS 宽度约 3072 / 2560 / 1920；
- 高度 720 / 900 / 1440 / 2160。

验收清单：

1. 任意宽度下 `document.documentElement.scrollWidth <= clientWidth`（整页无横向滚动）。
2. 2560/3840 下主内容区没有 1500/1100 的居中空白，看板/详情/表格吃满。
3. ≥1200 侧栏展开，<1200 轨道，<900 抽屉；导航项不换行、不重叠。
4. 任务大厅与任务详情右栏可拖拽、可折叠，刷新后宽度/折叠状态保留。
5. <1100 右栏上下堆叠，分隔条隐藏，拖拽禁用。
6. 任务详情会话用满剩余高度（保留内部滚动与自动滚底）；输入框在滚动时可见。
7. 表格页窄屏横向滚动发生在 `.table-scroll` 内，整页不横滚。
8. 键盘可操作侧栏折叠、抽屉关闭、分隔条调整。
9. `npm run build` 无 TypeScript 错误。
10. 现有深色配色与状态徽章语义不变。

## 9. 风险与回滚

- **CSS 级联**：`.lobby`、`.session`、`.topbar` 存在历史重复规则；实施时先清理再叠加，避免旧规则覆盖新布局。
- **应用壳溢出**：所有 flex/grid 子项加 `min-width: 0; min-height: 0`，内容区单独滚动。
- **拖拽事件**：只监听 Pointer Events；窄屏禁用；组件卸载时移除全局监听。
- **localStorage 异常**：读取失败回退默认宽度/展开；写入失败静默忽略。
- **页面回归**：Shell 改动影响所有页面，先完成 App.tsx + style.css，再逐页验证；如出现严重回归，可回滚 App.tsx 与 style.css 的 shell 部分，页面组件改动可独立保留。
- **不引入外部字体/图标库**：避免离线环境与打包体积变化。

## 10. 非目标

- 不改浅色/黄色品牌视觉；
- 不引入 Space Grotesk 等外部字体；
- 不新增页面或后端接口；
- 不改变任务状态机、SSE、CLI 或数据模型；
- 不重写所有页面的信息架构，只调整布局、容器与响应式尺度。
