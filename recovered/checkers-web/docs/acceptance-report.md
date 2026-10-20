# 中国跳棋（2 人 Web 版）集成验收报告（S8）

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 任务 | S8「集成验收测试与发布验证」 |
| 父任务 | `28e915e5-6e96-48a0-88d0-b446c1a77e82`「开发一个跳棋小程序」 |
| 工作流 | `40d55cd5-f559-4a7f-999f-eeaa20eac377` |
| 验收对象 | **最终构建产物** `dist/`（Vite build，2026-10-19） |
| 被测版本 | `chinese-checkers-web@0.1.0` |
| 需求基线 | `docs/prd.md` v1.0、`docs/acceptance-criteria.md` v1.0（AC-01 ~ AC-51） |
| 接口/设计基线 | `docs/interface-contract.md` v1.0、`docs/design-spec.md` v1.0 |
| 上游测试 | `docs/test-plan.md`、`docs/test-report.md`（S5/S6）、`docs/ux-review.md`（S9） |
| 执行日期 | 2026-10-19 |
| 执行人 | tester |
| 测试结论 | **自动化全绿；无 P0/P1；建议有条件发布**（条件见 §9，最终决策由 PM 在 S10 给出） |

---

## 1. 执行摘要

| 项目 | 结果 |
|---|---|
| 单元/控制器（Vitest） | **191 / 191 通过**（8 文件，0.81s） |
| E2E（Playwright Chromium + WebKit） | **66 / 66 通过**（Chromium 33 + WebKit 33，1.3m） |
| 真实 Chrome 实机冒烟 | **通过**（Chrome 155.0.8059.80；先手/后手、AI 首手 400ms、非法提示、重开、刷新、红胜/蓝胜/平局） |
| 最终构建 | `npm run build` 通过；dist 产物已由静态服务器实际加载 |
| Typecheck | 通过（TypeScript 5.9.3 strict） |
| 关键路径 | 先手/后手、AI 首步、单步/连跳、非法拦截、AI 时限与合法性、胜负结算、重开、刷新后行为——全部通过 |
| P0/P1 缺陷 | **0** |
| P2 缺陷 | S6 D-01 与 S9 P2-01~05 **全部修复并验证**；无未关闭 P2 |
| P3/风险 | 记录 4 类：剩余 P3 微动效/文案、FAV-01（favicon 404 控制台噪音）、SEC-01（dev-only 审计）、ENV-02/AC-50（人工三浏览器冒烟环境限制） |
| 发布建议 | **有条件发布**：自动化与 Chrome 实机满足发布门槛；需 PM 在 S10 接受 AC-50 环境限制、SEC-01 与剩余 P3，或安排人工冒烟后再发布 |

---

## 2. 环境与最终构建版本

| 项目 | 值 |
|---|---|
| OS | macOS 26.6.2（Darwin 25.6.0） |
| Node.js / npm | v26.0.0 / 11.15.0 |
| TypeScript / Vitest / Playwright / Vite | 5.9.3 / 3.2.7 / 1.64.1 / 7.3.7 |
| Chromium（Playwright） | Chrome for Testing 156.0.8078.4（chromium v1248） |
| WebKit（Playwright） | 27.2（webkit v2370） |
| 真实 Chrome（channel=chrome） | **155.0.8059.80** |
| Edge / Safari 手工 | Edge 未安装；Safari 无 GUI 手工环境（WebKit 引擎自动化覆盖） |
| 工程根目录 | `/Users/ikaruga/cctest/aliceAgent/data/agents/developer` |
| 入口 | `dist/index.html`，静态托管，`?e2e=1` 启用测试钩子 |

### 2.1 dist 最终产物（sha256）

| 文件 | 大小 | sha256 |
|---|---:|---|
| `dist/index.html` | 464 B | `e5c7fe64ca38eb84f35dfea01b00971eb9d2cb8533c663b0d6c30ab26bffee2e` |
| `dist/assets/index-DaOPTblc.js` | 63.21 kB | `c6876ce09796b8d56fdb473a611fd8f16b9d97015ce0b3cf5bcebd9b1b5a5356` |
| `dist/assets/index-Z-WM6PhP.css` | 14.52 kB | `c278630f54c4dac36f27000fe20f1959db7ff2bc46c7e09380552254cf152a94` |
| `dist/assets/index-DaOPTblc.js.map` | 267.26 kB | `7861cc20d9dc1abd6fdf0b52ec27bb2d3ba11fa888b9941425f388005d3c289b` |

