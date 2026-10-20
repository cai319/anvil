# 中国跳棋（2 人 Web 版）测试计划

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 文档状态 | **S5 测试基线**——S6/S8 执行与回归的唯一测试依据 |
| 任务 | S5「测试计划与用例设计（规则/引擎/E2E）」 |
| 父任务 | `28e915e5-6e96-48a0-88d0-b446c1a77e82`「开发一个跳棋小程序」 |
| 上游需求基线 | `docs/prd.md`（v1.0）、`docs/acceptance-criteria.md`（v1.0，AC-01 ~ AC-51） |
| 上游接口基线 | `docs/interface-contract.md`（v1.0）、`docs/design-spec.md`（v1.0） |
| 负责人 | tester |
| 最后更新 | 2026-10-19 |
| 被测对象 | S3 规则引擎 + AI；S4 Web UI / 控制器（与 S3/S4 并行编写，实现完成后执行） |

> **基线说明**
>
> 1. 本计划以 `docs/prd.md` v1.0 与 `docs/acceptance-criteria.md` v1.0 为唯一需求真值，以 `docs/interface-contract.md` v1.0 为唯一接口真值。测试代码中的类型、函数名、错误码、字段名、`data-testid` 均直接取自接口契约，不自行发明。
> 2. 当前 tester 工作区未复制上游基线原文；在工作流工作区中它们分别位于 `../pm/docs/` 与 `../designer/docs/`。集成到项目根目录后，路径恢复为 `docs/prd.md`、`docs/acceptance-criteria.md`、`docs/interface-contract.md`、`docs/design-spec.md`。本计划中的路径均按集成后的项目根目录书写。
> 3. 测试只读取实现，不修改 `src/**`、`package.json`、`vitest.config.ts`、`playwright.config.ts`。测试用例文件位于 `tests/unit/**` 与 `tests/e2e/**`。
> 4. 若实现与契约冲突，先按 PRD > 接口契约 > 实现的顺序判定，并在任务会话中 `@designer` / `@pm` 留下裁定记录；测试不得为迁就实现而静默放宽断言。

---

## 1. 测试目标

1. **规则正确性**：121 孔棋盘、6 方向单步、跳跃/连跳、回合切换、胜负、轮空、300 手上限逐条可验证，非法走子零容忍。
2. **状态机正确性**：`SETUP → HUMAN_TURN / AI_THINKING → GAME_OVER` 的迁移、先后手流程、AI 思考锁定、重开/返回设置与代际竞态全部可复现。
3. **AI 边界**：三档难度只输出合法着法；每步 ≤2000ms；超时/异常/非法输出/无输出均有确定性兜底，绝不卡死；必胜/必挡战术底线成立。
4. **交互与交付**：关键流程（先手完整对局、后手完整对局、连跳、非法提示、重开、终局）在 Chromium 与 WebKit 下可自动化执行；静态交付、零外部请求、测试钩子隔离可验证。
5. **可回归**：每条 AC 至少映射一个自动化或明确的手工/文档检查项；P0 缺陷修复后可按 §10 的回归矩阵快速重跑。

---

## 2. 测试范围

### 2.1 范围内

| 类别 | 内容 |
|---|---|
| 规则引擎 | 棋盘几何与孔位集合、初始局面、合法着法生成、路径校验、走子应用、胜负、轮空、手数上限、状态不变量、夹具加载 |
| AI | 三档难度、合法性、时限、超时/异常/非法输出/无输出降级、连续降级强制简单、必胜、必挡、可注入策略/种子/时钟 |
| 控制器 | 设置、开局、点击分派、选中/连跳草稿、回合结算、轮空循环、AI 调度与看门狗、重开/返回设置、事件顺序、不可变性 |
| UI | 开始界面、121 孔/20 子渲染、选中与合法落点高亮、连跳条、状态区、AI 思考锁定、非法提示、结果弹层、响应式、`data-testid` 稳定性 |
| 交付 | `npm ci / build / test / test:e2e`、静态产物、零外部请求、测试钩子 `?e2e=1` 隔离、README、浏览器兼容（手工） |

### 2.2 范围外

- 3/4/6 人模式、联网对战、账号、排行榜、观战、存档/读档、悔棋、棋谱导出、计时器、音效、国际化、原生 App、服务端。
- 键盘完整对局操作、屏幕阅读器全量适配（仅按 AC-29 做基础检查）。
- 真实低端设备性能基准（仅记录测试机环境与 `chooseMove` 墙钟时间）。
- S1 基线文档本身的评审（AC-51 由 PM/reviewer 负责，本计划只登记为文档检查项）。

---

## 3. 测试环境与工具

### 3.1 环境矩阵

| 项目 | 要求 | 本计划使用 |
|---|---|---|
| 操作系统 | 无强制要求 | macOS（记录实际版本） |
| Node.js | ≥20 LTS | 执行时记录 `node -v` |
| npm | ≥10 | 执行时记录 `npm -v` |
| 构建 | Vite + TypeScript `strict` | S3 提供 |
| 单元测试 | Vitest（Node 环境） | `tests/unit/**/*.test.ts` |
| E2E | Playwright，Chromium + WebKit | `tests/e2e/**/*.spec.ts` |
| 浏览器（手工） | Chrome / Edge / Safari 最近两个大版本 | S6/S8 记录具体版本号 |
| 网络 | 运行时零外部请求 | E2E 请求监听 + 断网冒烟 |

### 3.2 命令

| 目的 | 命令 | 预期 |
|---|---|---|
| 安装 | `npm ci` | 退出码 0；首次 E2E 前 `npx playwright install` |
| 构建 | `npm run build` | 退出码 0；生成 `dist/index.html` 与静态资源 |
| 单元测试 | `npm test` | Vitest `run` 模式，全绿 |
| E2E | `npm run test:e2e` | 构建后启动静态服务，Chromium + WebKit 全绿 |
| 全量 | `npm run test:all` | 单测 + E2E（若脚本存在） |
| 预览 | `npm run preview` | 可访问 `dist/` |

### 3.3 测试运行配置（S3 已提供，S5 复核）

S3 工程脚手架已提供 S5 所需配置，S5 **未修改**这些文件：

| 配置 | 现状 | 对 S5 用例的影响 |
|---|---|---|
| `package.json` | 含 `vitest`、`@playwright/test`、`typescript`；scripts `test`（`vitest run`）、`test:e2e`（`playwright test`）、`build`、`preview`、`test:all` | 直接可用 |
| `vite.config.ts` | `test.environment='node'`，`test.include` 覆盖 `tests/unit/**/*.test.ts` | 单元测试直接可用；用例显式导入 `vitest`，不依赖 `globals: true` |
| `playwright.config.ts` | `testDir='tests/e2e'`；Chromium + WebKit 两个 project；`webServer` 构建并启动 127.0.0.1:4173；失败保留 trace/screenshot；默认超时 45s | 完整对局用例在用例内 `test.setTimeout(240_000)` |
| `tsconfig.json` | `strict`、`moduleResolution: Bundler`、`include: ["src", "vite.config.ts", "playwright.config.ts", "tests"]` | 全部测试纳入类型检查 |

**物理分层映射（重要）**：S3 的物理目录为 `src/engine/**` + `src/types/index.ts`，契约 §0.1 的 `src/core/**` 为逻辑分层名；S3 在 `src/engine/index.ts` 与 `src/types/index.ts` 中已注明并保持公开名称/签名与契约一致。因此：

