# 跳棋 Web 项目恢复说明

本项目是 2026-10-19「开发一个跳棋小程序」任务（任务 ID `28e915e5…`）的 **中国跳棋（2 人 Web 版）** 源码恢复版本。

## 恢复来源

- Claude Code 会话转录（developer）：`~/.claude/projects/-Users-ikaruga-cctest-aliceAgent-data-agents-developer/*.jsonl`
- Claude Code 会话转录（tester）：`...-data-agents-tester/*.jsonl`
- 时间窗口：2026-10-19 03:50–07:30 UTC（跳棋工作流 S1–S10 执行期间）
- 方法：按时间顺序回放 `Write` / `Edit` 工具调用；测试用例与 helpers 取自 tester 会话，PM/设计/Cindy 文档从各自 home 复制。

## 已恢复

- 完整源码：`src/engine`、`src/ai`、`src/ui`、`src/types`、`src/styles`、`index.html`、`vite/tsconfig/playwright` 配置、`package.json`
- 单元测试：`src/**/*.test.ts`（45 个用例）
- E2E：`tests/e2e/*.spec.ts` + `helpers.ts`（tester 提供的 S5 测试基线）
- 文档：`docs/`（PRD、验收、测试计划/报告、设计规格、UX 走查、工作流草案）

## 验证结果

```bash
npm install
npm run typecheck   # 通过
npm test            # 3 个测试文件，45/45 通过
npm run build       # 通过，产物在 dist/
```

## 已知差异

- 原始 `dist/`、测试截图、node_modules 未恢复（按源码重建的 `dist/` 已生成）。
- `tests/e2e/second-player.spec.ts` 由 tester 的初始版本 + developer 的计时口径修正编辑合成。
- 运行 E2E 前需安装浏览器：`npx playwright install chromium webkit`。

## 运行

```bash
cd /Users/ikaruga/cctest/aliceAgent/recovered/checkers-web
npm run dev        # 开发服务器
npm run build && npm run preview
```