---

## 3. 执行命令与原始结果

| 命令 | 退出码 | 结果 | 原始日志 |
|---|---:|---|---|
| `npm run build` | 0 | Vite 7.3.7 构建成功，18 modules，dist 生成 | 终端记录 / `test-results/S8-e2e-final.log`（webServer 亦构建） |
| `npm run typecheck` | 0 | `tsc --noEmit` 无错误 | 终端记录 |
| `npm test` | 0 | **191/191**（8 文件，0.809s） | `test-results/S8-unit-final.log` |
| `npx playwright test` | 0 | **66/66**（Chromium 33 + WebKit 33，1.3m） | `test-results/S8-e2e-final.log` |
| 真实 Chrome 冒烟 | 0 | 关键路径全部通过；1 个 favicon 404 控制台噪音 | `test-results/acceptance-chrome-smoke.log` |
| Chrome 蓝胜/平局 | 0 | 蓝方获胜 / 平局弹层正确 | `test-results/acceptance-chrome-win-draw.log` |
| Chrome 404/刷新检查 | 0 | `/favicon.ico` 404；刷新后 SETUP 可见、棋盘不可见 | `test-results/acceptance-chrome-404-check.log` |
| Chrome 移动端截图 | 0 | 360×640：CTA bottom 443.8、高 44；棋盘 336×380.8、无横向滚动 | `test-results/acceptance-chrome-mobile.log` |

> `playwright.config.ts` 已设置 `reuseExistingServer: false`，每次 E2E 都重新构建并启动静态服务，避免复用陈旧 dist。

---

## 4. 关键路径验证（S8 要求）

| 关键路径 | 结果 | 证据 |
|---|---|---|
| 人类先手完整流程 | 通过 | E2E-FIRST-01/02/03；Chrome 冒烟 move-count 0→1→2、回合回到红方 |
| 人类后手 / AI 自动首手 | 通过 | E2E-SECOND-01/02；Chrome 实机 AI 首手 **400ms**（≤2000ms），随后「你的回合 · 蓝方」 |
| SETUP → AI_THINKING 同步迁移 | 通过 | E2E-S7-06：点击开始后立即 `getState()`，phase=AI_THINKING、ply=0、turn=R |
| 单步 | 通过 | E2E-FIRST-02；UT-RULE-01/02；Chrome 实机单步 |
| 跳跃 / 连跳 / 结束回合 | 通过 | E2E-JUMP-01/02/03；UT-RULE-04~09 |
| 非法走子拦截 | 通过 | E2E-ILLEGAL-01/02/03；UT-RULE-12；Chrome 实机提示「请选择一枚棋子」 |
| AI 只输出合法着法 | 通过 | UT-AI-02：3 档 × 100 手 AI-vs-AI，全部合法 |
| AI 每步 ≤2000ms | 通过 | UT-AI-03：3 档 × 3 局面，墙钟 ≤2000ms；E2E-SECOND-01；Chrome 实机 400ms |
| AI 超时/异常/非法输出兜底 | 通过 | UT-AI-04/05、UT-CTRL-13/14/15 |
| 胜负结算（红胜/蓝胜/平局） | 通过 | E2E-FIRST-04（红胜）；Chrome 夹具 B（蓝胜）、GAME_OVER 平局弹层；UT-RULE-17/19/21 |
| 终局封盘与重开 | 通过 | E2E-FIRST-03/04、E2E-RESTART-04；Chrome 重开后 move-count=0 |
| AI 思考中重开/返回设置 | 通过 | E2E-RESTART-02/05；UT-CTRL-12 |
| 刷新后行为 | 通过（允许丢失对局） | Chrome：刷新后 `setup-panel` 可见、棋盘不可见（`boardVisible=false`）；可重新开始 |
| 零外部请求 | 通过 | E2E-NET-01（Chromium + WebKit） |
| 响应式 360×640 / 1280×720 | 通过 | E2E-RESP-01；E2E-S7-03/04；Chrome 移动端实测无横向滚动 |
| 落子动画不阻塞状态更新 | 通过 | E2E-ANIM-01 页面内 pointerdown→MutationObserver：move-count/occupant <100ms |

---

## 5. 验收标准逐条结论（AC-01 ~ AC-51）

> 状态：**通过** / **部分** / **未执行** / **继承**。P0/P1/P2 沿用验收基线。