- 本套单测从 `../../src/engine/index` 导入 `BOARD`、规则/状态函数与全部核心类型；
- AI 用例从 `../../src/ai/index` 导入 `createAiClient` 与 AI 类型；
- 控制器用例仍按契约路径 `../../src/ui/controller` 导入（S4 负责实现该文件）；
- E2E 从 `../../src/types/index` 引用测试钩子类型（编译期擦除，运行时仍为黑盒）。

若后续 S4/designer 要求恢复字面 `src/core` 目录，只需在工程侧增加 re-export 兼容层，S5 用例的断言与字段无需变更（但需同步调整导入路径）。

---

## 4. 测试数据与夹具

### 4.1 标准夹具（来自 `docs/acceptance-criteria.md` 附录 B）

| 夹具 | 用途 | 关键期望（已独立复算） |
|---|---|---|
| **A** 红方一步获胜 | AC-13/14/38、终局展示 | 红方 9/10 占上营，缺 `(-4,5,-1)`；第 10 子在 `(-3,4,-1)`。红方**唯一**获胜着法为 `(-3,4,-1) → (-4,5,-1)`（单步）；蓝方 9/10 无一步获胜；`ply=80` |
| **B** 红方必须挡蓝方一步获胜 | AC-39 | 蓝方 9/10 占下营，缺 `(4,-5,1)`，`(3,-4,1)` 可一步 `→ (4,-5,1)` 获胜；红方无一步获胜；红方**唯一**阻挡着法为 `(4,-4,0) → (4,-5,1)`；执行后蓝方无任何一步获胜着法；`ply=60` |
| **C** 连跳与结束回合 | AC-08/20 | 红 `(0,0,0)` 可首跳 `→ (2,-2,0)`（跨越 `(1,-1,0)`），之后可续跳 `→ (2,0,-2)`（跨越 `(2,-1,-1)`）；两跳路径均合法；首跳后可随时结束；`ply=40` |

### 4.2 新增夹具 D2：获胜即截断连跳（用于 AC-14 后半条）

在夹具 A 的思想上构造：红方 9 子占上营（缺 `(-4,5,-1)`），第 10 子在 **营地外** `(-4,3,1)`；蓝方 8 子占下营，另有 2 子作为跳跃障碍在 `(-4,4,0)` 与 `(-3,4,-1)`。

- 红方 `(-4,3,1)` 跨越 `(-4,4,0)` 跳到 `(-4,5,-1)`：因起点在营地外，落下后红方恰好 10/10，**立即获胜**。
- 从落点 `(-4,5,-1)` 沿 D0 方向还存在几何续跳：跨越 `(-3,4,-1)` 到 `(-2,3,-1)`（该落点为空）。
- 期望：`getLegalMoves` 只输出获胜前缀 `[(-4,5,-1)]`，**不得**输出 `[(-4,5,-1),(-2,3,-1)]`；对后者调用 `applyMove` 返回 `PATH_BEYOND_WIN`。
- 该夹具已在独立参考实现中验证：原始 DFS 存在续跳，而带「获胜即停」的合法着法生成将其正确截断。

### 4.3 辅助夹具

| 夹具 | 用途 | 构造 |
|---|---|---|
| **CENTER** | AC-06 六方向单步 | 红方 9 子占下营（缺 `(1,-5,4)`）+ 红子 `(0,0,0)`；蓝方 10 子占上营；`turn=R`。中心 6 邻居全空 |
| **PLY-299-A** | AC-16 300 手上限 | 与夹具 A 同局面，`ply=299`。非获胜单步 `(-3,4,-1) → (-2,3,-1)` 可验证“300 手平局”；获胜单步验证“胜负优先于手数上限” |

### 4.4 确定性手段

- **AI**：通过 `createAiClient({ strategy, clock, seed })` 注入策略、假时钟与固定种子。超时/异常/非法输出/无输出四条降级路径必须可确定性触发，不真实等待 2 秒（AC-40）。
- **控制器**：通过 `ControllerDeps` 注入 `clock`（`ManualClock`）、`rules`（可返回空着法的包装器）、`ai`（可为延迟/抛错/返回非法着法的假客户端），确定性验证轮空、看门狗与 `gameId` 代际竞态。
- **E2E**：通过 `?e2e=1` 的 `window.__CC_TEST__` 调用 `loadState`（夹具 A/C）、`setAiMode`（`delay`/`timeout`/`illegal`/`no-legal-moves`）与 `getState`/`getLegalMoves`，避免随机性与长等待。
- **种子**：`deriveSeed(gameId, ply, player)`（mulberry32）为默认种子；同一 `gameId + ply + player` 必须可复现。

### 4.5 坐标与夹具格式

- 坐标一律使用立方坐标 `(x, y, z)`，`x + y + z = 0`。
- 夹具 JSON 使用 `FixtureState`：`{ turn: 'R'|'B', ply: 0..299, pieces: [{color, pos:[x,y,z]} × 20] }`，恰好 10 红 + 10 蓝、坐标唯一合法。
- DOM 定位使用 `data-testid="cell-<x>_<y>_<z>"` / `piece-<x>_<y>_<z>`，负号为 ASCII `-`（U+002D）。
- 状态断言优先使用 `window.__CC_TEST__.getState()`，不依赖 DOM 层级；视觉/高亮断言使用 `data-occupant`、`data-target`、`data-selected`。

---

## 5. 测试策略与分层

### 5.1 分层模型

```
L1 规则引擎（纯函数，Node）      tests/unit/board.test.ts, rules.test.ts, game.test.ts
L2 AI（策略/时钟可注入，Node）    tests/unit/ai.test.ts
L3 控制器（DOM-free 状态机，Node） tests/unit/controller.test.ts
L4 E2E（浏览器黑盒 + 测试钩子）    tests/e2e/*.spec.ts
L5 手工冒烟（三浏览器）            S6/S8 记录
```

依赖方向只允许向下：L4 依赖公开 DOM 契约与测试钩子；L3 只注入 `RulesApi`/`AiClient`/`Clock`；L1/L2 不依赖 DOM、浏览器与网络。

### 5.2 单元测试设计原则

- **纯函数、无 DOM**：直接导入逻辑核心层（S3 物理路径为 `src/engine/index`，类型在 `src/types/index`）与 `src/ai/*`；每个用例只断言输入/输出与不可变性，不 mock 规则。
- **状态快照对比**：非法操作前后比较 `gameId/phase/turn/ply/pieces/consecutivePasses/result/lastMove`，确保“拒绝且状态不变”。
- **边界优先**：9/10 与 10/10、跳跃中点为空/落点被占、连跳重复落点、获胜后继续跳、300 手边界、双方连续轮空、`ply=299` 的胜负优先。
- **确定性**：AI 断言不依赖真实时间；时限用例记录实际墙钟但只用 2000ms 硬上限断言；超时用例使用假时钟。

### 5.3 控制器测试设计原则

- 用 `createController({ ai, clock, rules, aiMinDelayMs: 0 })` 构造无 DOM 状态机。
- `ManualClock.advance(ms)` 推进 AI 最短延迟与看门狗；`flush()` 清微任务后断言快照。
- 事件用 `subscribe` 捕获，断言关键事件存在与顺序（`move-applied` → `turn-changed` → `ai-thinking` 等），不依赖动画。
- 竞态用例：旧 AI Promise 在 `restart()/backToSetup()` 后 resolve，断言 `gameId` 不匹配被丢弃、旧着法绝不落到新局。
- 轮空用例：包装 `RulesApi.getLegalMoves` 对指定回合返回 `[]`，并把空列表传给真实 `passTurn`，验证轮空循环与连续轮空平局。

