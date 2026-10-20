# 中国跳棋（2 人 Web 版）S10 需求验收与发布评审

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 任务 | S10「需求验收与发布评审」 |
| 父任务 | `28e915e5-6e96-48a0-88d0-b446c1a77e82`「开发一个跳棋小程序」 |
| 工作流 | `40d55cd5-f559-4a7f-999f-eeaa20eac377` |
| 评审对象 | 最终构建产物 `dist/`（S8 验收构建 `e5c7fe64…`；FAV-01 修复后将更新哈希） |
| 需求基线 | `docs/prd.md` v1.0、`docs/acceptance-criteria.md` v1.0（AC-01 ~ AC-51） |
| 设计/接口基线 | `docs/design-spec.md` v1.0、`docs/interface-contract.md` v1.0 |
| 输入证据 | S6 `docs/test-report.md`、S8 `docs/acceptance-report.md`、S9 `docs/ux-review.md`、PM 独立复核 |
| 评审日期 | 2026-10-19 |
| 评审人 | PM |
| 发布决策 | **有条件发布（Conditional Release）**，条件见 §5；human 审批前不视为最终放行 |

---

## 1. 执行摘要

### 1.1 结论

本次评审的结论为 **有条件发布**：

- **质量门槛已满足**：无未关闭 P0/P1 缺陷；全部 P2 已修复并有回归；单元 191/191、E2E 66/66（Chromium 33 + WebKit 33）、真实 Chrome 155.0.8059.80 实机冒烟通过；AI 首手实测 400ms（≤2000ms）。
- **验收项**：AC-01 ~ AC-51 共 51 条，**50 条通过、1 条部分（AC-50）、0 条未通过**。AC-50 为 P1「三浏览器人工冒烟」，因无头环境未执行 Edge/Safari 人工冒烟，已用 Chromium/WebKit 双引擎 E2E + 真实 Chrome 实机自动化作为替代证据，需 human 明确接受该偏离。
- **发布前必做**：FAV-01（P3，favicon 404 控制台噪音）按 PM 决定 **发布前修复**，不作为遗留接受；当前 `dist/`（`e5c7fe64…`）不是最终发布产物，修复后需重建并复跑 191/191 + 68/68，由 tester 复核并更新验收报告。
- **P3 接受/排期**：SEC-01（dev-only 审计告警）接受并安排下一迭代升级 Vitest 5.x；剩余 S9 P3（草稿/最后一手微动效、少量文案）接受为 backlog；AC-45 的 `src/engine`+`src/types` 等价分层接受为文档对齐项。
- **若条件未满足**：不得给出最终发布结论；若 human 不接受 AC-50 替代证据，则应在发布前完成真实 Edge/Safari 人工冒烟后再放行。

### 1.2 关键数字

| 项目 | 结果 |
|---|---|
| 单元/控制器（Vitest） | **191 / 191**（8 文件，PM 本地复跑 191/191） |
| E2E（Playwright） | **66 / 66**（Chromium 33 + WebKit 33） |
| 真实 Chrome 实机 | **通过**（Chrome 155.0.8059.80；AI 首手 400ms） |
| Typecheck / Build | 通过（TypeScript 5.9.3 strict；Vite 7.3.7） |
| P0/P1 缺陷 | **0** |
| 未关闭 P2 | **0**（D-01、S9 P2-01~05 全部关闭/验证） |
| AC 通过 | **50 / 51**；AC-50 部分（P1，待 human） |
| 运行时安全 | `npm audit --omit=dev` = **0**；3 项告警均为 dev-only（vitest/tinypool） |
| 外部网络依赖 | 0（dist 仅同源静态资源；唯一 `http://www.w3.org/2000/svg` 为 SVG 命名空间，不产生网络请求） |

---

## 2. 评审方法与独立复核

### 2.1 评审输入

