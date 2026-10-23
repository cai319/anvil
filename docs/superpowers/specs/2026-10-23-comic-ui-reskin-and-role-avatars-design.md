# Anvil 漫画工坊 UI 装修 + 角色头像设计

- 日期：2026-10-23
- 状态：设计已确认，待实现
- 范围：前端视觉系统、应用壳与主要页面结构、角色头像的选择与修改
- 非范围：业务逻辑、工作流引擎、HITL 流程、运行时/Agent 行为、浅色主题、新 npm 依赖

## 1. 背景与目标

Anvil 前端目前是深色玻璃质感：单文件 `dev/frontend/src/style.css`（约 1160 行）、靛蓝主色、8–12px 圆角、部分遗留硬编码浅色（如 `#fdeaea`、`#7a6ff0`），导航和卡片里混用 emoji / unicode 图标。2026-10-23 已完成全宽响应式壳层，本次不再改信息架构，而是在此基础上做一次完整的视觉装修，并加入角色头像。

目标：

1. 全站统一为「漫画工坊 / Ink & Paper」视觉语言：墨黑工作台、奶油纸面板、2px 墨线、硬偏移阴影、半调网点。
2. 在不动功能流程的前提下，优化全局壳层、任务大厅、任务详情、角色工坊和配置/表格页的局部结构。
3. 角色创建时可以选择卡通头像，创建后可以修改；头像在全站角色出现的位置一致生效。
4. 不引入新的 npm 运行依赖，头像素材本地打包、离线可用。
5. 保持现有响应式断点、面板拖拽/折叠、移动端抽屉和所有键盘操作。

## 2. 视觉方向：漫画工坊（Ink & Paper）

### 2.1 概念

把 Anvil 做成一间在纸上工作的本地工作室：

- **壳层是墨**：侧栏、页头、画布使用近黑墨色，承载导航和全局状态。
- **内容是纸**：任务卡、表格、弹窗、表单、消息气泡使用奶油纸面，正文用墨色字，保证密集信息可读。
- **描边是笔**：2px 墨线负责分组和强调；颜色块只表达状态。
- **阴影是贴纸**：硬偏移阴影（2–6px）制造纸片层叠感，不使用模糊阴影和玻璃模糊。
- **装饰要克制**：轻微旋转、胶带、半调网点只用于头像、状态章和空态；表单和表格保持平整。

### 2.2 色彩 token

```css
:root {
  /* 墨色壳层 */
  --ink-900: #14131a;
  --ink-800: #1b1a23;
  --ink-700: #241f2e;
  --ink-line: rgba(247, 239, 227, 0.16);
  --ink-line-strong: rgba(247, 239, 227, 0.28);

  /* 纸面 */
  --paper: #f7efe3;
  --paper-2: #efe4d2;
  --paper-3: #fffaf1;

  /* 文字 */
  --text-on-ink: #f7efe3;
  --text-on-ink-muted: #a8a0b4;
  --text-on-ink-faint: #6f6878;
  --text-on-paper: #14131a;
  --text-on-paper-soft: #4b4652;
  --text-on-paper-muted: #7a7385;

  /* 状态与强调 */
  --yellow: #facc15;
  --cyan: #22d3ee;
  --red: #f87171;
  --green: #4ade80;
  --blue: #60a5fa;

  /* 描边、圆角、阴影 */
  --stroke: 2px;
  --radius-sm: 6px;
  --radius: 10px;
  --radius-lg: 14px;
  --shadow-1: 2px 2px 0 rgba(20, 19, 26, 0.22);
  --shadow-2: 4px 4px 0 rgba(0, 0, 0, 0.4);
  --shadow-3: 6px 6px 0 rgba(0, 0, 0, 0.42);
  --shadow-yellow: 3px 3px 0 var(--yellow);
  --shadow-cyan: 3px 3px 0 var(--cyan);
  --shadow-red: 3px 3px 0 var(--red);

  /* 字体 */
  --font-body: "Inter", system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
```

对比度规则：

