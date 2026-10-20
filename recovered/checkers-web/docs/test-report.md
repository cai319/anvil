# 中国跳棋（2 人 Web 版）测试报告（S6）

> **S8 更新（2026-10-19）**：本文件为 S6 时点快照。D-01 已在 S7 修复并由 S8 复跑确认关闭；AC-49 最终 **66/66** 通过；AC-22 完全通过。最终验收结论、构建版本与发布建议以 [`docs/acceptance-report.md`](acceptance-report.md) 为准。

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 任务 | S6「自动化与手工测试执行、缺陷反馈」 |
| 父任务 | `28e915e5-6e96-48a0-88d0-b446c1a77e82`「开发一个跳棋小程序」 |
| 工作流 | `40d55cd5-f559-4a7f-999f-eeaa20eac377` |
| 被测版本 | `chinese-checkers-web@0.1.0`，构建 `dist/index.html` sha256 `76cd602c…` |
| 需求基线 | `docs/prd.md` v1.0、`docs/acceptance-criteria.md` v1.0（AC-01 ~ AC-51） |
| 接口基线 | `docs/interface-contract.md` v1.0、`docs/design-spec.md` v1.0 |
| 测试计划 | `docs/test-plan.md` v1.0（S5） |
| 执行日期 | 2026-10-19 |
| 执行人 | tester |
| 结论 | **无 P0/P1；1 个 P2（D-01）待 S7 修复/PM 决定；可进入 S7 缺陷修复阶段** |

---

## 1. 执行摘要

| 套件 | 结果 | 说明 |
|---|---|---|
| 单元/控制器（Vitest） | **191 / 191 通过**（8 文件，约 0.82s） | 含 S3/S4 自带 45 条 + S5 交付 146 条 |
| E2E（Playwright，Chromium + WebKit） | **50 / 52 通过**（1.3m） | Chromium 25/26、WebKit 25/26；唯一根因 **D-01**（P2） |
| 构建 | `npm run build` 通过 | Vite 7.3.7，18 modules，dist 产物已生成 |
| 类型检查 | `npm run typecheck` 通过 | TypeScript 5.9.3 strict |
| 安装 | `npm ci` 通过 | 3 个 devDependency 审计告警（SEC-01，P3） |
| 手工三浏览器冒烟 | **未执行（环境限制）** | 见 ENV-02；Chromium/WebKit 自动化已覆盖对应功能流 |

**核心结论**：

1. 规则引擎、AI、控制器、UI 主流程在 Chromium 与 WebKit 下均按 PRD/契约工作；先手与后手完整对局均通过。
2. 唯一自动化失败来自 **D-01**：`aria-busy`/`aria-disabled` 被设置在 `board-svg` 而非 `board` 容器，违反 `design-spec.md` §10.4 的 DOM 契约。**不影响玩法/输入锁定功能**，定级 P2。
3. 无 P0（无法开始对局、非法落子、胜负误判、AI 卡死/非法着法）与 P1 缺陷。
4. AC-49（E2E 全绿）当前不满足，需 S7 关闭 D-01 后由 S8 复跑；AC-50（三浏览器手工冒烟）需 PM/human 安排或接受环境限制。

---

## 2. 环境与构建版本

| 项目 | 值 |
|---|---|
| OS | macOS 26.6.2（Darwin 25.6.0） |
| Node.js / npm | v26.0.0 / 11.15.0 |
| TypeScript / Vitest / Playwright / Vite | 5.9.3 / 3.2.7 / 1.64.1 / 7.3.7 |
| Chromium | Chrome for Testing 156.0.8078.4（playwright chromium v1248） |
| WebKit | 27.2（playwright webkit v2370） |
| 被测工程 | `/Users/ikaruga/cctest/aliceAgent/data/agents/developer` |
| Package | `chinese-checkers-web@0.1.0` |
| 构建产物 | `dist/index.html` 464 B；`assets/index-DxaT9q5O.js` 62,810 B；`assets/index-C8phTnxe.css` 13,672 B |
| 产物 sha256 | index.html `76cd602c1f1b0cf336af0cb9d04a937cacba678cf407acd79b79e3f08549f97a`；JS `77688906a6584f10b49eac4237b81d8ed6ef52a38b75e6ee12d5c1cab6039124` |