1. **S8 集成验收报告**：`docs/acceptance-report.md`（逐条 AC、环境/构建哈希、截图/日志索引、发布建议）。
2. **S9 UX/视觉走查**：`docs/ux-review.md`（无 P0/P1；P2 5 项；P3 9 项；修正建议）。
3. **S6 测试报告**：`docs/test-report.md`（缺陷 D-01 等）。
4. **S7 修复与回归**：developer 报告 + tester 复核记录（P2 全部关闭）。
5. **PM 独立复核**（本轮执行）：
   - `npm test` 在 developer 最终工作区复跑：**191/191**。
   - `npm audit --omit=dev`：**0 漏洞**；`npm audit` 全量：1 moderate + 2 critical，均在 devDependencies（vitest/tinypool），`dist` 不含。
   - `dist/index.html`、`dist/assets/index-DaOPTblc.js`、`dist/assets/index-Z-WM6PhP.css` 的 sha256 与 S8 报告逐项一致。
   - 真实 Chrome 冒烟日志复核：121 孔 / 20 子、先手/后手、AI 首手 400ms、非法提示、重开、刷新、红胜/蓝胜/平局；唯一 Console error 为 `/favicon.ico` 404（FAV-01）。
   - `dist/index.html` 无 favicon 声明，确认 FAV-01 可复现；`dist` 无外部网络依赖。

### 2.2 评审边界

- 本次评审基于 S8 的 **最终构建产物** 与 S9 的 **冻结构建** 证据；FAV-01 修复后将产生新的 `dist`，需以新构建的哈希与 tester 复核结果替换本文件 §2.1 中的构建证据。
- 本次评审不重新执行完整 300 手自然对局；AI 时限/合法性由 S6/S8 自动化用例覆盖。
- 人工三浏览器冒烟的浏览器环境限制见 AC-50；PM 不将其视为“已通过”，而是作为需 human 接受的 P1 偏离。

---

## 3. 需求验收逐条结论

### 3.1 P0 验收项

**结论：全部通过（无未通过项）。**

依据 S8 报告与 PM 复核，以下 P0 项全部通过：

- 棋盘/规则/状态机：AC-01、AC-02、AC-06 ~ AC-16。
- 流程/交互/终局：AC-03、AC-04、AC-05、AC-17 ~ AC-22、AC-24、AC-26。
- AI 硬约束：AC-30 ~ AC-36、AC-41。
- 技术/交付/测试：AC-42、AC-43、AC-44、AC-48、AC-49。
- 基线：AC-51（S1 基线继承通过）。

未发现无法开始对局、非法落子被接受、胜负判定错误、AI 卡死或 AI 非法着法等 P0。

### 3.2 P1 验收项

| AC | 结论 | 说明 |
|---|---|---|
| AC-23 非法点击提示 | 通过 | E2E-ILLEGAL-01/02/03；Chrome 实机提示 |
| AC-25 返回设置 | 通过 | E2E-RESTART-03/05、UT-CTRL-11 |
| AC-27 响应式 | 通过 | E2E-RESP-01、E2E-S7-03/04；Chrome 360×640 实测 |
| AC-28 稳定选择器 | 通过 | 全部 E2E 使用 `data-testid` |
| AC-37 连续降级切简单 | 通过 | UT-CTRL-16、E2E-S7-05 |
| AC-38 AI 一步获胜 | 通过 | UT-AI-07（夹具 A） |
| AC-39 AI 必挡 | 通过 | UT-AI-08（夹具 B） |
| AC-40 可注入策略/种子/时钟 | 通过 | UT-AI-04/05/06/09 |
| AC-45 目录分层 | 通过（等价分层） | `src/engine`+`src/types` 承担 core 契约逻辑，`src/ai`/`src/ui` 分离；engine/ai 无 DOM，Node 可单测；PM 接受为文档对齐项（P3） |
| AC-46 测试钩子 | 通过 | E2E-HOOK-01/02 双浏览器 |
| AC-47 README | 通过 | 安装/命令/规则/操作/AI/浏览器/限制章节齐全 |
| **AC-50 三浏览器手工冒烟** | **部分（P1，需 human 接受）** | 真实 Chrome 155.0.8059.80 实机自动化通过；Edge 未安装、Safari 无 GUI 手工环境；以 Chromium 33/33 + WebKit 33/33 作为替代证据。**PM 建议接受该环境限制，条件见 §5** |

> P1 结论：除 AC-50 外全部通过；AC-50 有明确结论（部分 + 替代证据 + human 决策），不存在“未解释的 P1”。

### 3.3 P2 验收项 / 设计走查

