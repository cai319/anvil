# 跳棋程序 项目计划与协作安排

- 任务：`5523825a-d074-464c-811b-c93cd7a1736a`
- 协调人：PM
- 参与角色：designer / developer / tester / PM

## 1. 现状盘点

| 阶段 | 产出 | 位置 | 状态 |
|---|---|---|---|
| 需求 | PRD、验收清单 | `agents/pm/docs/requirements.md`、`docs/acceptance.md` | ✅ PM 完成 |
| 设计 | 《跳棋程序设计文档》v1.0（规则契约、坐标、接口、算法、测试策略） | `agents/designer/docs/design.md` | ✅ designer 完成 |
| 参考实现 | 6 个模块 + 3 个测试文件 + 45 个单测 | `agents/designer/checkers/`、`agents/designer/tests/` | ✅ designer 完成（run 失败但产物齐全） |
| 实现基线 | 以 developer 复核后的代码为唯一基线 | `agents/developer/checkers/` | 🔄 待 developer |
| 独立测试 | 验收报告 | `agents/tester/` | ⏳ 待 tester |

## 2. 角色分工（RACI）

| 事项 | PM | designer | developer | tester |
|---|---|---|---|---|
| 需求定义与范围冻结 | **A/R** | C | I | I |
| 规则与架构设计 | A | **R** | C | I |
| 参考实现 | I | **R** | C | I |
| 正式实现与代码复核 | A | C | **R** | I |
| 独立验收测试 | A | I | C | **R** |
| 最终提交与人类评审 | **A/R** | I | I | I |

> R=执行，A=负责，C=咨询，I=知会。

## 3. 交付流程

1. **M1 需求（PM，已完成）**：明确棋种、MVP 范围、验收标准。
2. **M2 设计（designer，已完成）**：输出设计与接口契约，附参考实现。
3. **M3 实现（developer，进行中）**：
   - 以 designer 参考实现为起点，建立 developer 工作区的正式实现；
   - 复核规则引擎、CLI 交互与异常处理；
   - 补齐 README（安装、运行、操作、测试）；
   - 运行全部单测与自动对局；
   - 完成后 `anvil task mention --to pm` 回报，不提交共享任务。
4. **M4 测试（tester，待 M3 交付）**：
   - 按 `docs/acceptance.md` AC-1 ~ AC-14 独立验证；
   - 输出测试报告（含命令、输出、失败复现步骤）；
   - 完成后 `anvil task mention --to pm` 回报，不提交共享任务。
5. **M5 验收（PM）**：汇总证据 → 修复阻塞项 → `anvil task submit` → @human 评审。

## 4. 协作约定

- 共享任务已由 designer 认领且在 `in_progress`，状态切换由 PM 统一执行；其他角色**不要**对共享任务执行 `claim` / `submit`，避免状态竞争。
- 各角色在自身工作目录产出；designer 参考实现作为只读输入。
- 关键节点用 `anvil task comment` 同步；需要对方行动时用 `anvil task mention --to <角色>`。
- 发现阻塞或规则歧义立即回传 PM，由 PM 决策或升级人类。

## 5. 时间与里程碑

| 里程碑 | 目标 | 状态 |
|---|---|---|
| M1 需求 | 需求冻结 | ✅ |
| M2 设计 | 设计文档 + 参考实现 | ✅ |
| M3 实现 | 可运行基线 + README + 单测通过 | 🔄 |
| M4 测试 | 独立验收报告 | ⏳ |
| M5 交付 | PM 提交、人类评审 | ⏳ |

## 6. 风险与对策

- **实现分叉**：只保留 developer 工作区一份正式代码，designer 目录仅作参考。
- **共享任务状态竞争**：状态变更权收归 PM。
- **AI 残局不收敛**：保留 `--max-plies` 上限；AI 增强列入 Backlog。