原始环境记录：`test-results/env.txt`。

---

## 3. 执行命令与原始结果

| 命令 | 退出码 | 结果 | 原始日志 |
|---|---:|---|---|
| `npm ci` | 0 | 安装成功；3 个 devDependency 审计告警（SEC-01） | 终端记录（见 §7 SEC-01） |
| `npm run typecheck` | 0 | `tsc --noEmit` 无错误 | — |
| `npm run build` | 0 | Vite 构建成功，`dist/` 生成 | — |
| `npm test` | 0 | **191/191**，8 文件，约 0.82s | `test-results/unit-raw-after-ci.log` |
| `npx playwright test` | 1 | **50/52**，1.3m；2 失败均为 D-01 | `test-results/e2e-raw-run2.log` |
| `npx playwright install webkit` | 0 | 补装 WebKit 27.2（首轮缺失） | `test-results/e2e-raw.log`（首轮说明） |
| `npx playwright test ... --grep E2E-LOCK-01` | 1 | D-01 聚焦复现成功 | `test-results/defect-D01-repro.log` |

> 首轮 E2E 的 26 个 WebKit 用例因 `webkit-2370/pw_run.sh` 不存在而以 0ms 失败；执行 `npx playwright install webkit` 后重跑，WebKit 实际执行并通过 25/26。该环境事件记为 **ENV-01（已解决）**，不计产品缺陷。

### 3.1 单元测试分文件

| 文件 | 用例数 | 结果 |
|---|---:|---|
| `src/engine/engine.test.ts`（S3） | 20 | 通过 |
| `src/ai/ai.test.ts`（S3） | 11 | 通过 |
| `src/ui/controller.test.ts`（S4） | 14 | 通过 |
| `tests/unit/board.test.ts`（S5） | 39 | 通过 |
| `tests/unit/rules.test.ts`（S5） | 49 | 通过 |
| `tests/unit/game.test.ts`（S5） | 23 | 通过 |
| `tests/unit/ai.test.ts`（S5） | 13 | 通过 |
| `tests/unit/controller.test.ts`（S5） | 22 | 通过 |
| **合计** | **191** | **191 通过 / 0 失败 / 0 跳过** |

### 3.2 E2E 分浏览器

| Project | 用例数 | 通过 | 失败 | 失败用例 |
|---|---:|---:|---:|---|
| Chromium | 26 | 25 | 1 | E2E-LOCK-01（D-01） |
| WebKit | 26 | 25 | 1 | E2E-LOCK-01（D-01） |
| **合计** | **52** | **50** | **2** | — |

通过的关键流程（两浏览器均通过）：

- 先手完整对局：`E2E-FIRST-03`（SETUP → GAME_OVER → 重开）。
- 后手完整对局：`E2E-SECOND-02`（AI 自动首手 → 人类操作 → GAME_OVER → 重开）。
- AI 思考锁定：`E2E-SECOND-01`、`E2E-LOCK-02`。
- 非法操作提示：`E2E-ILLEGAL-01/02/03`。
- 连跳 UI：`E2E-JUMP-01/02/03`。
- 重开/返回设置：`E2E-RESTART-01~05`。
- 测试钩子隔离：`E2E-HOOK-01/02`。
- 零外部请求：`E2E-NET-01`。
- 响应式：`E2E-RESP-01`（1280×720、360×640）。

---

## 4. 缺陷清单

### D-01（P2，Open）棋盘容器缺少 `aria-busy` / `aria-disabled`

| 字段 | 内容 |
|---|---|
| 严重级别 | **P2**（次要可访问性/DOM 契约问题，无玩法与输入锁定影响；有 `turn-text`/`ai-thinking` 替代信息通道） |
| 关联 AC | AC-22（AI 思考锁定）、AC-28（稳定 DOM）、AC-49（E2E 全绿） |
| 关联设计条款 | `docs/design-spec.md` §10.4：棋盘容器 `aria-busy="true"` 仅 `AI_THINKING`；`GAME_OVER` 时 `aria-disabled="true"` |
| 影响用例 | `E2E-LOCK-01`（Chromium、WebKit 各 1 次失败） |
| 状态 | Open（待 S7 修复或 S10 决定接受） |