- 纸面（`--paper*`）上只使用墨色文字（`--text-on-paper*`）。
- 墨色壳层上只使用奶油色文字（`--text-on-ink*`）。
- 彩色块（黄/青/红/绿/蓝）上使用墨色文字，不叠加第二种彩色文字。
- 次要信息可以用 `--text-on-paper-muted` / `--text-on-ink-muted`，但正文不低于 13px。

### 2.3 字体与排版

- 正文字体：Inter + 系统中文（PingFang SC / Microsoft YaHei），不引入 Web 字体。
- 基础字号 13px，行高 1.55；正文 500–600，标题 800–900。
- H1 24px / 900 / `letter-spacing: -0.03em`；H2 17px / 850；H3 13px / 800。
- 微标签使用等宽字体、8–9px、全大写、`letter-spacing: 0.12em`，只用于状态、ID、计数和 eyebrow。
- 数字使用 `font-variant-numeric: tabular-nums`；ID、slug、路径、日志用等宽字体。
- 不使用装饰性字体；漫画感来自字重、描边和布局，而不是字体本身。

### 2.4 图标

- 统一使用 Lucide 风格线性 SVG，描边 2px，尺寸 16px（导航/按钮）或 18px（空态）。
- 用本地 `Icon` 组件内联路径，不新增依赖；图标路径按 ISC 许可使用并在文件头注明来源。
- 移除全站 emoji / unicode 图标：导航（✉、▦、⑂、#、◉、⚙、◇、◷、⌘、◈）、卡片（👥、👤、⏳）、提示（🔔）、按钮（＋、⇥、←、×）等。
- 图标颜色跟随上下文：墨面用奶油色，纸面用墨色，激活态用黄色或青色；不做多色图标。

### 2.5 纹理与动效

- 画布半调网点：`radial-gradient(rgba(247,239,227,.05) 1px, transparent 1.2px)`，`background-size: 6–7px`。
- 纸面轻微纸纹：同样使用极低透明度的点阵，不引入图片资源。
- 悬停：`translate(-1px, -1px)`，阴影从 3px 增到 4px。
- 按下：`translate(2px, 2px)`，阴影收缩。
- 状态呼吸点：1.8s 透明度脉冲；`prefers-reduced-motion: reduce` 时关闭位移和脉冲。

## 3. 组件系统

所有组件优先复用现有 class 名，减少 JSX 改动；新增 class 只用于新的结构块。

### 3.1 按钮

| 变体 | 外观 | 用途 |
| --- | --- | --- |
| primary | 黄底墨字、2px 墨边、红色硬阴影 | 新建、派发、发送、保存 |
| secondary | 纸底墨字、2px 墨边、灰色硬阴影 | 常规操作 |
| accent | 青底墨字、墨边 | 强调但非主操作 |
| ghost | 透明底、奶油描边、奶油字 | 墨色壳层上的次要操作 |
| danger | 纸底/红底、红字或墨字、红边 | 删除、驳回、取消 |

- 高度 30px（普通）/ 25px（small）/ 34px（大号表单按钮）。
- 焦点态：`box-shadow: 0 0 0 3px rgba(34,211,238,.45)`，不改变描边颜色。
- 图标按钮必须带 `aria-label`。

### 3.2 输入与表单

- 纸底、2px 墨边、6px 圆角，高度 34–36px；textarea 最小 80px。
- 焦点：青色 3px 偏移阴影；错误：红色描边 + 红色阴影。
- 标签在上、提示在下；提示用 11px `--text-on-paper-muted`。
- `select` 使用原生控件，加统一内边距和箭头图标。

### 3.3 状态徽章

- 形状：4px 圆角、2px 墨边、9px 等宽大写字。
- 颜色映射：
  - todo / closed / abandoned：纸灰 + 墨字
  - in_progress / running：黄底墨字
  - in_review / queued / unverified：琥珀描边 + 纸底
  - done / completed / ready：绿底墨字
  - busy / failed / unhealthy：红底墨字
  - submitted / reviewing / projecting / created：青底墨字
- 线上状态点（角色点灯）保留现有五色语义：绿=本任务工作中、红=其他任务、琥珀=切换中、蓝=空闲有上下文、灰=空闲。