### 5.4 E2E 测试设计原则

- 入口统一 `/?e2e=1`（除测试钩子隔离用例外），先等待 `window.__CC_TEST__` 可用。
- 关键元素一律 `getByTestId`；坐标辅助函数生成 `cell-x_y_z`；禁止依赖 DOM 层级与文本标签定位。
- 每例结束前断言 `getState()` 与 DOM 一致；AI 用 `setAiMode` 控制时长，完整对局用 `delay` 模式加速，但仍由真实规则/AI 落子。
- 完整对局循环：读取 `getLegalMoves()` → 点击 origin → 逐跳点击 `path` → 若 `end-turn` 仍可见则点击 → 等待 `ply` 增加/`phase` 变化，直到 `GAME_OVER` 或达到防护上限。
- 计时断言：后手开局从 `start-game` 点击到 `ply=1` ≤2000ms；普通 AI 回合从人类落子到 `ply+2` ≤2000ms（若浏览器抖动导致偶发失败，记录环境并按 2000ms 硬上限复核，不得放宽验收值）。
- 失败保留 Playwright trace/screenshot（配置由 S3 提供）。

### 5.5 手工冒烟

三浏览器（Chrome/Edge/Safari 最近两个大版本）各完成：先手一局、后手开局并确认 AI 首手、一次连跳并主动结束、一次非法落点提示、终局与重开。记录浏览器版本、日期、截图/日志（AC-50）。

---

## 6. 用例清单

### 6.1 单元测试（`tests/unit/`）

#### `fixtures.ts`（共享数据，非测试文件）

导出夹具 A/B/C/D2/CENTER/PLY-299-A、坐标工具与 `expectOk` 等辅助函数；所有期望值均已在独立参考实现中复算。

#### `board.test.ts`

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| UT-BOARD-01 | `BOARD.cells` 121、`centerCells` 61、6 营地各 10、两两不相交、并集等于 `cells` | AC-01 |
| UT-BOARD-02 | 6 方向向量与顺序；`q=p+d ⇒ p=q−d` 双向邻接；邻居均在棋盘内 | AC-01 |
| UT-BOARD-03 | 上营 `C+`/下营 `C-` 孔位清单与 PRD §4.1.5 逐项一致 | AC-01 |
| UT-BOARD-04 | `createInitialState`：20 子、10R/10B、R 在下营、B 在上营、`turn=R`、`ply=0`、`phase=PLAYING`、`result=null` | AC-02 |
| UT-BOARD-05 | 初始合法着法恰好 14（8 单步 + 6 跳跃）；无重复 `(piece,path)`；无路径重复落点 | AC-02 |
| UT-COORD-01 | `toCoord/toTuple/coordKey/parseCoordKey/isCell/getPieceAt` 正常与非法输入 | AC-01 |
| UT-FIX-01 | 夹具 A/B/C/D2 均可加载为合法 20 子状态，10/10、坐标唯一 | AC-01/02 |
| UT-FIX-02 | 19 子、11 红、重复坐标、越界、非法 `turn`、`ply=300`、已 10/10 的夹具均返回 `INVALID_FIXTURE` 且不抛异常 | AC-01/02 |

#### `rules.test.ts`

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| UT-RULE-01 | CENTER 夹具：`(0,0,0)` 六个方向各有且仅有 1 个合法单步；逐方向执行后 origin 空、target 有子、总子数 20、`ply+1`、回合切换 | AC-06 |
| UT-RULE-02 | 初始红方单步恰好 8；`(1,-5,4)→(1,-4,3)`、`(1,-5,4)→(0,-4,4)` 合法 | AC-06 |
| UT-RULE-03 | 单步反例：不相邻 `NOT_ADJACENT`、目标被占 `OCCUPIED`、目标越界 `OUT_OF_BOARD`；状态不变 | AC-06/11 |
| UT-RULE-04 | 初始 `(4,-6,2)→(4,-4,0)` 跳跃合法；中点 `(4,-5,1)` 仍在且为蓝；总子数 20；`ply+1`；回合切换 | AC-07 |
| UT-RULE-05 | 跳跃反例：中点为空 `JUMP_NO_PIECE`、落点被占 `JUMP_LANDING_OCCUPIED`、落点越界 `OUT_OF_BOARD`；状态不变 | AC-07/11 |
| UT-RULE-06 | 初始红方跳跃恰好 6，且全部为单跳前缀 | AC-07 |
| UT-RULE-07 | 夹具 C：`(0,0,0)` 首跳 `→(2,-2,0)` 合法；`queryMoveOptions` 进入 `jump` 态，`jumps=[(2,0,-2)]`、`canEndTurn=true`；query 不修改引擎状态（`ply` 不变） | AC-08/18/20 |
| UT-RULE-08 | 夹具 C：首跳后停止（完整路径 `[(2,-2,0)]`）→ `ply+1`、回合切蓝、棋子停在 `(2,-2,0)` | AC-08/09 |
| UT-RULE-09 | 夹具 C：两跳 `[(2,-2,0),(2,0,-2)]` → 合法、`ply+1`、回合切蓝、棋子停在 `(2,0,-2)` | AC-08 |
| UT-RULE-10 | 连跳重复落点：夹具 C 反向跳回起点 `(0,0,0)` → `PATH_REPEAT`；`getLegalMoves` 不含任何重复坐标路径 | AC-09 |
| UT-RULE-11 | 动作类型：跳跃后接单步 → `MIXED_MOVE`；`kind='step'` 但路径长度 2 → `MIXED_MOVE/ILLEGAL_MOVE`；跳跃中被视为单步的段 → 拒绝 | AC-10/11 |
| UT-RULE-12 | 非法走子矩阵：空格 `EMPTY_CELL`、对方棋子 `NOT_YOUR_PIECE`、非当前回合、越界、占位、路径不连续/缺段；逐项断言错误码与完整状态快照不变 | AC-11 |
| UT-RULE-13 | 终局后调用 `applyMove` → `GAME_OVER`，棋子/手数/结果不变 | AC-11/17 |
| UT-RULE-14 | `isLegalMove`：合法着法 true；畸形 move（空 path、越界、kind 不匹配）false 且不抛异常 | AC-11 |
| UT-RULE-15 | `queryMoveOptions`：select/jump 态字段（steps/jumps/canEndTurn/winningIfEndTurn）；无合法着法的棋子返回全空结果而非错误；`pathSoFar` 非法前缀的错误码 | AC-08/19 |
| UT-RULE-16 | `queryMoveOptions` 错误：空格/对方棋子/终局/重复落点/路径断裂 | AC-11/19 |
| UT-RULE-17 | 夹具 A：`countInTargetCamp(R)=9`、未获胜；执行唯一获胜着法 → `FINISHED`、`result={R,'goal'}`、`ply=81`、`turn=B`；9/10 不判胜 | AC-13/14 |
| UT-RULE-18 | 夹具 D2：合法着法只含获胜前缀；对续跳路径 `applyMove` → `PATH_BEYOND_WIN`；执行获胜跳立即终局，后续不执行 | AC-14 |
| UT-RULE-19 | 蓝方对称获胜：夹具 B 改为 `turn=B`，`(3,-4,1)→(4,-5,1)` 使蓝方 10/10 获胜 | AC-13 |
| UT-RULE-20 | `passTurn`：初始局面（有合法着法）→ `PASS_NOT_ALLOWED`；注入空着法 → `ply+1`、回合切换、`consecutivePasses=1`、`lastMove` 不变 | AC-15 |
| UT-RULE-21 | 双方连续轮空：第二次 `passTurn` → `FINISHED`、`result={'draw','both-pass'}`、不再走子 | AC-15 |
| UT-RULE-22 | 300 手：`ply=299` 非获胜着法 → `draw/ply-limit`；`ply=299` 获胜着法 → 胜（胜负优先）；`ply=299` 轮空 → `draw/ply-limit` | AC-16 |
| UT-RULE-23 | 回合切换：单步一次 `ply+1`；一次两跳连跳只 `ply+1`；终局后不再切换；非当前方走子被拒 | AC-10 |