**复现步骤**

1. `cd /Users/ikaruga/cctest/aliceAgent/data/agents/developer`
2. `npm ci && npm run build`
3. `npx playwright test tests/e2e/ai-thinking.spec.ts --project=chromium --grep "E2E-LOCK-01"`
4. 或手工：打开 `/?e2e=1`，选「我先手」开始，点击 `(1,-5,4)` → `(1,-4,3)`，在 AI 思考期间执行：
   - `document.querySelector('[data-testid="board"]').getAttribute('aria-busy')`
   - `document.querySelector('[data-testid="board-svg"]').getAttribute('aria-busy')`

**预期**

- `[data-testid="board"]` 在 `AI_THINKING` 时 `aria-busy="true"`；`AI_THINKING`/`GAME_OVER` 时 `aria-disabled="true"`；其余状态为 `"false"` 或不存在。
- 与 `data-phase`、`data-game-id` 一起，保证读屏/自动化可通过棋盘容器获知锁定态。

**实际**

- `board` 容器（`div.board-frame`）只有 `data-phase`/`data-game-id`，**没有** `aria-busy`/`aria-disabled`（Playwright 取到 `null`）。
- 两个属性被设置在 `board-svg`：`src/ui/boardView.ts:767-768`。
- 运行时实测（AI_THINKING）：`{"boardBusy":null,"boardDisabled":null,"svgBusy":"true","svgDisabled":"true"}`。
- 功能层面点击锁定正常：`E2E-SECOND-01`、`E2E-LOCK-02`、`UT-CTRL-07` 均通过。

**证据**

- 失败日志：`test-results/defect-D01-repro.log`、`test-results/e2e-raw-run2.log`（失败详情 `Expected "true" / Received ""`）。
- 截图/trace/error-context：`test-results/artifacts/D-01/ai-thinking-…-chromium/test-failed-1.png`、`trace.zip`、`error-context.md`。
- 运行时属性实测：`test-results/artifacts/D-01/aria-runtime-check.log`（`boardBusy=null, svgBusy="true"`）。
- 页面截图：`test-results/artifacts/screenshots/01-board-initial-1280x720.png`、`02-ai-thinking-1280x720.png`。
- 源码证据：`test-results/artifacts/D-01/source-boardView.txt`（`src/ui/boardView.ts:767-768`）、`source-app.txt`（`src/ui/app.ts:288-293`）。

**修复建议**

在 `src/ui/boardView.ts` 的 `update()` 中，除 `svg` 外同步设置 `mount`（`data-testid="board"`）：

```ts
mount.setAttribute('aria-busy', snapshot.phase === 'AI_THINKING' ? 'true' : 'false');
mount.setAttribute('aria-disabled', locked ? 'true' : 'false');
```

可保留 `svg` 上的属性（读屏兼容），或按设计文档只保留在 `board` 容器。修复后 `E2E-LOCK-01` 应双浏览器通过。

---

### SEC-01（P3 / 风险，Open，待 S10 决定）

- **现象**：`npm ci` 报告 3 个漏洞：1 moderate（`@vitest/mocker` 路径穿越）、2 critical（`tinypool` 原型污染 → RCE）。
- **影响面**：均位于 **devDependencies**（Vitest 测试工具链），不进入 `dist` 运行时；应用为纯静态、零后端、零外部请求。
- **修复**：`npm audit fix --force` 会升级到 `vitest@5.0.5`（breaking），可能破坏现有测试/脚本。**S6 未执行强制升级**。
- **建议**：由 PM 在 S10 决定「接受为开发期风险」或「单独排期升级 Vitest 5.x 并回归」。
- **证据**：`npm audit` 输出（本报告 §3 命令记录）。

### ENV-01（P3，Resolved）

- 首轮 E2E 时 WebKit 可执行文件缺失（`webkit-2370/pw_run.sh` 不存在），26 个 WebKit 用例 0ms 失败。
- 处理：`npx playwright install webkit` 成功下载 WebKit 27.2；重跑后 WebKit 25/26 通过。**非产品缺陷**。