### 3.4 卡片、页签、表格

- 卡片：纸底 `--paper-3`、2px 墨边、`--shadow-1`；悬停抬升 1px；选中用 `--shadow-yellow`。
- 页签：横向分段，4–5px 间距；激活项纸底 + 2px 墨边 + 黄色硬阴影；计数用小红/青圆角块。
- 表格：整张纸面，外层 2px 墨边；表头墨底奶油字、吸顶；行分隔 1.5px 虚线；悬停浅黄；操作列右对齐；外层 `.table-scroll` 横向滚动。

### 3.5 弹窗、空态、提示

- 弹窗：纸面、2px 墨边、`--shadow-3`；遮罩 `rgba(20,19,26,.72)` + 半调网点；关闭按钮红色圆形。
- 空态：虚线墨边 + 柔光图标 + 一句可行动提示；不用大面积插画。
- 错误提示：红边纸条；待评审提示：黄边纸条；信息提示：青边纸条。

### 3.6 头像

见第 6 节。

## 4. 页面结构

### 4.1 全局壳层

- 保留 `.app-shell` 的 100dvh 网格、侧栏宽度变量、折叠 rail、移动端抽屉和现有断点。
- 侧栏：墨黑底、奶油字；品牌改为黄色 burst 标 + `ANVIL!`；导航激活项为纸面标签 + 黄色硬阴影；分组标题改为等宽大写。
- 页头：墨黑、底部 2px 奶油线；面包屑 + 页面 kicker；右侧 Token chip、全场冻结按钮。
- 内容画布：墨黑 + 半调网点；页面标题统一使用新的 `PageHeader`：

```tsx
<PageHeader
  kicker="MISSION BOARD"
  title="任务大厅"
  subtitle="5 个角色在线 · 2 个任务进行中"
  actions={<button className="primary"><Icon name="plus" />新建任务</button>}
/>
```

- 新增共享组件：`PageHeader`、`SectionHeader`、`EmptyState`、`StatusPill`、`Icon`、`RoleAvatar`。
- 不新增页面、不改路由。

### 4.2 任务大厅

- 看板列变成纸面板：顶部状态色块 + 列名 + 计数；列内卡片间距 6–7px。
- 任务卡分三层：标题（最多 2 行）、角色与标签（`RoleAvatar` + slug + tag）、操作（派发/通过/驳回/去收件箱）。
- 进行中卡片用黄色边条 + 状态标签区分（当前没有可靠的细粒度进度数据，不显示虚假百分比）；待评审卡片突出「去收件箱」入口；空列显示「暂无任务」空态。
- 右栏保持「实时会话 + 最近活动」上下堆叠，不改成页签；会话卡改为纸便签，活动改为等宽时间 + 角色 + 动作的紧凑日志。
- 面板拖拽、折叠、宽度持久化沿用 `usePanelLayout`。
- 工具栏保留现有功能（新建任务、显示/隐藏右栏），本轮不新增搜索/筛选接口。

### 4.3 任务详情

- 把 `.session-head`、待评审提示和页签视觉上组织成一张任务单：
  - 左侧：返回链接、kicker、标题、印章式状态、元信息（参与角色/tags/ID）。
  - `.session-actions` 保留在会话列顶部，使用同一套纸面按钮，避免把状态相关操作移出会话上下文。
  - 待评审时在任务单下方显示黄边提示条，不再单独占一整块。
- 页签改为粗颗粒分段式：会话 / 工作流 / 子任务 / 角色运行时，计数用小徽章。
- 会话线程：
  - 角色消息：纸面气泡 + 头像 + 名称/角色标签 + 时间。
  - 人类消息：黄色气泡 + 人类头像。
  - 系统事件：居中虚线 + 等宽小字，不再用卡片行。
  - @ 提及：青色高亮块。
  - 保留筛选、跳到最新、加载更早、自动滚动和长度限制。