| AC | 优先级 | 结论 | 证据/说明 |
|---|---|---|---|
| AC-01 棋盘结构 | P0 | 通过 | UT-BOARD-01/02/03、UT-COORD-01 |
| AC-02 初始局面 | P0 | 通过 | UT-BOARD-04/05、UT-GAME-01/02 |
| AC-03 开始界面 | P0 | 通过 | E2E-SETUP-01、UT-CTRL-17；注：favicon 404 控制台噪音见 FAV-01（P3） |
| AC-04 人类先手流程 | P0 | 通过 | UT-CTRL-01/03、E2E-FIRST-01/02 |
| AC-05 人类后手/AI 首手 | P0 | 通过 | E2E-SECOND-01/02；Chrome 实机 400ms |
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
| AC-17 终局封盘/展示 | P0 | 通过 | E2E-FIRST-03/04、E2E-RESTART-04；Chrome 红/蓝/平局弹层；刷新丢失对局符合预期 |
| AC-18 棋盘渲染 | P0 | 通过 | E2E-FIRST-01/05（121 孔/20 子/SVG） |
| AC-19 选子/高亮 | P0 | 通过 | E2E-FIRST-02、UT-CTRL-05/06 |
| AC-20 连跳 UI | P0 | 通过 | E2E-JUMP-01/02/03、UT-RULE-07 |
| AC-21 状态区 | P0 | 通过 | E2E-FIRST-01/02、E2E-SECOND-01 |
| AC-22 AI 思考锁定 | P0 | 通过 | D-01 修复后 E2E-LOCK-01/02 双浏览器通过；board/svg aria-busy/aria-disabled 同步 |
| AC-23 非法点击提示 | P1 | 通过 | E2E-ILLEGAL-01/02/03；Chrome 实机提示 |
| AC-24 重新开始 | P0 | 通过 | E2E-RESTART-01/04、UT-CTRL-10；Chrome 重开 move-count=0 |
| AC-25 返回设置 | P1 | 通过 | E2E-RESTART-03/05、UT-CTRL-11 |
| AC-26 AI 思考中重开/返回 | P0 | 通过 | E2E-RESTART-02/05、UT-CTRL-12 |
| AC-27 响应式 | P1 | 通过 | E2E-RESP-01、E2E-S7-03/04；Chrome 360×640 实测 |
| AC-28 稳定选择器 | P1 | 通过 | E2E-FIRST-05、E2E-SETUP-01；全部 E2E 使用 data-testid |
| AC-29 中文文案/可访问性 | P2 | 通过（S9 人工） | 中文文案/无英文占位由 E2E 验证；S9 走查验证键盘 radio/焦点环/aria-live/prefers-reduced-motion；无 P0/P1 |
| AC-30 三档难度/默认 | P0 | 通过 | UT-AI-01/02、E2E-SETUP-01 |
| AC-31 AI 只产生合法着法 | P0 | 通过 | UT-AI-02（3 档 × 100 手） |
| AC-32 AI 每步 ≤2000ms | P0 | 通过 | UT-AI-03；E2E-SECOND-01；Chrome 400ms |
| AC-33 AI 超时降级 | P0 | 通过 | UT-AI-04、UT-CTRL-13 |
| AC-34 AI 异常降级 | P0 | 通过 | UT-AI-05、UT-CTRL-15 |
| AC-35 AI 非法输出拒绝 | P0 | 通过 | UT-AI-06、UT-CTRL-14 |
| AC-36 AI 无合法着法轮空 | P0 | 通过 | UT-CTRL-08/09 |
| AC-37 连续降级切简单 | P1 | 通过 | UT-CTRL-16；E2E-S7-05（forcedEasy、notice 在状态卡内、不压页脚） |
| AC-38 AI 一步获胜 | P1 | 通过 | UT-AI-07 |
| AC-39 AI 必挡 | P1 | 通过 | UT-AI-08 |
| AC-40 可注入策略/种子/时钟 | P1 | 通过 | UT-AI-04/05/06/09 |
| AC-41 AI 先手首手时限 | P0 | 通过 | E2E-SECOND-01（计时口径按 FR-28/AC-32，AI 段 ≤2000ms）；E2E-S7-06 覆盖点击→AI_THINKING 同步迁移 |
| AC-42 技术栈/脚本 | P0 | 通过 | `npm ci`/`typecheck`/`build`/`test`/`test:e2e` 均可执行；最终构建 exit 0 |
| AC-43 静态/零外部请求 | P0 | 通过 | E2E-NET-01（双浏览器）；dist 纯静态 |
| AC-44 浏览器兼容 | P0 | 通过（自动化+Chrome 实机） | Chromium 33/33、WebKit 33/33；真实 Chrome 155.0.8059.80 冒烟通过；Edge/Safari 以同内核自动化覆盖，人工三浏览器见 AC-50 |
| AC-45 目录分层 | P1 | 通过 | `src/engine`+`src/types`（契约逻辑 `src/core`）/`src/ai`/`src/ui`；engine/ai 无 DOM；单测 Node 运行 |
| AC-46 测试钩子 | P1 | 通过 | E2E-HOOK-01/02 双浏览器 |
| AC-47 README | P1 | 通过 | 安装/命令/规则/操作/AI/浏览器/限制章节齐全 |
| AC-48 单元测试全绿 | P0 | 通过 | `npm test` 191/191，无 P0 skip |
| AC-49 E2E 全绿 | P0 | 通过 | 最终构建 **66/66**（Chromium 33 + WebKit 33） |
| AC-50 三浏览器手工冒烟 | P1 | **部分（环境限制）** | 真实 Chrome 实机自动化通过；Edge 未安装、Safari 无 GUI 手工；WebKit 引擎 E2E 覆盖 Safari 行为。需 PM/human 在 S10 接受或安排人工 |
| AC-51 S1 交付基线 | P0 | 继承通过 | S1 已 approved；S8 未修改需求/设计文档 |