### ENV-02（P3，Open）

- **AC-50 三浏览器手工冒烟未执行**：当前为无头自动化环境，无 Chrome/Edge/Safari 图形界面。
- 替代证据：Playwright Chromium（Chrome/Edge 同内核）与 WebKit（Safari 同内核）已完成先手/后手完整对局、连跳、AI 思考、重开、非法提示等流程。
- **建议**：S8 或发布前由人工完成 AC-50；若无法执行，由 PM/human 在 S10 书面接受该环境限制。

---

## 5. 验收标准执行结果（AC-01 ~ AC-51）

> 状态：**通过** / **失败** / **部分** / **未执行** / **继承**。证据中的 UT/E2E 编号见 `docs/test-plan.md` §6。

| AC | 优先级 | 状态 | 证据/说明 |
|---|---|---|---|
| AC-01 棋盘结构 | P0 | 通过 | UT-BOARD-01/02/03、UT-COORD-01 |
| AC-02 初始局面 | P0 | 通过 | UT-BOARD-04/05、UT-GAME-01/02 |
| AC-03 开始界面 | P0 | 通过 | E2E-SETUP-01、UT-CTRL-17 |
| AC-04 人类先手流程 | P0 | 通过 | UT-CTRL-01/03、E2E-FIRST-01/02 |
| AC-05 人类后手/AI 首手 | P0 | 通过 | UT-CTRL-02、E2E-SECOND-01 |
| AC-06 六方向单步 | P0 | 通过 | UT-RULE-01/02/03 |
| AC-07 跳跃与不吃子 | P0 | 通过 | UT-RULE-04/05/06 |
| AC-08 连跳/转向/停止 | P0 | 通过 | UT-RULE-07/08/09、E2E-JUMP-01/02 |
| AC-09 连跳不得重复落点 | P0 | 通过 | UT-RULE-10 |
| AC-10 动作类型/回合切换 | P0 | 通过 | UT-RULE-11/23、UT-CTRL-03 |
| AC-11 非法走子拒绝 | P0 | 通过 | UT-RULE-12/13/14、UT-CTRL-07、E2E-ILLEGAL-03 |
| AC-12 行动方/可选棋子 | P0 | 通过 | UT-CTRL-04/06、E2E-ILLEGAL-01/02 |
| AC-13 胜负 10/10 与 9/10 | P0 | 通过 | UT-RULE-17/19、UT-GAME-05 |
| AC-14 最后一子进营立即胜 | P0 | 通过 | UT-RULE-18、E2E-FIRST-04 |
| AC-15 轮空/连续轮空 | P0 | 通过 | UT-RULE-20/21、UT-CTRL-08/09 |
| AC-16 300 手平局 | P0 | 通过 | UT-RULE-22 |
| AC-17 终局封盘/展示 | P0 | 通过 | E2E-FIRST-03/04、E2E-RESTART-04、UT-RULE-13 |
| AC-18 棋盘渲染 | P0 | 通过 | E2E-FIRST-01/05（121 孔/20 子/SVG） |
| AC-19 选子/高亮 | P0 | 通过 | E2E-FIRST-02、UT-CTRL-05/06 |
| AC-20 连跳 UI | P0 | 通过 | E2E-JUMP-01/02/03、UT-RULE-07 |
| AC-21 状态区 | P0 | 通过 | E2E-FIRST-01/02、E2E-SECOND-01 |
| AC-22 AI 思考锁定 | P0 | **通过（功能）** | 功能：UT-CTRL-07、E2E-SECOND-01、E2E-LOCK-02；`E2E-LOCK-01` 因 D-01 的 `aria-busy` 断言失败（P2，不影响输入锁定） |
| AC-23 非法点击提示 | P1 | 通过 | E2E-ILLEGAL-01/02/03 |
| AC-24 重新开始 | P0 | 通过 | E2E-RESTART-01/04、UT-CTRL-10 |
| AC-25 返回设置 | P1 | 通过 | E2E-RESTART-03/05、UT-CTRL-11 |
| AC-26 AI 思考中重开/返回 | P0 | 通过 | E2E-RESTART-02/05、UT-CTRL-12 |
| AC-27 响应式 | P1 | 通过 | E2E-RESP-01（1280×720、360×640） |
| AC-28 稳定选择器 | P1 | 通过 | E2E-FIRST-05、E2E-SETUP-01；全部 E2E 使用 `data-testid` |
| AC-29 中文文案/可访问性 | P2 | **部分** | 中文文案/无英文占位已由 E2E-ILLEGAL-01/02 验证；`aria-live`/焦点/200% 缩放等人工检查未执行（P2，建议 S9/S8） |
| AC-30 三档难度/默认 | P0 | 通过 | UT-AI-01/02、E2E-SETUP-01 |
| AC-31 AI 只产生合法着法 | P0 | 通过 | UT-AI-02（3 档 × 100 手 AI-vs-AI） |
| AC-32 AI 每步 ≤2000ms | P0 | 通过 | UT-AI-03（3 档 × 3 局面，实测均 ≤2000ms） |
| AC-33 AI 超时降级 | P0 | 通过 | UT-AI-04（假时钟）、UT-CTRL-13（看门狗 + console.warn） |
| AC-34 AI 异常降级 | P0 | 通过 | UT-AI-05、UT-CTRL-15 |
| AC-35 AI 非法输出拒绝 | P0 | 通过 | UT-AI-06、UT-CTRL-14 |
| AC-36 AI 无合法着法轮空 | P0 | 通过 | UT-CTRL-08/09 |
| AC-37 连续降级切简单 | P1 | 通过（控制器层） | UT-CTRL-16；UI 持久提示未单独 E2E 断言（P1，建议 S8 人工确认） |
| AC-38 AI 一步获胜 | P1 | 通过 | UT-AI-07 |
| AC-39 AI 必挡 | P1 | 通过 | UT-AI-08 |
| AC-40 可注入策略/种子/时钟 | P1 | 通过 | UT-AI-04/05/06/09 |
| AC-41 AI 先手首手时限 | P0 | 通过 | E2E-SECOND-01（≤2000ms）、UT-CTRL-02 |
| AC-42 技术栈/脚本 | P0 | 通过（命令） | `npm ci`/`typecheck`/`build`/`test` 均 exit 0；`test:e2e` 可执行（结果见 AC-49） |
| AC-43 静态/零外部请求 | P0 | 通过 | E2E-NET-01（Chromium + WebKit） |
| AC-44 浏览器兼容 | P0 | **部分** | Chromium 25/26、WebKit 25/26；唯一失败为 D-01 属性；三浏览器手工冒烟未执行（ENV-02） |
| AC-45 目录分层 | P1 | 通过 | `src/engine`+`src/types`（契约逻辑 `src/core`）/`src/ai`/`src/ui`；engine/ai 无 DOM 引用；单测在 Node 运行 |
| AC-46 测试钩子 | P1 | 通过 | E2E-HOOK-01/02（双浏览器） |
| AC-47 README | P1 | 通过 | 安装/命令/规则/操作/AI/浏览器/限制章节齐全（人工检查） |
| AC-48 单元测试全绿 | P0 | 通过 | `npm test` 191/191，无 P0 skip |
| AC-49 E2E 全绿 | P0 | **失败** | 50/52；2 失败均为 D-01（P2）；需 S7 修复或 S10 决定后由 S8 复跑 |
| AC-50 三浏览器手工冒烟 | P1 | **未执行** | 无头环境限制；Chromium/WebKit 自动化覆盖对应功能流（ENV-02） |
| AC-51 S1 交付基线 | P0 | 继承通过 | S1 已 approved；S6 未修改需求/设计文档 |