#### `game.test.ts`

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| UT-GAME-01 | 初始 `pieces` 规范顺序（R 前 B 后、同色按 `(x,y,z)` 升序）；`gameId` 透传 | AC-02 |
| UT-GAME-02 | `createStateFromFixture` 正常路径：字段、`phase=PLAYING`、`result=null`、`lastMove` 透传 | AC-02 |
| UT-GAME-03 | 夹具非法矩阵（数量/颜色/重复/越界/回合/ply/已终局）均 `INVALID_FIXTURE` | AC-02 |
| UT-GAME-04 | `validateState`：合法状态 `ok`；篡改子数/重复坐标/终局不一致 → `INVALID_STATE`；不修改入参 | AC-02/11 |
| UT-GAME-05 | `getProgress`/`countInTargetCamp`：初始 0/0；夹具 A 9/9；夹具 B R0/B9；获胜后 10/x | AC-13/21 |
| UT-GAME-06 | 不可变性：`Object.freeze` 状态下 `applyMove/getLegalMoves/queryMoveOptions/validateState` 正常工作且不修改入参 | AC-11 |
| UT-GAME-07 | 连续走子后 `gameId` 不变；`lastMove` 字段（origin/path/kind/to/plyBefore/plyAfter/player）正确 | AC-10 |

#### `ai.test.ts`

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| UT-AI-01 | 三档难度均可创建/调用；`DEFAULT_SETUP_CONFIG` 为普通 + 我先手 | AC-30 |
| UT-AI-02 | 每档难度连续 100 手 AI-vs-AI：每个 `move` 都属于 `getLegalMoves`，`applyMove` 成功，棋子恒 10/10；到终局则新开一局继续 | AC-31 |
| UT-AI-03 | 3 档 × 3 个代表局面（初始/夹具 B/夹具 A）：每次 `chooseMove` 墙钟 ≤2000ms；记录最大/均值，不抛异常 | AC-32 |
| UT-AI-04 | 假时钟 + 永不返回策略：在预算内 resolve，`degraded=true`、`reason='timeout'`，兜底着法合法 | AC-33/40 |
| UT-AI-05 | 策略同步 throw / reject：`chooseMove` 不 reject，`degraded=true`、`reason='exception'`；有合法兜底或 `bestSoFar` | AC-34/40 |
| UT-AI-06 | 非法输出 / 无输出策略：注入被原样使用（AI 客户端不信任校验由控制器完成）；`move=null` 不导致 reject | AC-35/40 |
| UT-AI-07 | 夹具 A：三档难度均返回获胜着法；应用后立即 `GAME_OVER` 红胜 | AC-38 |
| UT-AI-08 | 夹具 B：普通/困难返回能消除蓝方全部一步获胜的着法（用“阻挡着法集合”断言，不硬编码唯一性）；简单档不强制 | AC-39 |
| UT-AI-09 | 确定性：同一局面 + 同一种子 + 同一难度重复调用返回相同着法；不同 `gameId/ply/player` 派生不同种子 | AC-40 |
| UT-AI-10 | 不可变性：`chooseMove` 前后 `GameState` 快照完全一致；不修改 `BOARD` | AC-31/40 |

#### `controller.test.ts`

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| UT-CTRL-01 | `startGame({humanSide:'first'})`：`HUMAN_TURN`、human=R、turn=R、ply=0；`match-start` + `turn-changed` | AC-04 |
| UT-CTRL-02 | `startGame({humanSide:'second'})`：`AI_THINKING`、human=B、turn=R；推进时钟后 AI 自动落子、ply=1、`HUMAN_TURN` | AC-05/41 |
| UT-CTRL-03 | 人类单步后 `ply+1`、`AI_THINKING`；AI 落子后 `ply+2`、`HUMAN_TURN`；事件顺序 `move-applied → turn-changed → ai-thinking` | AC-04/10 |
| UT-CTRL-04 | 点击矩阵：空格 `EMPTY_CELL`、对方棋子 `NOT_YOUR_PIECE`、己方有走法棋子选中、再次点击取消、点击另一己方棋子切换 | AC-12/19 |
| UT-CTRL-05 | 选中后快照 `selection.steps/jumps` 与 `getLegalMoves` 派生的单步/首跳集合完全一致 | AC-19 |
| UT-CTRL-06 | 选中后点击非高亮孔位 → `ILLEGAL_MOVE`，选中保持、局面/手数不变；点击己方无走法棋子 → `ILLEGAL_MOVE`，保持原选中 | AC-12/19 |
| UT-CTRL-07 | `AI_THINKING` 下 `handleCellClick`/`endTurn` → `AI_BUSY`（silent），快照不变；`GAME_OVER` 下 → `GAME_OVER` | AC-11/22 |
| UT-CTRL-08 | 注入 `rules.getLegalMoves` 对 B 回合返回 `[]`：人类落子后自动轮空，`pass` 事件、`ply+1`、回到 R 的 `HUMAN_TURN`，不调用 AI | AC-15/36 |
| UT-CTRL-09 | 连续两次注入轮空 → `GAME_OVER`、`result={'draw','both-pass'}`、`game-over` 事件 | AC-15 |
| UT-CTRL-10 | `restart()`：`gameId+1`、初始 20 子、`ply=0`、`lastMove=null`、按当前先后手进入 `HUMAN_TURN/AI_THINKING` | AC-24 |
| UT-CTRL-11 | `backToSetup()`：`phase=SETUP`、`board=null`、`config` 保留、`gameId` 递增；SETUP 下 `restart` → `WRONG_PHASE` | AC-25 |
| UT-CTRL-12 | AI 思考中 `restart()`：`gameId+1`；旧 AI Promise 随后 resolve 被丢弃，新局 `ply/棋子` 不变；无 `move-applied` 旧事件 | AC-26 |
| UT-CTRL-13 | 看门狗：AI 永不返回，`ManualClock` 推进到 1950ms → 兜底合法着法应用、`ply+1`、`streak=1`、`console.warn`，不卡死 | AC-33 |
| UT-CTRL-14 | AI 返回非法着法：控制器拒绝并改用合法兜底，`ply+1`、`streak=1`、棋盘无非法落子 | AC-35 |
| UT-CTRL-15 | AI reject/throw：控制器捕获并兜底，`streak=1`，页面无未捕获异常 | AC-34 |
| UT-CTRL-16 | 连续 3 次降级 → `forcedEasy=true`、`effectiveDifficulty='easy'`、`ai-degraded` 仅发一次；正常着法后 `streak=0` | AC-37 |
| UT-CTRL-17 | `setSetupConfig` 仅 SETUP 可用；非法难度/先后手 → `INVALID_CONFIG`；合法变更发 `setup-config-changed` | AC-03/30 |
| UT-CTRL-18 | 订阅/取消订阅、监听器异常不影响状态机；`destroy()` 清定时器与监听、后续调用 no-op/`WRONG_PHASE` | AC-22/26 |
| UT-CTRL-19 | `checkDeadlines()` 幂等：后台恢复后超过看门狗只兜底一次，不重复落子 | AC-33 |

