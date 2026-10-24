# 全站字号与头像尺度标准

- 日期：2026-10-24
- 范围：`dev/frontend/src` 全部页面、组件与样式
- 目标：建立一套可复用的设计语言，消除“同一类信息在这个页面大、那个页面小”的逐页硬编码。

## 1. 设计原则

1. **功能文字不设小字**：导航、标签、状态、计数、时间、按钮、表格、正文等功能性信息不得低于 `--fs-meta`。
2. **只有提示类可以小**：帮助/说明文字使用 `--fs-hint`，且仍随视口增长，1440 下不低于 12px。
3. **字号随视口连续变化**：使用 `clamp()`，不做“某个断点突然放大/缩小”；390 → 3840 连续过渡。
4. **语义取档，不按页面取档**：同一类信息在所有页面使用同一个 token。
5. **正文优先**：任何页面不得用 8–11px 承载需要阅读或操作的信息。
6. **头像按场景取档**：头像尺寸由所在场景决定，不允许全局等比放大；状态由阴影色表达。
7. **点灯机制保留**：既有 `role-live-dot` / `status-light` 状态点保持原样，头像阴影是叠加的状态提示，不替代点灯。

## 2. 字号层级

所有 token 定义在 `src/styles/tokens.css`。下表为关键视口下的计算值（px）。

| 角色 | Token | 390 | 768 | 1024 | 1440 | 1920 | 2560 | 3840 | 用途 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 页面主标题 | `--fs-h1` | 24 | 24 | 24 | 24.0 | 26.9 | 30.8 | 38.4 | 页面标题、任务会话标题 |
| 区块标题 | `--fs-h2` | 18 | 18 | 18 | 18.1 | 20.1 | 22.8 | 28.1 | 弹窗标题、角色详情标题 |
| 正文 | `--fs-base` | 15 | 15 | 15 | 15.5 | 17.3 | 19.7 | 24 | 正文、卡片标题、表格、任务描述 |
| 次级功能 | `--fs-sm` | 13 | 13 | 13 | 13.1 | 14.6 | 16.5 | 20 | 按钮、页签、返回、区块小标题、活动流 |
| 功能小字（下限） | `--fs-meta` | 12.5 | 12.5 | 12.5 | 12.5 | 13.8 | 15.5 | 18.8 | 标签、状态、计数、时间、徽章、表头 |
| 提示 | `--fs-hint` | 12 | 12 | 12 | 12 | 13.0 | 14.5 | 17.6 | 仅帮助/说明文字 |
| 侧栏导航项 | `--nav-item-size` | 15 | 15 | 15 | 15.0 | 16.5 | 18.6 | 22.7 | 侧栏一级导航 |
| 侧栏分组 | `--nav-label-size` | 13 | 13 | 13 | 13.1 | 14.2 | 15.7 | 18.8 | “工作台 / 角色与知识 / 系统” |
| 等宽文字 | `inherit` | — | — | — | — | — | — | — | ID、slug、时间戳跟随父级，不再 `0.85em` 缩小 |

行高：正文 `1.55`，功能小字 `1.35`，提示 `1.45`。

### 2.1 语义映射

- **正文**：`.thread-text`、`.wf-detail-text`、`.side-p`、`.side-list li`、`.task-meta dd`、`.history-table`、`.board`、`.inbox-item`、`.modal`、`.field`、`.card-link strong`、`.run-title`、`.role-name`、`.member-name`。
- **次级功能**：`button`、`.tabs button`、`.detail-tabs .tab`、`.session-back a`、`.page-subtitle`、`.page-crumb`、`.kanban-col h2`、`.wf-*` 的区块标题、`.activity li`。
- **功能小字**：`.nav-label` 以外的标签、`.status`、`.badge`、`.chip`、`.tab-count`、`.card-meta`、`.run-sub`、`.session-meta`、`.thread-meta`、`.mention-tag`、时间戳、表头、图例。
- **提示**：`.role-slug-hint`、`.hint`、`.session-compose p.muted`、`.field small`、`.avatar-picker-hint`、`small`。

## 3. 布局尺度

| Token | 计算式 | 1440 | 1920 | 2560 | 3840 |
|---|---|---:|---:|---:|---:|
| `--sidebar-w` | `clamp(200px, 12.5vw, 330px)` | 200 | 240 | 320 | 330 |
| `--sidebar-rail-w` | `clamp(68px, 4vw, 92px)` | 68 | 76.8 | 92 | 92 |
| `--content-pad` | `clamp(16px, 1.2vw, 36px)` | 17.3 | 23.0 | 30.7 | 36 |
| `--header-h` | `clamp(56px, 3.2vw, 76px)` | 56 | 61.4 | 76 | 76 |
| `--nav-item-h` | `clamp(40px, 2.6vw, 56px)` | 40 | 49.9 | 56 | 56 |
| `--control-h` | `clamp(34px, 2.2vw, 48px)` | 34 | 42.2 | 48 | 48 |
| `--control-h-sm` | `clamp(28px, 1.8vw, 38px)` | 28 | 34.6 | 38 | 38 |