- 输入条：纸面长条，左侧 @ 按钮，右侧黄色发送按钮；@ 菜单改为纸面浮层，键盘上下选择逻辑不变。
- 右栏「任务档案」分区：基本信息、描述（可展开）、参与角色（头像 + 状态点 + 状态 + 队列）、验收标准、scope。
- 工作流/子任务页签保留现有全部操作，只把步骤行改成带编号的任务步骤样式。

### 4.4 角色工坊

- 新建角色面板改为纸面表单：
  - 第一行：slug、显示名、运行时。
  - 第二行：头像选择（15 个漫画头像 + 首字母方案）。
  - 主按钮「新建角色」；下方 AI 生成创建共用同一个头像选择。
  - 校验提示改为纸条样式，不再用裸红字。
- 角色列表改为花名册：`RoleAvatar`、显示名、slug、状态/默认助理标记、删除按钮；选中项用黄色硬阴影。
- 角色详情头部：大头像 + 显示名 + slug + 关键状态；点击头像或「更换头像」打开同一个选择器。
- 基础页签增加「外观」一行；其余页签（版本、工具、私有工作区、技能/MCP、运行时、删除）保持现有功能和顺序。
- 头像保存只需调用独立接口，不重存整份角色配置。

### 4.5 配置 / 表格类页面

覆盖：运行时、知识库、治理、提醒、收件箱、频道、工具、活动历史、角色会话、运行详情。

- 表格统一纸面样式、墨色表头、吸顶、行悬停、横向滚动。
- 工具栏输入/选择/按钮统一高度和描边；危险操作使用 danger 变体。
- 弹窗、错误/空态、加载态统一到第 3 节组件。
- 不改变接口、字段、业务流程和文案含义。

### 4.6 明确不动的部分

- 路由结构、数据流、HITL 人工评审、工作流引擎、租约/认领、Token 统计。
- 现有功能入口、按钮语义、接口调用顺序。
- 响应式断点（1199 / 1099 / 899）和移动端抽屉行为。
- 浅色主题：本次仍只做深色。

## 5. 角色头像功能

### 5.1 行为

1. 新建角色（手动）时，在表单里直接选择头像；默认预选一个由 slug 稳定映射的漫画头像。
2. AI 生成创建时，同样的选择器可用；未选择时按生成后的 slug 计算默认头像。
3. 创建后写入角色记录；之后可在角色详情中随时更换。
4. 可选 15 个漫画头像，或显式选择「首字母」保留原来的色块首字母方案。
5. 全站所有角色出现的位置统一使用该头像；没有头像的旧角色按 slug 稳定映射到默认漫画头像，不修改数据。
6. 系统/人类/工作流节点等非角色头像不使用该功能。

### 5.2 数据模型

在 `dev/backend/anvil/db.py` 的 `roles` 表增加列：

```sql
ALTER TABLE roles ADD COLUMN avatar TEXT;
```

沿用 `init_schema` 里现有的 try/except `ALTER TABLE` 迁移模式。值格式：

- `big-smile:<id>`：漫画头像预设，`id` 见附录 A。
- `initials`：强制使用首字母 + `avatar_color`。
- `NULL`：未选择，前端按 slug 计算默认漫画头像。

`RoleIn` / `RoleOut` 增加 `avatar: str | None`；`services.roles.create` / `update` 同时写入列和 `definition_json`，`_row` 返回 `avatar`。克隆角色时把源角色的 `avatar` 复制到新角色；`rollback` 等走 `update` 的路径在 definition 显式携带 avatar 时同步列，避免列与 definition 长期分叉。

### 5.3 API

- `POST /roles`：接受可选 `avatar`，写入角色。
- `POST /roles/auto`：接受可选 `avatar`，生成角色时一并写入。
- 新增 `PUT /roles/{slug}/avatar`：

```json
{ "avatar": "big-smile:ace" }
```

返回 `RoleOut`。仅自然人可调用（沿用 `require_human`）。校验规则：

- `null` / `"initials"` / `^big-smile:[a-z0-9-]{1,32}$`；
- 其他格式返回 422；
- 未知 id 允许写入，由前端回退到默认头像，便于以后扩展素材包。