### 6.2 E2E 测试（`tests/e2e/`）

#### `helpers.ts`（共享辅助，非测试文件）

- `openApp(page, {e2e:true})`、`waitForHook`、`clickCell`、`snapshot`、`getLegalMoves`、`loadFixture`、`setAiMode`、`waitForPhase`、`playFullGame`、`expectInitialBoard`、`isSelected`。
- 本地声明 `CcTestHook` 的最小类型（黑盒消费测试钩子），不导入 `src/**` 内部类型；坐标统一 `[x,y,z]`。

#### `first-player.spec.ts`（先手完整对局）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-SETUP-01 | `/?e2e=1`：`setup-panel` 可见；3 个难度、2 个先后手选项存在；默认「普通」「我先手」；点击切换后选中态生效 | AC-03/28/30 |
| E2E-FIRST-01 | 选「我先手」开始：`setup-panel` 消失、`board` 可见；恰好 121 个 `cell-*`、20 个 `piece-*`；`board-svg` 存在；`turn-text=你的回合 · 红方`、`move-count=0`、`progress-red/blue=0/10`；`board[data-phase=HUMAN_TURN]` | AC-04/18/21/28 |
| E2E-FIRST-02 | 点击红子 `(1,-5,4)`：选中态；高亮集合与 `getLegalMoves(piece)` 派生的单步/首跳集合完全一致；点击 `(1,-4,3)` 单步 → `move-count=1`、`AI 思考中`；等待 AI → `move-count=2`、`你的回合 · 红方`；从人类落子到 AI 落子 ≤2000ms | AC-04/06/19/21/32 |
| E2E-FIRST-03 | `setAiMode('delay',{delayMs:0})` + 先手：`playFullGame` 循环点击合法着法直到 `GAME_OVER`（防护上限 300 手）；结果标题∈{红方获胜,蓝方获胜,平局}；棋盘锁定；`result-restart-button` 重开成功 | AC-13/14/17/24/49 |
| E2E-FIRST-04 | 通过 `loadState(夹具 A)` 加载红方一步获胜局面：点击 `(-3,4,-1)` 再点 `(-4,5,-1)`；`game-result-title=红方获胜`、`game-result-reason=占满对方营地 10 孔`、`move-count=81`；终局后点击棋盘无变化 | AC-14/17 |
| E2E-FIRST-05 | 初始 20 个 `piece-*` 的 testid 与 `getState().pieces` 一一对应；抽查 `cell-1_-5_4` 存在，验证 ASCII 负号/下划线 | AC-18/28 |

#### `second-player.spec.ts`（后手完整对局）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-SECOND-01 | 选「我后手」开始：立即 `AI_THINKING`、`ai-thinking` 可见、`turn-text` 含「AI 思考中 · 红方」；思考期间点击任意格子后快照不变（`ply=0`）；等待 `ply=1` 的耗时 ≤2000ms；恰好 1 枚红子离开初始位置；随后 `HUMAN_TURN`、`turn-text=你的回合 · 蓝方`；人类合法走子后 `ply=2`、再次 `AI_THINKING` | AC-05/22/41 |
| E2E-SECOND-02 | `setAiMode('delay',{delayMs:0})` + 后手：等待 AI 首手后 `playFullGame` 直到 `GAME_OVER`；结果弹层与重开可用 | AC-05/49 |

#### `jump-chain.spec.ts`（连跳 UI）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-JUMP-01 | `loadState(夹具 C)`；点击 `(0,0,0)`；点击 `(2,-2,0)`：`jump-bar`/`end-turn` 可见、`jump-status` 为「连跳中 · 已跳 1 跳」、`move-count` 仍 40；点击 `end-turn` → `move-count=41`、棋子停在 `(2,-2,0)` | AC-08/20 |
| E2E-JUMP-02 | 重新加载夹具 C；点击 `(0,0,0)` → `(2,-2,0)` → `(2,0,-2)`：无后续跳自动结束、`move-count=41`、`jump-bar` 隐藏、棋子停在 `(2,0,-2)` | AC-08/20 |
| E2E-JUMP-03 | 夹具 C 连跳中点击其他己方棋子或非高亮孔位：提示「连跳中只能继续跳跃或结束回合」、`selection`/棋子/手数不变 | AC-10/11/20 |

#### `ai-thinking.spec.ts`（AI 思考态禁止落子）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-LOCK-01 | `setAiMode('delay',{delayMs:1200})` + 先手；人类落子后进入 `AI_THINKING`；连续点击己方/对方/空格；等待 200ms 后快照与点击前一致（棋子、`ply`、`selection=null`）；`ai-thinking` 可见；随后恢复 `HUMAN_TURN`，`ply` 只增加 1 | AC-22 |
| E2E-LOCK-02 | `AI_THINKING` 时强制点击隐藏的 `end-turn`：状态不变、无 `move-applied`；`board[aria-busy=true]` | AC-22 |

#### `illegal-move.spec.ts`（非法操作提示）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-ILLEGAL-01 | 先手开局；点击空中心 `(0,0,0)`：`hint` 出现（`不可落子`/`请选择一枚棋子`）、无 alert/confirm、状态不变；约 2s 内提示消失；点击蓝子 `(-4,8,-4)`：提示「请选择己方棋子」、状态不变 | AC-12/23/29 |
| E2E-ILLEGAL-02 | 选中红子 `(1,-5,4)` 后点击非高亮空位 `(0,0,0)`：提示「不可落子」、选中保持、棋子/手数不变 | AC-19/23 |
| E2E-ILLEGAL-03 | 非法点击前后 `getState()` 深比较（pieces/ply/turn/selection/lastMove）；随后执行合法着法仍成功，证明 UI 未被污染 | AC-11/23 |

#### `restart.spec.ts`（重开流程）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-RESTART-01 | 先手开局走 1 手并等 AI 落子；点击 `restart-button`：`board[data-game-id]` 严格 +1、`move-count=0`、初始 10+10 布局、`HUMAN_TURN`、`lastMove=null` | AC-24 |
| E2E-RESTART-02 | `setAiMode('delay',{delayMs:1500})`；人类落子后 `AI_THINKING`，立即点击 `restart-button`：新 `gameId`、`ply=0`；等待 2000ms 后仍 `ply=0`、初始布局，旧 AI 结果未落入新局 | AC-26 |
| E2E-RESTART-03 | 选择「困难 + 我后手」开局后点击 `back-to-setup`：回到 `setup-panel`、无 `board`；不改选项再次开始，`config` 仍为 `hard/second`、`AI_THINKING`、人类执蓝 | AC-25 |
| E2E-RESTART-04 | 夹具 A 终局后：`game-result` 可见、标题正确；HUD 重开被弹层拦截；点击 `result-restart-button` → 新局 `ply=0`、初始布局、按原先后手开始 | AC-17/24 |
| E2E-RESTART-05 | `AI_THINKING` 中 `back-to-setup`：回到 SETUP；旧 AI 晚到不产生任何落子；再次开始不继承旧局面 | AC-25/26 |

#### `test-hook.spec.ts`（测试钩子隔离）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-HOOK-01 | 不带 `?e2e=1` 加载：`window.__CC_TEST__` 为 `undefined` | AC-46 |
| E2E-HOOK-02 | `?e2e=1` 加载：钩子存在且 `version='1.0'`；`getState()` 返回快照；`loadState` 19 子夹具返回 `ok:false, code=INVALID_FIXTURE` 且当前状态不变 | AC-46 |