**统计**：51 条中，**通过 44 条**、**通过（功能）/部分 4 条**（AC-22、AC-29、AC-37、AC-44）、**失败 1 条**（AC-49）、**未执行 1 条**（AC-50）、**继承通过 1 条**（AC-51）。
> 失败/部分项均不涉及 P0/P1 功能缺陷；AC-49 由 D-01（P2）导致。

---

## 6. 手工测试记录

| 项目 | 结果 | 说明 |
|---|---|---|
| Chromium 自动化冒烟（≈Chrome/Edge 内核） | 通过（除 D-01） | 先手/后手完整对局、连跳、AI 思考、重开、非法提示、响应式、零外部请求 |
| WebKit 自动化冒烟（≈Safari 内核） | 通过（除 D-01） | 同上；WebKit 27.2 |
| Chrome / Edge / Safari 人工冒烟（AC-50） | 未执行 | 无头环境，无图形界面；建议 S8/PM 安排人工或由 human 接受 |
| 控制台错误检查 | 未单独断言 | 完整对局与关键流程未出现 Playwright 页面异常；建议 S8 加 `pageerror`/`console.error` 监听（P2） |

---

## 7. 失败原因与复现入口

| 失败 | 根因 | 复现入口 |
|---|---|---|
| `E2E-LOCK-01` Chromium/WebKit | D-01：`board` 容器缺少 `aria-busy`/`aria-disabled` | `test-results/defect-D01-repro.log`；`test-results/artifacts/D-01/`；命令见 §4 D-01 |
| 首轮 26 个 WebKit 用例 | ENV-01：WebKit 未安装 | `test-results/e2e-raw.log`；已通过 `npx playwright install webkit` 解决 |