头像字段以 `roles.avatar` 列为准；创建时同时镜像进 `definition_json` 以保持现有 `avatar_color` 的存储习惯。头像单独更新时合并 `definition_json`，但不触发角色画像版本快照，避免把纯外观修改混进版本历史。

### 5.4 素材与授权

- 素材：DiceBear「Big Smile」风格，生成 15 个本地 SVG，放在 `dev/frontend/public/avatars/big-smile/`。
- 生成方式：设计阶段通过 DiceBear API 生成并保存，运行期不联网。
- 授权：Big Smile by Ashley Seo，CC BY 4.0，来源 Figma。新增 `dev/frontend/public/avatars/CREDITS.md`，写明作者、许可、来源链接和 DiceBear 版本。
- 单个 SVG 约 5–10KB，总计约 100–150KB；不新增 npm 依赖。

### 5.5 前端组件

新增 `dev/frontend/src/components/RoleAvatar.tsx`：

```tsx
type Props = {
  slug: string;
  name: string;
  avatar?: string | null;
  color?: string | null;
  size?: number;
  className?: string;
};
```

解析顺序：

1. `avatar === "initials"` → 首字母 + `avatar_color`；颜色缺失时沿用现有 `avatarColor(slug)` 规则。
2. `avatar` 匹配 `big-smile:<id>` 且 id 在预设表 → `/avatars/big-smile/<id>.svg`。
3. 否则用 FNV-1a 哈希 `hash(slug) % 15` 选默认预设，保证同一 slug 始终得到同一头像。
4. 图片加载失败 → 首字母兜底（`onError`）。

新增 `dev/frontend/src/components/AvatarPicker.tsx`：

- 弹窗式网格：8 列 × 2 行（15 个头像 + 首字母），选中项黄色硬阴影 + 勾选角标。
- 支持键盘 Tab / Enter / Escape；关闭按钮带 `aria-label`。
- 创建角色时内联展示同一组选项（不弹窗），角色详情里点击「更换头像」时用弹窗。
- 保存后触发角色列表和当前详情刷新；其他页面通过 SSE/刷新自然更新。

### 5.6 渲染点清单

| 位置 | 文件 | 处理 |
| --- | --- | --- |
| 任务大厅会话卡 | `src/pages/Lobby.tsx` | 换成 `RoleAvatar` |
| 任务详情消息头像 | `src/pages/TaskDetail.tsx` | 角色消息用 `RoleAvatar`；人类/系统保留专用头像 |
| 新建任务成员列表 | `src/pages/NewTaskModal.tsx` | 换成 `RoleAvatar` |
| 角色会话页 | `src/pages/RoleSessions.tsx` | 换成 `RoleAvatar` |
| Agent 会话页 | `src/pages/AgentSessions.tsx` | 换成 `RoleAvatar` |
| 角色工坊列表/详情 | `src/pages/Roles.tsx` | 换成 `RoleAvatar`，增加选择器入口 |

### 5.7 前端 API 与类型

- `Role` 类型增加 `avatar?: string | null`。
- 新增 `api.updateRoleAvatar(slug, avatar)`。
- `api.createRole` 增加可选 `avatar` 参数；`api.autoCreateRole` 同样。
- `avatar_color` 保留为兜底，不删除。

## 6. 前端架构

### 6.1 样式拆分

把 `src/style.css` 拆成：

```text
src/styles/tokens.css     设计 token、主题变量
src/styles/base.css       reset、排版、滚动条、焦点、纹理
src/styles/components.css 按钮/输入/徽章/卡片/页签/表格/弹窗/空态/头像
src/styles/layout.css     app shell、侧栏、页头、面板、响应式
src/styles/pages.css      页面专属规则
```

在 `src/main.tsx` 按 tokens → base → pages → components → layout 顺序导入。`pages.css` 先加载旧页面规则作为兼容底座，`components.css` 和 `layout.css` 后加载，用同选择器覆盖成新视觉；这样不用一次性重写全部页面规则。保留现有 class 名，先做 token 替换，再逐页调整结构。

### 6.2 图标组件

新增 `src/components/Icon.tsx`：