**统计**：AC-01 ~ AC-51 共 51 条；**通过 50 条**（其中 AC-44 含 Chrome 实机与同内核自动化证据）、**部分 1 条**（AC-50 人工三浏览器环境限制）、**未通过 0 条**。

---

## 6. S9 P2/P3 与 S6 D-01 关闭情况

| 编号 | 级别 | 状态 | 验证 |
|---|---|---|---|
| D-01（S6）board aria-busy/aria-disabled | P2 | **已关闭** | E2E-LOCK-01 双浏览器通过；board 与 svg 同步 |
| S9 P2-01 连跳条改变棋盘尺寸 | P2 | **已修复/已验证** | E2E-S7-01（1280×720 前后尺寸一致） |
| S9 P2-02 降级提示越出首屏/压页脚 | P2 | **已修复/已验证** | E2E-S7-05（forcedEasy、notice 在 turn-indicator 内、bottom<footer top） |
| S9 P2-03 右栏卡片间距 71px | P2 | **已修复/已验证** | E2E-S7-02（1440×900 间距 16px） |
| S9 P2-04 360×640 开始 CTA 折叠以下 | P2 | **已修复/已验证** | E2E-S7-03；Chrome 实测 start bottom=443.8、height=44 |
| S9 P2-05 移动端结束回合 <44px | P2 | **已修复/已验证** | E2E-S7-04（360×640 height≥44） |
| S9 P3（7 项已修） | P3 | 已修复 | 选中卡背景、结算图标、Toast 避让、降级读屏分隔、副文案、操作说明、720–1023 并排（S7 自测） |
| S9 P3（剩余 2 项） | P3 | Open | 草稿/最后一手微动效、少量文案微调；建议 PM 在 S10 接受或排期 |
| FAV-01 favicon 404 | P3 | Open | 浏览器自动请求 `/favicon.ico` 返回 404，控制台一条 `Failed to load resource`；无功能影响。详见 §7 |
| SEC-01 devDependency 审计 | P3 | Open | `npm audit` 3 漏洞（1 moderate / 2 critical）均在 vitest/tinypool 测试工具链，dist 不含；建议 PM S10 接受或排期升级 |
| ENV-02 / AC-50 人工三浏览器 | P3 | Open | 真实 Chrome 自动化通过；Edge/Safari 人工未执行；需 PM/human 决策 |
| ENV-01 WebKit 缺失 | P3 | Resolved | `npx playwright install webkit` 后 33/33 通过 |

**无未关闭 P0/P1。** 所有 P2 均已修复并有回归用例。

---

## 7. 遗留问题详情

### FAV-01（P3）favicon 404 控制台噪音

- **复现**：用 Chrome 打开 `http://127.0.0.1:4174/`（dist 静态服务）→ DevTools Console 出现 `Failed to load resource: the server responded with a status of 404 (Not Found)`；`curl http://127.0.0.1:4174/favicon.ico` 返回 404。
- **原因**：`index.html` / `dist/index.html` 未声明 favicon，浏览器自动请求 `/favicon.ico`。
- **影响**：仅控制台噪音，无功能/布局/网络外部依赖影响。
- **建议**：添加 favicon 文件或 `<link rel="icon" href="data:,">`；P3，可由 PM 在 S10 决定接受或排期。
- **证据**：`test-results/acceptance-chrome-smoke.log`、`test-results/acceptance-chrome-404-check.log`。