**结论：全部关闭，无未关闭 P2。**

| 编号 | 级别 | 状态 | 验证 |
|---|---|---|---|
| D-01（S6）board aria-busy/aria-disabled | P2 | 已关闭 | E2E-LOCK-01 双浏览器通过；board 与 svg 同步 |
| S9 P2-01 连跳条改变棋盘尺寸 | P2 | 已修复/已验证 | E2E-S7-01（1280×720 前后尺寸一致） |
| S9 P2-02 降级提示越出首屏/压页脚 | P2 | 已修复/已验证 | E2E-S7-05（notice 在 turn-indicator 内，bottom<footer top） |
| S9 P2-03 右栏卡片间距 71px | P2 | 已修复/已验证 | E2E-S7-02（1440×900 间距 16px） |
| S9 P2-04 360×640 开始 CTA 折叠以下 | P2 | 已修复/已验证 | E2E-S7-03；Chrome 实测 start bottom=443.8、height=44 |
| S9 P2-05 移动端结束回合 <44px | P2 | 已修复/已验证 | E2E-S7-04（360×640 height≥44） |

### 3.4 P3 与遗留风险

| 编号 | 级别 | 当前状态 | PM 决策 |
|---|---|---|---|
| **FAV-01** favicon 404 控制台噪音 | P3 | **Open；PM 决定发布前修复** | 在 `index.html` 加 `<link rel="icon" href="data:," />`；重建 dist；新增 `tests/e2e/console-clean.spec.ts`；复跑 191/191 + 68/68；tester 复核并更新 acceptance-report。**当前 dist e5c7fe64 不是最终发布产物。** |
| **SEC-01** devDependency 审计 | P3 | Open（dev-only） | 接受；`npm audit --omit=dev` = 0，3 项（1 moderate + 2 critical）均在 vitest/tinypool 测试链，`dist` 不含；下一迭代单独升级 Vitest 5.x（breaking，需完整回归），不在本次发布执行 `npm audit fix --force`。 |
| **剩余 S9 P3** | P3 | Open（cosmetic） | 接受为 backlog：草稿/最后一手微动效、少量文案微调；不影响功能、可访问性与核心验收。 |
| **AC-45 目录命名** | P3 | 文档对齐 | `src/engine`+`src/types` 等价实现 core 分层；PM 接受，建议后续在 README/设计文档中统一表述。 |
| **ENV-02 / AC-50** | P1（验收项） | 部分 | 见 §3.2；需 human 接受替代证据或要求发布前人工冒烟。 |

---

## 4. 发布证据摘要

### 4.1 构建与哈希（S8 验收构建，FAV-01 修复前）

| 文件 | 大小 | sha256 |
|---|---:|---|
| `dist/index.html` | 464 B | `e5c7fe64ca38eb84f35dfea01b00971eb9d2cb8533c663b0d6c30ab26bffee2e` |
| `dist/assets/index-DaOPTblc.js` | 63.21 kB | `c6876ce09796b8d56fdb473a611fd8f16b9d97015ce0b3cf5bcebd9b1b5a5356` |
| `dist/assets/index-Z-WM6PhP.css` | 14.52 kB | `c278630f54c4dac36f27000fe20f1959db7ff2bc46c7e09380552254cf152a94` |

> FAV-01 修复后必须重新构建并更新本表；旧哈希仅对应 S8 验收构建，不得作为最终发布产物的哈希。

### 4.2 关键路径

- 人类先手完整流程、人类后手/AI 自动首手、SETUP→AI_THINKING 同步迁移。
- 单步、跳跃、连跳、结束回合、最后一步、非法走子拦截。
- AI 三档合法性与时限、超时/异常/非法输出兜底、连续降级。
- 红胜/蓝胜/平局、终局封盘、重开、返回设置、AI 思考中重开/返回的旧结果丢弃。
- 零外部请求、360×640/1280×720 响应式、测试钩子隔离。
- 证据索引见 S8 `docs/acceptance-report.md` §8 与 `test-results/**`。

---

## 5. 发布决策与条件

### 5.1 决策

**有条件发布（Conditional Release）。**

PM 建议：在下列条件全部满足后放行；条件未满足前不视为最终发布，不得对外宣告“已发布”。