- `name` 映射到 SVG path 数组；`size` 默认 16；`strokeWidth` 默认 2；`aria-hidden` 默认 true。
- 约 30 个图标：dashboard、inbox、git-branch、hash、users、cpu、book、bell、wrench、shield、search、plus、send、at-sign、x、check、chevron-left/right/down、clock、alert、message、panel-right、menu、trash、refresh、play、filter、file-text、terminal。
- 路径来自 Lucide（ISC），在文件头注明来源与许可。

### 6.3 不新增依赖

- 不引入 Tailwind、Radix、lucide-react 或其他 UI 库。
- 头像 SVG 作为静态资源放在 `public/`，由 Vite 直接服务。
- 不引入 Web 字体；标题字重使用系统字体的 800/900。

## 7. 响应式与可访问性

### 7.1 响应式

沿用现有断点：

- ≥ 1200px：完整侧栏 + 可调右栏。
- 900–1199px：侧栏收成图标 rail；任务大厅/任务详情面板可折叠。
- < 900px：侧栏抽屉；任务详情和右栏上下堆叠。

补充：

- 移动端把 2px 描边保留，硬阴影缩小到 2px，避免横向溢出。
- 表格外层继续使用 `.table-scroll`；纸面表格最小宽度不低于 720px。
- 卡片网格在窄屏单列，按钮允许换行。

### 7.2 可访问性

- 所有交互元素有可见 `:focus-visible` 焦点环（青色 3px）。
- 图标按钮必须有 `aria-label`；头像选择器用 `role="dialog"` + `aria-modal="true"`。
- 颜色不单独承担信息：状态同时有文字/圆点/形状。
- 正文对比度 ≥ 4.5:1；纸面墨字和墨面奶油字均满足。
- `prefers-reduced-motion: reduce` 时关闭位移、脉冲和过渡。
- 保留现有 aria 属性、表格语义和表单 label。

## 8. 实施顺序

1. **后端头像数据层**：迁移列、schema、service、`PUT /roles/{slug}/avatar`、创建/自动创建支持 avatar；补测试。
2. **前端基础视觉层**：拆分 styles、tokens、base、Icon 组件；替换导航和通用图标。
3. **头像链路**：生成并打包 15 个 SVG、CREDITS；`RoleAvatar`、`AvatarPicker`、创建/修改流程；替换 6 个头像渲染点。
4. **壳层与两大主页面**：App 壳层、任务大厅、任务详情。
5. **配置/表格页 + 收尾**：其余页面统一纸面组件；响应式、可访问性、构建和全量 QA。

每一阶段独立可构建、可回滚；CSS 阶段不改变 DOM 结构，结构阶段再逐页调整。

## 9. 测试与验收

### 9.1 自动化

- 后端：`pytest` 相关角色测试；新增 avatar 创建、更新、非法值、旧库迁移用例。
- 前端：`npm run build`（tsc + vite）通过。
- 静态检查：grep 确认无遗留 emoji/unicode 图标、无遗留浅色硬编码。

### 9.2 手工走查

- 宽度：1440 / 1200 / 1024 / 900 / 390。
- 路由：任务大厅、任务详情、收件箱、活动历史、目标拆解、频道、角色工坊、运行时、知识库、提醒、工具、治理、Agent 会话、Run 详情。
- 交互：侧栏折叠、移动端抽屉、任务大厅右栏拖拽/折叠、任务详情右栏拖拽/折叠、表格横向滚动、弹窗 Escape/遮罩关闭。
- 头像：手动创建选头像、AI 创建选头像、详情更换、首字母方案、旧角色默认头像、图片加载失败兜底。
- 功能回归：新建任务、派发、评审通过/驳回、发送消息、@ 提及、工作流操作、角色删除。

### 9.3 验收标准

1. 所有页面视觉统一为漫画工坊，无旧玻璃质感残留。
2. 无 emoji/unicode 图标；导航、按钮、空态图标风格一致。
3. 角色头像可在创建时选择、创建后修改，并在全站一致显示。
4. 所有现有功能和交互零回归；构建与相关测试通过。
5. 1440 到 390 宽度无横向溢出、无遮挡、无不可点击区域。
6. 键盘可完成头像选择、弹窗关闭和主要表单操作。

