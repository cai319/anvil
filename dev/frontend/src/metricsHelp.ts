/**
 * Customer-facing metric definitions. Keep these in sync with the backend
 * calculations in `services/metrics.py`.
 */
export const METRIC_TIPS = {
  steps:
    "已完成步骤 / 总步骤数。完成包含 approved 与 skipped；运行中、待审、失败和待办步骤不计入完成。",
  maxDepth:
    "工作流依赖图的最大深度。深度越大，串行等待链越长；同一深度的步骤可以并行。",
  taskDuration:
    "任务创建到最后一次更新的墙钟时间。包含 workflow 创建前的规划，以及 workflow 结束后的最终人工验收等待。",
  workflowDuration:
    "当前 workflow 从创建到完成/终止的墙钟时间。只覆盖这次执行，不含任务创建前和 workflow 结束后的时间。",
  activeDuration:
    "工作流耗时扣除纯人工等待后的有效执行时间。只扣除没有任何角色在推进、系统明确等待自然人处理的区间；并行等待只扣一次，仍有人/agent 在工作的时段不扣。",
  humanWait:
    "workflow 内纯等待自然人处理的时长：gate 已提交待审、人工决策步骤、AI 升级人工等。并行等待会合并去重；workflow 结束后的任务最终验收不计入此值。",
  gates:
    "workflow 中 gate=true 的步骤数。审核门要求角色提交后由自然人通过或驳回；任务级最终评审不计入。",
  decisions:
    "决策步骤的最终结果数。新执行来自不可变 decisions 记录；历史执行没有记录时，按 decision 步骤的最终状态和 rework_count 推算（界面会单独提示）。",
  humanInterventions:
    "自然人实际介入次数 = workflow 人工决策次数 + 任务最终人工评审次数。AI 自动决策不计入。",
  rework:
    "决策打回次数。新执行来自不可变决策记录；历史执行按 decision 步骤的 rework_count 推算。",
  taskTokens:
    "该任务所有 run 的 Token 总量 = 输入 + 输出 + 缓存读取 + 缓存写入。包含规划、最终验收、重试及未直接挂在步骤上的常驻会话；是任务视角的总消耗。",
  executionTokens:
    "仅直接挂在 workflow 步骤上的 run 的 Token 总量 = 输入 + 输出 + 缓存读取 + 缓存写入。它是任务 Token 的子集，不包含规划、最终验收等未挂步骤的 run。",
  generatedSlices:
    "运行期由 plan_slices 展开生成的切片步骤数。计划图里没有这些节点，它们会出现在 planned/actual diff 中。",
} as const;