### 5.2 发布条件

1. **FAV-01 发布前修复（P3，必须）**
   - `index.html` `<head>` 增加 `<link rel="icon" href="data:," />`（零网络请求；不新增 `public/` 资源）。
   - 新增 `tests/e2e/console-clean.spec.ts`：主断言 `console type=error` 与 `pageerror` 计数为 0；正向断言 `link[rel~="icon"]` 存在且 href 以 `data:` 开头；404 response 仅作补充；`/` 与 `/?e2e=1` 各等待 ≥800ms，并在 `?e2e=1` 执行一次开局 + 人类落子 + AI idle；双浏览器。
   - 重建 `dist`；复跑 `npm test` 191/191 与全量 E2E 68/68（Chromium 34 + WebKit 34）；提供新的 index.html/JS/CSS sha256。
   - tester 在新 dist 上独立复核并更新 `docs/acceptance-report.md`（哈希、AC-03、FAV-01=Fixed），向 PM 回执。
   - **当前 dist `e5c7fe64…` 不是最终发布产物。**

2. **AC-50（P1）human 接受（必须）**
   - 接受以 Chromium 33/33 + WebKit 33/33 + 真实 Chrome 155.0.8059.80 实机自动化替代“三浏览器人工冒烟”的偏差。
   - 条件：**发布后 7 天内或公开发布前（以较早者为准）** 补做真实 Edge + Safari 人工冒烟，并登记责任人与截止时间；若发现 P0/P1，立即 hotfix/回滚评估，该条件视为未满足。
   - 若 human 不接受该替代证据，则应在发布前完成人工三浏览器冒烟并记录通过后再放行；在此之前发布决策为“不发布/暂缓”。

3. **P3 接受/排期（必须明确）**
   - **SEC-01**：接受为 dev-only 风险（`npm audit --omit=dev` = 0；dist 不含受影响包）；下一迭代单独升级 Vitest 5.x，执行完整回归。
   - **剩余 S9 P3**：接受为 backlog（草稿/最后一手微动效、少量文案微调、AC-45 文档对齐）；不阻塞发布。
   - 以上接受项需在 human 审批记录中明确写入。

### 5.3 不建议的选项

- **无条件发布**：AC-50 为 P1 且仅部分执行，FAV-01 修复尚未完成；不满足“无条件发布”的证据完整性要求。
- **不发布**：当前无 P0/P1、无未关闭 P2，自动化与 Chrome 实机证据充分；无技术性阻断理由。仅在 human 不接受 AC-50 替代证据且要求发布前人工冒烟时，暂缓至人工冒烟完成。

---

## 6. 遗留风险与后续建议

| 风险/遗留 | 等级 | 影响 | 处置 | 责任/时限 |
|---|---|---|---|---|
| FAV-01 favicon 404 | P3 | 仅控制台噪音；可能让用户误以为页面有错误 | 发布前修复 + console-clean 回归 | developer/tester；发布前 |
| AC-50 人工三浏览器未执行 | P1 验收偏离 | 真实 Edge/Safari 的 UI/输入差异未被人工覆盖 | human 接受替代证据；发布后 7 天内或公开发布前补做真实 Edge/Safari 冒烟；P0/P1 则 hotfix/回滚 | human 裁决；PM/tester 执行记录 |
| SEC-01 dev-only 审计 | P3 | 仅开发/测试工具链；不影响 dist | 下一迭代升级 Vitest 5.x（breaking，完整回归） | developer；下一迭代 |
| 剩余 S9 P3 | P3 | 草稿/最后一手微动效、少量文案 | 接受为 backlog 或后续补丁 | PM/designer；后续迭代 |
| AC-45 目录命名 | P3 | 文档与实现的 core/engine 命名不一致 | 文档对齐；不影响分层与测试 | PM/designer；后续文档更新 |
| 子路径部署（备注） | P3 | 若部署在非根路径，Vite 绝对资源路径可能失效 | 部署时使用根路径，或按需设置 Vite `base` | developer/运维；部署前确认 |

**后续建议**