## 10. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 单文件 CSS 重写导致跨页回归 | 保留 class 名，先 token 后结构；按页面清单逐页 QA |
| 纸面 + 硬阴影在密集表格里显得嘈杂 | 表格内不使用旋转和装饰，阴影只保留外框；悬停才用黄色 |
| 头像 API 改动影响角色创建/更新 | 头像字段可选，旧数据不受影响；独立接口不重存配置；补迁移和接口测试 |
| 默认头像改变旧角色外观 | 这是本次预期效果；用户可显式选择「首字母」恢复原方案 |
| CC BY 素材署名遗漏 | `CREDITS.md` + 设计文档记录；素材随前端打包 |
| 描边/阴影导致移动端横向溢出 | 移动端缩小阴影、允许按钮换行、表格外层滚动 |
| 图标替换遗漏 | 收尾阶段全仓库 grep emoji/unicode 和旧图标字符 |

## 附录 A：头像预设

| id | 素材 |
| --- | --- |
| ace | big-smile/ace.svg |
| bee | big-smile/bee.svg |
| cid | big-smile/cid.svg |
| dot | big-smile/dot.svg |
| eli | big-smile/eli.svg |
| fay | big-smile/fay.svg |
| gus | big-smile/gus.svg |
| hana | big-smile/hana.svg |
| ivan | big-smile/ivan.svg |
| june | big-smile/june.svg |
| kip | big-smile/kip.svg |
| luna | big-smile/luna.svg |
| max | big-smile/max.svg |
| nia | big-smile/nia.svg |
| omar | big-smile/omar.svg |

另有一个 `initials` 选项，使用 `avatar_color` + 显示名首字。

## 附录 B：涉及文件

新增：

- `dev/frontend/src/styles/tokens.css`
- `dev/frontend/src/styles/base.css`
- `dev/frontend/src/styles/components.css`
- `dev/frontend/src/styles/layout.css`
- `dev/frontend/src/styles/pages.css`
- `dev/frontend/src/components/Icon.tsx`
- `dev/frontend/src/components/ui.tsx`（PageHeader / SectionHeader / EmptyState / StatusPill）
- `dev/frontend/src/components/RoleAvatar.tsx`
- `dev/frontend/src/components/AvatarPicker.tsx`
- `dev/frontend/public/avatars/big-smile/*.svg`
- `dev/frontend/public/avatars/CREDITS.md`

修改：

- `dev/frontend/src/main.tsx`（样式导入）
- `dev/frontend/src/style.css`（删除；样式导入改到 `main.tsx`）
- `dev/frontend/src/App.tsx`（导航图标、壳层 class）
- `dev/frontend/src/api/client.ts`（Role 类型、头像接口）
- `dev/frontend/src/pages/Lobby.tsx`
- `dev/frontend/src/pages/TaskDetail.tsx`
- `dev/frontend/src/pages/Roles.tsx`
- `dev/frontend/src/pages/NewTaskModal.tsx`
- `dev/frontend/src/pages/RoleSessions.tsx`
- `dev/frontend/src/pages/AgentSessions.tsx`
- 其余页面按统一组件做少量 class 调整
- `dev/backend/anvil/db.py`（avatar 列迁移）
- `dev/backend/anvil/schemas.py`（RoleIn/RoleOut）
- `dev/backend/anvil/services/roles.py`（create/update/_row）
- `dev/backend/anvil/api/roles.py`（创建/自动创建/头像接口）
- `dev/tests/`（头像用例）

## 附录 C：头像接口示例

```http
PUT /roles/developer/avatar
Content-Type: application/json

{ "avatar": "big-smile:ace" }
```

```json
{
  "slug": "developer",
  "display_name": "开发工程师",
  "avatar": "big-smile:ace",
  "avatar_color": "#6d7cff",
  "home_path": "...",
  "enabled": true,
  "created_at": "...",
  "updated_at": "...",
  "definition": {}
}
```