#### `network.spec.ts`（零外部请求）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-NET-01 | 监听全部请求；开局、走子、AI 落子、重开；断言所有请求 URL 与页面同源，无 CDN/远程字体/后端 API | AC-43 |

#### `responsive.spec.ts`（响应式）

| 用例 ID | 内容 | 覆盖 |
|---|---|---|
| E2E-RESP-01 | 1280×720 与 360×640：棋盘完整可见、无横向滚动（`scrollWidth ≤ innerWidth+1`）、状态/HUD 按钮可见可点 | AC-27 |

### 6.3 用例与接口契约字段对齐表

| 测试使用 | 契约来源 | 字段/签名 |
|---|---|---|
| 棋盘/营地/方向 | `docs/interface-contract.md` §2.1 | `BOARD.cells/centerCells/camps/targetCamp/directions/initialPieces` |
| 状态 | §2.2 | `GameState{gameId,phase,turn,ply,pieces,consecutivePasses,result,lastMove}` |
| 着法 | §2.1/§4.3 | `LegalMove{piece,path,kind}`；`step` 路径长度 1，`jump` ≥1 |
| 规则 API | §4.1 | `getLegalMoves/isLegalMove/queryMoveOptions/applyMove/passTurn`；`Result<T>` |
| 错误 | §3.1/§3.2 | `EngineErrorCode` 枚举与 `display` 语义；测试只断言 `code`，不断言 `detail` |
| 快照 | §5.2 | `GameSnapshot/UiAppliedMove/SelectionState/AiInfo` |
| 控制器 | §5.3/§5.5 | `createController/startGame/restart/backToSetup/handleCellClick/endTurn/waitForIdle` |
| AI | §6.1/§6.3 | `createAiClient/chooseMove/AiMoveRequest/AiMoveResponse`；`AI_HARD_LIMIT_MS=2000` |
| 测试钩子 | §8.2 | `window.__CC_TEST__` 的 `loadState/getState/getLegalMoves/setAiMode/waitForIdle/reset` |
| DOM | §9 + design-spec §10 | `data-testid` 与 `data-occupant/data-target/data-selected/data-phase/data-game-id` |

---

## 7. 验收标准逐条映射（AC-01 ~ AC-51）

> 说明：`类型` 中 U = Vitest 单元/控制器，E = Playwright，M = 手工，D = 文档/命令检查。P0/P1/P2 沿用 `docs/acceptance-criteria.md`。

| AC | P | 覆盖用例 | 类型 | 预期结果摘要 |
|---|---|---|---|---|
| AC-01 棋盘结构 | P0 | UT-BOARD-01/02/03, UT-COORD-01, UT-FIX-01/02 | U | 121 孔、中心 61、6×10、无交集；方向双向；上下营清单逐项一致 |
| AC-02 初始局面 | P0 | UT-BOARD-04/05, UT-GAME-01/02 | U | 20 子 10/10；R 先；ply=0；初始 14 个合法着法（8 单步+6 跳） |
| AC-03 开始界面 | P0 | E2E-SETUP-01, UT-CTRL-17 | E/U | 3 难度 + 2 先后手，默认普通/我先手；切换生效 |
| AC-04 人类先手流程 | P0 | UT-CTRL-01/03, E2E-FIRST-01/02 | U/E | 开始即 HUMAN_TURN、ply=0；人类走子后 AI 思考并落子，ply=2 |
| AC-05 人类后手/AI 首手 | P0 | UT-CTRL-02, E2E-SECOND-01 | U/E | 开始即 AI_THINKING；≤2s 自动首手 ply=1；人类执蓝 |
| AC-06 六方向单步 | P0 | UT-RULE-01/02/03 | U | 6 方向逐一验证；初始 8 单步；非法步拒绝 |
| AC-07 跳跃与不吃子 | P0 | UT-RULE-04/05/06 | U | 跳跃合法、中点保留、20 子不变；初始 6 跳；反例拒绝 |
| AC-08 连跳/转向/停止 | P0 | UT-RULE-07/08/09, E2E-JUMP-01/02 | U/E | 首跳后可停止或续跳；两路径均 ply+1 |
| AC-09 连跳不得重复落点 | P0 | UT-RULE-10 | U | 回到已访问棋位被拒；合法列表无重复路径 |
| AC-10 动作类型/回合切换 | P0 | UT-RULE-11/23, UT-CTRL-03 | U | 跳跃后不能改单步；一次连跳 1 手；回合切换一次 |
| AC-11 非法走子拒绝 | P0 | UT-RULE-12/13/14, UT-CTRL-07, E2E-ILLEGAL-03 | U/E | 错误码正确；状态快照逐项不变；无部分移动 |
| AC-12 行动方/可选棋子 | P0 | UT-CTRL-04/06, E2E-ILLEGAL-01/02 | U/E | 对方子/空格/无走法棋子不可选；AI/终局点击无变化 |
| AC-13 胜负 10/10 与 9/10 | P0 | UT-RULE-17/19, UT-GAME-05 | U | 9/10 不胜；10/10 立即胜；双方对称 |
| AC-14 最后一子进营立即胜 | P0 | UT-RULE-18, E2E-FIRST-04 | U/E | 获胜跳立即终局；获胜后的续跳被截断/拒绝 |
| AC-15 轮空/连续轮空 | P0 | UT-RULE-20/21, UT-CTRL-08/09 | U | 无着法自动轮空 ply+1；连续两轮空平局；有着法不得主动轮空 |
| AC-16 300 手平局 | P0 | UT-RULE-22 | U | 第 300 手非胜 → 平局；获胜优先；轮空/连跳各计 1 手 |
| AC-17 终局封盘/展示 | P0 | E2E-FIRST-03/04, E2E-RESTART-04, UT-RULE-13 | E/U | 结果弹层文案正确；棋盘锁定；仅重开/返回可用 |
| AC-18 棋盘渲染 | P0 | E2E-FIRST-01 | E | 121 孔、20 子、SVG、上下营可辨识、桌面完整可见 |
| AC-19 选子/高亮 | P0 | E2E-FIRST-02, UT-CTRL-05/06 | E/U | 高亮集合与引擎一致；切换选中清除旧高亮 |
| AC-20 连跳 UI | P0 | E2E-JUMP-01/02/03, UT-RULE-07 | E/U | 连跳条/结束回合；自动结束；禁止切换 |
| AC-21 状态区 | P0 | E2E-FIRST-01/02, E2E-SECOND-01 | E | 回合/手数/双方进营数实时正确 |
| AC-22 AI 思考锁定 | P0 | E2E-LOCK-01/02, UT-CTRL-07 | E/U | 显示思考中；点击全部忽略；落子后恢复 |
| AC-23 非法点击提示 | P1 | E2E-ILLEGAL-01/02/03 | E | 非阻塞中文提示、约 2s 消失、无 alert/confirm |
| AC-24 重新开始 | P0 | E2E-RESTART-01, UT-CTRL-10 | E/U | gameId+1、初始布局、ply=0、沿用设置 |
| AC-25 返回设置 | P1 | E2E-RESTART-03/05, UT-CTRL-11 | E/U | 回 SETUP、保留配置、不自动开局、中止 AI |
| AC-26 AI 思考中重开/返回 | P0 | E2E-RESTART-02/05, UT-CTRL-12 | E/U | 旧 AI 结果丢弃；新局不被污染；无卡死 |
| AC-27 响应式 | P1 | E2E-RESP-01 | E | 1280×720 与 360×640 完整可见、无横向滚动 |
| AC-28 稳定选择器 | P1 | E2E-SETUP-01 及全部 E2E；E2E-HOOK-02 | E | 契约 testid 全部存在且唯一、跨局稳定 |
| AC-29 中文文案/可访问性 | P2 | E2E-ILLEGAL-01, MT-01, D-README | E/M/D | 中文文案、系统字体、焦点可见、`aria-live`、非仅颜色 |
| AC-30 三档难度/默认 | P0 | UT-AI-01/02, E2E-SETUP-01 | U/E | 三档可玩；默认普通；不降低合法性/时限/必胜 |
| AC-31 AI 只产生合法着法 | P0 | UT-AI-02 | U | 100 手/档位全部合法；10/10 恒成立；300 手内终止 |
| AC-32 AI 每步 ≤2000ms | P0 | UT-AI-03 | U | 3 档 × 3 局面每次 ≤2000ms；记录最大耗时 |
| AC-33 AI 超时降级 | P0 | UT-AI-04, UT-CTRL-13 | U | 及时返回合法兜底；ply+1；console.warn；不卡死 |
| AC-34 AI 异常降级 | P0 | UT-AI-05, UT-CTRL-15 | U | 捕获异常并兜底；无未捕获异常 |
| AC-35 AI 非法输出拒绝 | P0 | UT-AI-06, UT-CTRL-14 | U | 控制器二次校验拒绝；合法兜底；streak+1 |
| AC-36 AI 无合法着法轮空 | P0 | UT-CTRL-08 | U | 不落子、自动轮空、ply+1、提示；连续轮空平局 |
| AC-37 连续降级切简单 | P1 | UT-CTRL-16 | U | 第 3 次连续降级 forcedEasy；正常后清零；提示非阻塞 |
| AC-38 AI 一步获胜 | P1 | UT-AI-07 | U | 三档均选唯一获胜着法（夹具 A） |
| AC-39 AI 必挡 | P1 | UT-AI-08 | U | 普通/困难选择唯一阻挡着法（夹具 B）；简单不强制 |
| AC-40 可注入策略/种子/时钟 | P1 | UT-AI-04/05/06/09 | U | 四类降级确定性触发；同种子可复现；无全局随机/时钟 |
| AC-41 AI 先手首手时限 | P0 | E2E-SECOND-01, UT-CTRL-02 | E/U | ≤2000ms；恰好一枚红子移动；ply=1；思考期锁定 |
| AC-42 技术栈/脚本 | P0 | S6 执行记录（`npm ci/build/test/test:e2e`） | D | 命令退出码 0；生成 dist；Vitest+Playwright |
| AC-43 静态/零外部请求 | P0 | E2E-NET-01 | E | 全部请求同源；无 CDN/后端；断网可玩 |
| AC-44 浏览器兼容 | P0 | Playwright chromium+webkit（S3 配置）+ MT-02 | E/M | 两项目全绿；三浏览器手工冒烟无阻断 |
| AC-45 目录分层 | P1 | 代码检查 + UT-* 在 Node 导入 engine/ai | U/D | 逻辑分层 core/ai/ui 可用（S3 物理映射为 src/engine + src/types）；规则层无 DOM；tests/unit、tests/e2e 存在 |
| AC-46 测试钩子 | P1 | E2E-HOOK-01/02 | E | 无 `?e2e=1` 不存在；有则可 loadState/getState/setAiMode |
| AC-47 README | P1 | 文档检查（S6/PM） | D | 安装/命令/规则/操作/AI/浏览器/限制齐全 |
| AC-48 单元测试全绿 | P0 | `npm test` 执行记录 | D | 全部通过；无 P0 skip；Node 无浏览器/网络 |
| AC-49 E2E 全绿 | P0 | `npm run test:e2e` 执行记录 | D | Chromium+WebKit 全通过；失败有 trace/截图 |
| AC-50 三浏览器手工冒烟 | P1 | MT-01/MT-02 | M | Chrome/Edge/Safari 各完成指定四步；无阻断 |
| AC-51 S1 交付基线 | P0 | PM/reviewer 文档检查（非 S5 执行） | D | 仅改 S1 范围文档；FR 41 条全部映射；无未决 P0 歧义 |