---

## 8. 未覆盖/限制项

1. **AC-50 三浏览器人工冒烟**：无头环境限制，见 ENV-02。
2. **AC-29 可访问性人工检查**：`aria-live`、键盘焦点、200% 缩放、颜色非唯一信息载体等需人工/工具检查；S5 自动化仅验证中文文案与关键 testid。
3. **AC-37 UI 持久降级提示**：控制器层已通过 UT-CTRL-16；未在 E2E 中注入三次降级验证 `ai-degraded-notice` 文案（P1，建议 S8 补充）。
4. **AC-33 真实 2s 超时的 E2E**：AI 时限由单元测试假时钟与控制器看门狗覆盖；未在浏览器中真实等待 2s（按 S5 计划，超时逻辑用假时钟确定性验证）。
5. **控制台 `console.error` / `pageerror`**：未作为 E2E 全局失败条件；建议 S8 补充。

---

## 9. 结论与后续

### 9.1 S6 结论

- **无 P0/P1 缺陷**；规则/AI/控制器/UI 主流程在 Chromium 与 WebKit 下均可完成先手/后手完整对局。
- **1 个 P2 缺陷 D-01**（`aria-busy`/`aria-disabled` 位置）导致 `E2E-LOCK-01` 双浏览器失败；功能不受影响，但需 S7 修复（推荐）或在 S10 由 PM 接受并同步调整 DOM 契约/用例。
- **2 个 P3 风险**：SEC-01（devDependency 审计）、ENV-02（AC-50 手工冒烟未执行）。
- **测试结论：可以进入 S7 缺陷修复阶段**。

### 9.2 S7 建议

1. 修复 D-01：在 `board` 容器同步设置 `aria-busy`/`aria-disabled`（见 §4 修复建议），复跑 `E2E-LOCK-01` 双浏览器。
2. 复跑全量 `npm test` 与 `npm run test:e2e`，确认 191/191 + 52/52。
3. SEC-01 由 PM 在 S10 决定接受或排期升级 Vitest 5.x。
4. AC-50 人工冒烟由 S8/PM/human 安排；若无法执行，需书面接受环境限制。

### 9.3 证据索引

| 证据 | 路径 |
|---|---|
| 环境/版本/构建哈希 | `test-results/env.txt` |
| 结果摘要 JSON | `test-results/summary.json` |
| 最终单元日志 | `test-results/unit-raw-after-ci.log` |
| 最终 E2E 日志 | `test-results/e2e-raw-run2.log` |
| 首轮 E2E（WebKit 缺失） | `test-results/e2e-raw.log` |
| D-01 聚焦复现日志 | `test-results/defect-D01-repro.log` |
| D-01 截图/trace/源码证据 | `test-results/artifacts/D-01/` |
| D-01 运行时属性实测 | `test-results/artifacts/D-01/aria-runtime-check.log` |
| 页面截图（初始/AI 思考） | `test-results/artifacts/screenshots/` |
| 测试计划（AC 映射） | `docs/test-plan.md` |
