# GLOSSARY

Anvil 领域术语。实现、issue、测试与文档应使用这里的词汇；不要另造同义词。

## 澄清与规划

### 任务简报（Intent Brief）

Cindy 在首次规划前与 human 共同收敛出的结构化任务意图记录。包含目标、真实意图、
非目标、约束、验收标准、假设、开放问题、流程强度与计划形态。DB 中是
`tasks.intent_brief_json`，版本历史在 `task_brief_versions`；任务工作区投影为
`.anvil/intent-brief.json` / `.md`。human 可编辑，确认后作为 planner 的唯一意图源。

避免说法：需求文档、PRD、任务描述（这些是输入，不是简报）。

### 理解契约（Understanding Contract）

澄清收束的判断标准：Cindy 能完整复述目标、真实意图、非目标、约束、验收标准、
投入强度、已知假设与风险，并得到 human 明确确认。开放问题存在时必须由 human
显式接受，才可确认。

### 澄清阶段（Clarification）

Cindy 首次规划前的对话阶段，状态存于 `tasks.clarification_status`：
`none | in_progress | confirmed | skipped | stale`。澄清阶段不产出 workflow；
未确认时服务端拒绝 workflow / workflow_revision 草案。

### 澄清 run（Clarify Run）

以 `[anvil:clarify]` 标记启动的 Cindy run，只负责提问、更新简报草稿、@human 挂起。
不包含 `PLANNING_PROTOCOL`，不创建草案。每次 human 回复启动新一轮，轮次上限 6，
第 6 轮起 `must_conclude=true`。

### 首次规划阶段（First Planning Phase）

Cindy 担任 planner、任务尚未产生任何 workflow、尚未执行、状态为 `todo` 的规划阶段。
只有这个阶段会触发澄清。已有 workflow、已执行、非 todo 的任务永不进入澄清。

### 简报过期（Stale）

已确认/跳过的任务定义发生材料性变更（标题、描述、type、repo、scope_paths、
acceptance、base_branch），且任务尚未产生 workflow 时，`clarification_status`
变为 `stale`，规划门禁重新生效，需要重新确认或跳过。tags 不算材料性变更。

### 计划形态（Plan Shape）

简报字段 `plan_shape`：`flat`（扁平静态步骤）、`slices`（plan_slices + loop）、
`auto`（由 Cindy 按 scope/审计启发式决定）。human 在简报卡确认。

## 流程强度

### 流程强度（Workflow Weight）

执行严格度：`light | standard | strict`，中文展示为「轻量 / 标准 / 严格」。
现有实现：`tasks.workflow_weight` 人工覆盖、Cindy 草案 `weight` + `weight_rationale`、
`weights.resolve()` 优先级 human > Cindy > 启发式。澄清阶段确认后写入
`workflow_weight`（source=human）；工作流页的调整入口降级为「高级 → 仅影响下一次执行」。

避免说法：投入档位（口语可用，但文档统一写流程强度）。

### 严格度下限

流程强度对 workflow 的最低要求：证据必填、人工门、验证密度等。强度不强制决定
是否切片；切片仍由 scope/审计启发式与简报的 `plan_shape` 决定，避免
「为了严格而切片」。

## 运行与门禁

### 规划门禁（Clarification Gate）

服务端规则：`clarification_status in (in_progress, stale)` 时，拒绝创建
`workflow` / `workflow_revision` 草案，并拒绝直接执行 run。保证未确认简报
不可能产出 workflow。已有 workflow 的修订不受此门禁影响。

### 已运行任务（Executed Task）

已经产生过任何 workflow、或已有执行 run、或状态非 `todo` 的任务。澄清子系统对
这类任务完全惰性：不触发、不判 stale、不设门禁、不改变现有 dispatch / revision /
re-plan 行为。

## 角色

### Cindy

Anvil 的内置 onboarding planner（slug `cindy`）。只规划、不实现；不能成为任务成员
或 workflow 步骤执行角色。v1 的澄清协议是 Cindy 专属；其他角色由用户自行在角色
定义里选择 skill。

### 思考强度（Thinking Effort）

角色级 runtime effort：`low | medium | high | max`，存于
`definition.runtime.effort`，展示为 低 / 中 / 高 / 最高。Cindy 默认 `max`，
其他角色默认 `high`。Claude Code 通过 `--effort` 传入；不支持
`thinking_effort` 能力的运行时忽略该值。入口：角色 → 运行时 → 思考强度。
决策见 ADR-0002。