右侧面板宽度由 `usePanelLayout` 的拖拽值（300–560px）通过 `--panel-w` 覆盖；CSS token 仅作回退。

## 4. 头像尺度与状态阴影

### 4.1 尺寸

| 场景 | 尺寸 | 说明 |
|---|---|---|
| 任务卡角色头像 | 按卡片内容宽度自适应，宽屏上限 28px | 仅保留头像，不再显示角色名；窄屏每行至少 5 个，宽屏能排多少排多少，超出换行 |
| 实时会话头像 | `--avatar-live` = `clamp(40px, 2.6vw, 56px)` | 右侧实时会话，比列表头像大一档 |
| 角色工坊列表/详情 | 组件 `size` 原值（28 / 52） | 保持原样，不做视口放大 |
| 任务会话线程 | 组件 `size` 原值（32） | 保持原样 |
| 成员选择、角色会话等 | 组件 `size` 原值（30 / 28 / 34） | 保持原样 |

移动端任务卡头像随卡片宽度缩放（约 18–25px），仍保证一行至少 5 个头像；宽屏上限 28px。

### 4.2 状态色

头像阴影为 `2px 2px 0 var(--role-status, var(--cyan))`。默认青色是既有外观；传入 `status` 后按以下语义着色：

| 状态 | 阴影色 |
|---|---|
| `working` / `running` / `projecting` / `green` | 绿色 |
| `queued`（已派发，角色正忙在别的任务上） | 橙色 `#fb923c` |
| `submitted` / `in_review` / `reviewing` / `amber` | 黄色 |
| `approved` / `completed` / `done` / `blue` | 青色 / 蓝色 |
| `pending` / `released` / `idle` / `gray` | 中性灰 |
| `failed` / `error` / `red` | 红色 |

状态来源：
- 任务卡：**以这张卡自己的诉求为准**，结合 `RoleRuntimeStatus` 推导，而不是直接取 `role_claims`。
  - `rt.alive && rt.status === "busy"` 且 `rt.current_task_id === 本卡` → `running`（绿）。
  - 本卡在 `rt.queue_tasks` 中，或角色忙在别的卡且本卡已派给它 → `queued`（橙，等待）。
  - 本卡已提交/评审中 → `submitted`（黄）；已完成/关闭 → `approved`（青）。
  - 其余回退到 `role_claims[].status`。
  - 数据由批量接口 `GET /api/v1/roles/runtime` 提供，随调度 `tick` 每 5 秒刷新。
- 实时会话：`run.status`。
- 任务详情线程/侧栏：`roleLiveFor(slug).dot.cls`（既有点灯状态）。
- 角色会话页：`role-live-dot` 的 `dot.cls`。
- 角色会话总览页：是否有活跃 run。
- 新建任务成员选择：Lobby 传入的活跃状态，缺省为 `idle`。

任务卡状态在调度 `tick` 和 `run` 事件时刷新。mock 验证：角色 X 在 A 卡 `running`（绿），B 卡同时显示 `queued`（橙，等待中）。

`RoleAvatar` 新增 `status` 与 `label` 参数；隐藏姓名时用 `label` 提供可访问名称（角色名 + 状态）。

## 5. 样式架构

- `tokens.css`：设计 token，未分层。
- `base.css` / `pages.css` / `components.css` / `layout.css`：历史样式，统一包在 `@layer legacy { … }` 中。
- `type-scale.css`：未分层，加载在最后。由于未分层样式优先于分层样式，字号/间距覆盖不依赖选择器权重，也不需要 `!important`。
- 新页面/新组件：优先使用 token；需要覆盖历史规则时写入 `type-scale.css` 的对应语义组，不要新增逐页硬编码 px。

### 5.1 自定义几何按钮

`type-scale.css` 里的全局 `button { min-height; padding }` 未分层，优先级高于 legacy 层内的类规则。凡是自带几何的按钮，必须在 `type-scale.css` 中显式恢复原始 `padding/min-height`，否则会被撑开或压扁：

- `.avatar-tile`（头像选择器，必须保持 `padding: 2px`，否则 60×60 的 SVG 会被压成约 31×60）
- `.modal-close`、`.role-detail-avatar`、`.hamburger`、`.drawer-mask`
- `.mention-item`、`.member-row`、`.wf-node`、`.wf-slice-row-btn`、`.rs-row`
- `.rail-btn`、`.compose-at`

新增类似组件时，先检查它是否依赖自身的 padding/min-height；若是，加入这一组。

## 6. 验证

审计脚本对 13 个页面 × 7 个视口（390 / 768 / 1024 / 1440 / 1920 / 2560 / 3840）检查：

- 功能性文字低于下限的数量：**0**（下限按视口取 12 / 14 / 16px）。
- `document.scrollWidth` 均等于视口宽度；不存在页面级横向溢出。
- 横向滚动仅存在于看板（390 宽）和表格容器（窄屏的运行时/活动/治理/提醒页），属于预期交互。
- `npm run build` 通过：68 modules，CSS 98.61 kB / JS 308.25 kB。