1. 完成 FAV-01 修复与 tester 复核后，更新本文件 §4.1 的哈希与 §5.2 条件 1 的状态为“已满足”。
2. 发布后按条件 2 执行真实 Edge/Safari 人工冒烟，记录浏览器版本、日期、结果；发现 P0/P1 立即 hotfix/回滚。
3. 下一迭代安排 Vitest 5.x 升级（SEC-01），升级后完整回归 191+ 单测与 68+ E2E。
4. 将剩余 S9 P3 与 AC-45 文档对齐纳入 backlog；不阻塞本次发布。
5. 发布前确认最终 `dist` 哈希与 tester 复核证据一致；发布后在任务会话归档最终哈希与人工冒烟记录。

---

## 7. 发布检查清单

- [x] 全部 P0 验收项通过。
- [x] P1 项均有明确结论（AC-50 部分，需 human）。
- [x] 无未关闭 P0/P1 缺陷。
- [x] P2 全部关闭并有回归。
- [x] 单元 191/191；E2E 66/66（Chromium 33 + WebKit 33）；真实 Chrome 实机通过。
- [x] dist sha256 与 S8 报告一致（PM 复核）。
- [x] `npm audit --omit=dev` = 0；SEC-01 确认为 dev-only。
- [ ] FAV-01 修复 + 新 dist + 191/191 + 68/68 + tester 复核（发布前必须）。
- [ ] human 对 AC-50 替代证据与 P3 接受/排期作出审批（发布前必须）。
- [ ] 发布后 7 天内或公开发布前完成真实 Edge/Safari 人工冒烟（发布条件）。

---

## 8. 证据索引

| 证据 | 路径 |
|---|---|
| S8 集成验收报告（逐条 AC、哈希、日志索引） | `docs/acceptance-report.md` |
| S9 UX/视觉走查报告 | `docs/ux-review.md` |
| S6 测试报告 | `docs/test-report.md` |
| S8 单元日志（191/191） | `test-results/S8-unit-final.log` |
| S8 E2E 日志（66/66） | `test-results/S8-e2e-final.log` |
| Chrome 实机冒烟 | `test-results/acceptance-chrome-smoke.log` |
| Chrome 蓝胜/平局 | `test-results/acceptance-chrome-win-draw.log` |
| Chrome 404/刷新检查 | `test-results/acceptance-chrome-404-check.log` |
| Chrome 移动端指标 | `test-results/acceptance-chrome-mobile.log` |
| S7 回归日志 | `test-results/verify-S7-*.log` |
| 验收截图 | `test-results/acceptance/chrome-*.png` |

> 上述路径以 tester 工作目录 `data/agents/tester/` 为基准；最终发布时以新 dist 的哈希与 tester 复核日志为准。

---

## 9. 范围、签署与升级记录

### 9.1 范围合规

- 本步骤仅新增/更新 `docs/release-review.md`。
- 未修改 `src/**`、测试文件、需求文档或设计文档。
- FAV-01 的代码修复由 developer 在单独工作轮执行，tester 复核；PM 不直接修改实现。

### 9.2 签署

| 角色 | 结论/签署 | 日期 |
|---|---|---|
| PM | 有条件发布（条件见 §5.2） | 2026-10-19 |
| tester | S8 证据与 AC 结论已复核；同意有条件发布；FAV-01 修复后复跑 | 2026-10-19 |
| developer | FAV-01 修复方案已对齐；等待工作轮执行 | 2026-10-19 |
| designer | S9 走查通过（无 P0/P1）；P2 已关闭；剩余 P3 由 PM 决定 | 2026-10-19 |
| human | **待审批**：AC-50 替代证据、SEC-01/剩余 P3 接受、发布条件 | — |

### 9.3 升级记录

- 按项目发布规则，AC-50（P1）为部分执行，属于“验收项不通过/需接受偏离”的情形，已升级 human 决策。
- PM 建议：接受替代证据 + 发布后人工冒烟条件；若 human 不接受，则发布前完成人工三浏览器冒烟。
- FAV-01 已由 PM 决定发布前修复；修复完成与 tester 复核通过后，更新本文件 §4.1 与 §5.2，并执行 `anvil step submit` 完成 S10。

---

**当前状态：等待 human 对 AC-50 与 P3 接受/排期作出审批；等待 FAV-01 修复工作轮完成并复核。条件满足后，本文件将更新为最终放行版本。**