---

## 8. 通过标准与出口准则

### 8.1 单条用例通过标准

- 断言全部通过；无 `skip`/`todo`（P0 用例禁止跳过）。
- 失败时保留可复现证据：命令、夹具 ID、种子、`gameId/ply`、截图/trace、日志。

### 8.2 阶段出口（S6/S8）

1. `npm test` 与 `npm run test:e2e` 退出码 0；Chromium + WebKit 全部通过，无未解释的 flaky/重试。
2. AC-01 ~ AC-51 中全部 **P0** 通过；P1 通过或由 PM/人类书面接受；P2 由 PM 决定。
3. 无未关闭的 P0/P1 缺陷；无 AI 卡死、非法落子、胜负误判、无法开始对局。
4. 三浏览器手工冒烟完成并记录（AC-50）。
5. 测试报告（S6 `docs/test-report.md`、S8 `docs/acceptance-report.md`）逐条引用 AC 编号与证据。

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| S3/S4 与 S5 并行，接口尚未实现 | 用例暂时无法执行 | 严格按 `docs/interface-contract.md` 编写；S6 首次执行时先跑 `npm test` 暴露契约偏差，再 `@developer` 修复 |
| 测试运行配置缺失或缺项目根目录 | 无法直接运行 | 本计划 §3.3 已记录并 `@developer`；S3 脚手架必须提供 vitest/playwright 配置 |
| AI 真实计时受机器负载抖动 | 偶发超时失败 | 超时逻辑用假时钟确定性验证；墙钟用例只断言 2000ms 硬上限，记录环境与 max/p95；禁止放宽验收值 |
| `delay` 模式/测试钩子导致“假通过” | 未覆盖真实 AI | 关键 AI 用例直接测 `createAiClient` 真实策略；E2E 的 `delay` 仅用于缩短等待，落子仍走真实规则与控制器校验 |
| 完整对局耗时长 | E2E 超时 | `setAiMode('delay',{delayMs:0})` 加速 AI；用例 `test.setTimeout(240_000)`；循环加 300 手防护；失败保留 trace |
| 连跳 UI 的草稿状态与引擎状态不一致 | 断言误判 | 草稿期间统一用 `selection` + DOM `data-occupant/data-target` 断言；引擎状态用 `getState().pieces`，两者分开 |
| 重开/返回设置的 AI 竞态 | 旧结果污染新局 | 注入可控延迟 AI + 假时钟；断言 `gameId` 递增与旧 Promise 丢弃（UT-CTRL-12、E2E-RESTART-02/05） |
| 浏览器差异（WebKit SVG/布局） | E2E 失败 | Playwright 双项目；视口/命中区域用 testid 断言；渲染问题按 P1/P2 走设计走查 |
| 需求/契约文档笔误 | 用例与实现冲突 | 本计划已独立复算夹具 A/B/C；发现冲突按基线优先级 `@designer`/`@pm`，不在测试中静默修正 |

---

## 10. 回归策略

### 10.1 回归触发