### SEC-01（P3，dev-only）

- `npm audit`：`@vitest/mocker` moderate、`tinypool` critical ×2；均位于 devDependencies。
- `dist` 为纯静态产物，不含这些包；运行时无影响。
- `npm audit fix --force` 会升级 Vitest 5.x（breaking），S6/S8 未执行。建议 PM S10 接受或单独排期。

### ENV-02 / AC-50（P3，环境限制）

- 真实 Chrome 155.0.8059.80 已完成实机自动化冒烟（先手/后手、AI 首手 400ms、非法提示、重开、刷新、红/蓝/平局）。
- Edge 未安装；Safari 无 GUI 手工环境；WebKit 27.2 E2E 覆盖 Safari 引擎行为。
- 建议 PM/human 在 S10 决定：安排人工三浏览器冒烟，或书面接受该环境限制。

### 剩余 P3（S9）

- 草稿/最后一手微动效、少量文案微调；不影响功能与验收结论，建议 PM S10 决定接受或排期。

---

## 8. 证据索引

| 证据 | 路径 |
|---|---|
| 最终单元日志（191/191） | `test-results/S8-unit-final.log` |
| 最终 E2E 日志（66/66） | `test-results/S8-e2e-final.log` |
| Chrome 实机冒烟结果 | `test-results/acceptance-chrome-smoke.log` |
| Chrome 蓝胜/平局 | `test-results/acceptance-chrome-win-draw.log` |
| Chrome 404/刷新检查 | `test-results/acceptance-chrome-404-check.log` |
| Chrome 移动端指标 | `test-results/acceptance-chrome-mobile.log` |
| 桌面截图（初始/后手/结果） | `test-results/acceptance/chrome-01-initial.png`、`chrome-02-second-flow.png`、`chrome-03-result.png` |
| 移动端截图（360×640） | `test-results/acceptance/chrome-04-mobile-setup.png`、`chrome-05-mobile-board.png` |
| 蓝胜/平局截图 | `test-results/acceptance/chrome-06-blue-win.png`、`chrome-07-draw.png` |
| S7 回归日志（计时 5×WebKit、P2-02 等） | `test-results/verify-S7-*.log` |
| 测试计划（AC 映射） | `docs/test-plan.md` |
| S6 测试报告 | `docs/test-report.md` |
| S9 UX 走查 | `docs/ux-review.md` |

---

## 9. 发布建议（S10 输入）

### 9.1 测试结论

- **自动化验收通过**：单元 191/191、E2E 66/66、最终 dist 构建成功、Chrome 实机关键路径通过。
- **无未关闭 P0/P1**；全部 P2 已修复并验证；P3/风险已登记。
- 成功验收的关键路径：先手/后手、AI 首步、单步/连跳、非法拦截、AI 时限与合法性、红/蓝/平局结算、重开、刷新后行为、零外部请求、响应式。

### 9.2 建议

**建议有条件发布**，条件由 PM 在 S10 明确：

1. **AC-50（P1）**：人工三浏览器冒烟未完成（Edge 未安装、Safari 无 GUI）。建议安排人工冒烟，或由 PM/human 书面接受环境限制；替代证据为 Chromium/WebKit 自动化 + 真实 Chrome 实机冒烟。
2. **SEC-01（P3）**：接受 devDependency 审计告警为开发期风险，或单独排期升级 Vitest 5.x。
3. **剩余 P3**：FAV-01 favicon 404、草稿/最后一手微动效、少量文案微调；由 PM 决定接受为遗留或排期修复。

若上述条件未获接受，则不应给出无条件发布结论。**最终发布决策仅由 PM 在 S10 给出。**

---

## 10. 范围与合规

- S8 仅新增/更新测试产物与报告文件（`docs/acceptance-report.md`、`test-results/**`），**未修改 `src/**`、需求文档或设计文档**。
- 被测对象为最终 `dist/` 构建；E2E 与 Chrome 冒烟均通过静态服务器加载该产物。
- 测试钩子仅 `?e2e=1` 启用，生产入口不暴露 `window.__CC_TEST__`（E2E-HOOK-01/02）。