| 变更类型 | 必跑范围 |
|---|---|
| 规则引擎（逻辑 `src/core/**`；S3 物理 `src/engine/**` + `src/types/**`） | 全部单元测试（board/rules/game）+ 全部 E2E（完整对局） |
| AI（`src/ai/**`） | UT-AI-01~10 + 含 AI 回合的 E2E（FIRST/SECOND/LOCK/RESTART） |
| 控制器/UI（`src/ui/**`） | 全部 controller 单测 + 全部 E2E |
| 构建/配置 | `npm ci && npm run build && npm test && npm run test:e2e` 全量 |
| 任何 P0 修复 | 相关 AC + AC-31（合法性）+ AC-32（时限）+ AC-48/49 全量 |
| 文档/契约变更 | 受影响 AC 的用例 + 全量 E2E testid 检查 |

### 10.2 缺陷回归

- 每个 P0/P1 缺陷修复必须新增或更新至少一条回归用例（单元或 E2E），并在 S7 中引用缺陷 ID。
- 同类 P0 连续两个修复周期未关闭：按项目 CLAUDE.md 由 developer `@cindy`，leader 决定是否暂停放行并升级 human。
- 回归不通过时不得关闭缺陷；不得通过删除/跳过用例“变绿”。

### 10.3 冒烟集（每次构建）

`npm test` 全绿 + E2E 的 `first-player.spec.ts`、`second-player.spec.ts`、`restart.spec.ts`、`illegal-move.spec.ts`、`ai-thinking.spec.ts` 全部通过。

---

## 11. 缺陷管理

### 11.1 等级

| 级别 | 定义 | 处理 |
|---|---|---|
| P0 | 无法开始对局；非法落子被接受；胜负判定错误；AI 卡死或产生非法着法；页面崩溃 | 立即阻塞主线并升级 human；S6 中 `@developer` 附复现 |
| P1 | 核心流程可用但明显错误：AI 超时 >2s、重开竞态污染、连跳 UI 错误、状态区错误 | 发布前必须关闭或 PM/人类书面接受 |
| P2 | 次要交互/视觉/文案问题，有替代路径 | PM 在发布评审决定接受/延期 |
| P3 | 细节优化、无功能影响 | 视排期处理 |

### 11.2 缺陷必填字段

缺陷 ID、级别、AC 编号、环境（Node/浏览器/版本）、夹具/种子/`gameId/ply`、复现步骤、预期/实际、日志/截图/trace、关联用例 ID。

### 11.3 反馈路径

- 实现缺陷：tester 在 S6/S8 `@developer`，P0 立即升级 human。
- 契约/设计冲突：`@designer` 裁决；涉及 PRD 条款 `@pm`。
- 需求歧义或验收变更：`@pm`；范围/依赖/排期 `@cindy`。
- 安全/合规/外部依赖（依赖安装、浏览器不可用）：立即 `@cindy` 并升级 human。

---

## 12. 交付物与后续步骤

| 交付物 | 路径 | 状态 |
|---|---|---|
| 测试计划 | `docs/test-plan.md` | 本文件 |
| 单元/控制器用例 | `tests/unit/fixtures.ts`、`board.test.ts`、`rules.test.ts`、`game.test.ts`、`ai.test.ts`、`controller.test.ts` | 已交付（S5） |
| E2E 用例 | `tests/e2e/helpers.ts`、`first-player.spec.ts`、`second-player.spec.ts`、`jump-chain.spec.ts`、`ai-thinking.spec.ts`、`illegal-move.spec.ts`、`restart.spec.ts`、`test-hook.spec.ts`、`network.spec.ts`、`responsive.spec.ts` | 已交付（S5） |
| 测试报告 | `docs/test-report.md` | S6 |
| 验收报告 | `docs/acceptance-report.md` | S8 |

**S6 入口条件**：S3/S4 完成、项目根目录可执行 `npm ci && npm run build && npm test && npm run test:e2e`；测试配置缺口（§3.3）由 S3 补齐。

**S8 出口条件**：§8.2 全部满足；仅 PM 在 S10 可给出发布结论。

---

## 13. 参考

1. `docs/prd.md` v1.0（S1，PM）
2. `docs/acceptance-criteria.md` v1.0（S1，PM，AC-01 ~ AC-51）
3. `docs/interface-contract.md` v1.0（S2，designer）
4. `docs/design-spec.md` v1.0（S2，designer，§9 文案 / §10 DOM 契约）
5. 项目 `CLAUDE.md`：角色协作与反馈路径

---

## 14. S5 预执行记录（与 S3 并行的只读验证）

> 目的：在 S3 引擎/AI 已落盘后，尽早验证测试断言与实现契约一致；**不改动 S3 工作区**。

### 14.1 环境

| 项目 | 值 |
|---|---|
| OS | macOS 26.6.2（Darwin 25.6.0） |
| Node.js | v26.0.0 |
| npm | 11.15.0 |
| TypeScript | 5.9.3 |
| Vitest | 3.2.7 |
| Playwright | 1.64.1 |
| Vite | 7.3.7 |

### 14.2 方法

1. 将 S3 工作区的 `src/`、`package.json`、`tsconfig.json`、`vite.config.ts`、`playwright.config.ts`、`index.html` 复制到 `/tmp/cc-testrun`（`node_modules` 以符号链接复用）。
2. 将 S5 的 `tests/unit/**` 与 `tests/e2e/**` 原样放入副本的 `tests/` 下。
3. 执行 `vitest run tests/unit/board.test.ts tests/unit/rules.test.ts tests/unit/game.test.ts tests/unit/ai.test.ts`。
4. 执行 `tsc --noEmit` 与 `playwright test --list`。

### 14.3 结果

| 检查 | 结果 | 备注 |
|---|---|---|
| 引擎/AI 单元测试 | **124/124 通过**（4 个文件，约 0.9s） | 覆盖 UT-BOARD、UT-COORD、UT-FIX、UT-RULE、UT-GAME、UT-AI；0 失败 |
| 类型检查（不含 controller） | 通过 | `tests/unit/controller.test.ts` 依赖 S4 的 `src/ui/controller`，尚未实现；使用契约形状的临时类型桩时同样通过 |
| Playwright 用例发现 | **52 个测试 / 9 个文件**（Chromium + WebKit 各 26） | 语法与 `data-testid` 辅助函数类型检查通过 |
| E2E 执行 | 未执行 | S3 入口仍为占位页，正式 UI 由 S4 交付；S6 执行 |
| 引擎/AI 功能缺陷 | 0 | 预执行期间发现并修正 3 处用例自身问题：`-0` 比较、初始跳跃中点颜色（应为红方下营子）、AI 超时日志断言层级 |
| 实现观察（非缺陷结论） | AI 客户端内部硬超时分支未打印 `console.warn` | 控制器层 UT-CTRL-13 断言降级日志；若 S4 也未记录，S6 按 AC-33 登记为缺陷 |

### 14.4 预执行修正记录

| 用例 | 现象 | 处理 |
|---|---|---|
| UT-BOARD-02 | `BOARD` 坐标计算产生 `-0`，Vitest `toEqual` 将其与 `0` 视为不同 | 新增 `norm()` 归一化 `-0` |
| UT-RULE-04 | 初始跳跃中点 `(4,-5,1)` 属于红方下营，原断言误写为蓝方 | 修正为 `'R'`，符合“跳跃不吃子、中点不变” |
| UT-AI-04 | AI 客户端硬超时未自身 `console.warn` | AI 级只断言 `degraded/reason/legality`；日志要求移至控制器级 UT-CTRL-13 |

> 上述预执行只证明引擎/AI 断言与 S3 当前实现一致；正式结论以 S6 在集成工程上执行 `npm test` / `npm run test:e2e` 的结果为准。
