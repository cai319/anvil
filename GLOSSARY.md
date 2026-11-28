# GLOSSARY

Anvil 领域术语。实现、issue、测试与文档应使用这里的词汇；不要另造同义词。

## 澄清与规划

### 任务简报（Intent Brief）

Cindy 在首次规划前与 human 共同收敛出的结构化任务意图记录。包含目标、真实意图、
非目标、约束、验收标准、假设、开放问题、流程强度与计划形态。DB 中是
`tasks.intent_brief_json`，版本历史在 `task_brief_versions`；任务工作区投影为
`.anvil/intent-brief.json` / `.md`。human 可编辑，确认后作为 planner 的唯一意图源。
决策见本地 ADR-0001（`docs/adr/`，不随仓库发布）。

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
不包含 `PLANNING_PROTOCOL`，不创建草案。每次 human 回复都会启动新的 clarify run，
但 run 本身不消耗轮次：**只有 Cindy 主动发出一批结构化 grill 问题才计一轮，软上限 10 轮**，
每轮 1–3 问；human 的主动发言、追问和修正不计数。简报足够完整可提前收束，
已提出 10 轮问题后 `must_conclude=true`。

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
`workflow_weight`（source=human）；工作流页的调整入口降级为「高级：调整流程强度」，
只影响下一次执行。

避免说法：投入档位（口语可用，但文档统一写流程强度）。

### 严格度下限

流程强度对 workflow 的最低要求：证据必填、人工门、验证密度等。强度不强制决定
是否切片；切片仍由 scope/审计启发式与简报的 `plan_shape` 决定，避免
「为了严格而切片」。

## 验收检查

### 红校验（Red Check）

在切片锁定时于 base 提交上执行检查，确认目标行为尚未满足。红校验只认断言失败；
环境、导入/收集、runner 用法、超时或信号导致的失败不算红。

### 合法红（Valid Red）

检查因目标行为未满足而以断言失败告终。它是合格检查的判定条件；平凡的文件缺失红
不计入合法红。

### harness 错误（Harness Error）

检查因环境、导入/收集、runner 用法、超时或信号而失败，不能证明目标行为未满足。
harness 错误不计入合法红。

### 合格检查（Qualified Check）

在 base 上得到合法红的检查。它是有效红占比的分母；试点阶段一对它有最小样本要求。

### 有效红

合格检查在 final 上转为通过且签名匹配。有效红占比 = 有效红数 ÷ 合格检查数。
接口不匹配与行为不符合分开统计。

### 锁定集合（Lock Set）

scope 锁定时固定、execute 与 fix 不得修改的检查文件、运行器配置、测试依赖组与
stub 签名的集合。execute 的 `scope_paths` 必须与锁定集合不相交。

### enabling 切片（Enabling Slice）

不直接对应 Brief 验收、为后续切片铺路的切片（预重构、expand 步骤）。必须在 plan
人工审批时声明，运行时不得追加；检查为现有测试保持通过 + 声明的下游切片存在；
scope_paths 不得与 Brief 的 non-goals 冲突。

### 弱化 replan（Weakening Replan）

删除检查、把检查降为 manual、或修改断言但没有新的红证据的 replan。无论流程强度
都需要人工批准。

### 验收证据（Acceptance Evidence）

引擎执行检查后记录的结构化运行结果。只有验收证据能作为 decide 判定验收项的依据；
agent 提交的说明与 agent 上下文里的运行输出不算验收证据。它与
`workflow_steps.evidence_json` 中 agent 提交的证据不是同一概念。

避免说法：把 agent 自报的「已通过」、日志片段或上下文里的输出叫作验收证据。

## 运行与门禁

### 规划门禁（Clarification Gate）

服务端规则：`clarification_status in (in_progress, stale)` 时，拒绝创建
`workflow` / `workflow_revision` 草案，并拒绝直接执行 run。保证未确认简报
不可能产出 workflow。已有 workflow 的修订不受此门禁影响。

### 角色建议（Role Suggestion）

planner/Cindy 在任务缺少能力时提交的 `role_suggestion` 草案。`apply_mode` 有两种：

- `create_bind`（历史缺省也按此处理）：批准时创建缺失角色并绑定到任务；
- `authorize`：批准只授权，由 planner 后续 `role expand` + `task assign` 创建/绑定。

非法 `apply_mode` 返回 409。`create+bind` 视为 `create_bind` 的别名。

### 角色门禁（Role Suggestion Gate）

工作流审批前的角色约束：pending `role_suggestion` 会阻塞 workflow 与
workflow_revision 审批；已批准但未消费的 `authorize` 建议还要求角色已创建并绑定，
否则 workflow 审批保持锁定。`workflow_revision` 只受 pending 建议阻塞，避免修订
引入新角色时死锁。

### 收件箱 @human 生命周期（Human Mention Lifecycle）

`@human` 提醒按 `(task_id, role_slug)` 各自保留最新一条；只有该提醒之后
`role_slug=human` 的活动才算回应，agent/system/workflow 的评论不清除。简报确认/
跳过、board 草案批准/驳回、任务关闭/归档，以及失败事件已解决时，对应提醒自动隐藏。
任务进入 `in_review` 后，最终评审/问题板条目是唯一收件箱待办；`@human` 消息仍保留在
任务时间线，但不再单独生成 mention 条目。

## 界面与工具治理

### 工具子页（Tooling Tabs）

`/tooling` 的四个查询参数子页：`config`（工具配置，默认）、`import`（工具导入）、
`flags`（功能开关）、`proposals`（运行时提案）。`?tab=` 记忆当前子页；旧
`?rip=<id>` 兼容为 `proposals` 并选中提案。

### 功能开关覆盖（Feature Override）

功能开关的持久化覆盖存于 `settings`。把开关切回默认值会删除覆盖记录并返回
`overridden=false`，界面恢复「默认开启/关闭」，不再显示「已覆盖」。

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
决策见本地 ADR-0002（`docs/adr/`，不随仓库发布）。

## 执行沙箱

### 执行沙箱（Execution Sandbox）

包裹整个角色进程（agent CLI 及其工具/shell）的 OS 级边界。Anvil 控制器留在
边界之外；沙箱负责写范围、资源上限、进程树回收与秘密读拒绝。沙箱不是 Anvil
权限系统的替代：`bypassPermissions` 与单一 human token 模型不变。

避免说法：把任务工作区隔离、知识隔离、mock 试运行（workflow-editor 的
「Sandbox Run」）叫作执行沙箱。

### 标准档（Standard Tier）

普通角色 run 与工作流步骤的隔离档。保证工作区写范围、资源上限、进程树回收，
以及对 Anvil 自身秘密（human token、数据库、备份、其它任务/角色数据）的定向
读拒绝；**不限制普通宿主文件读取，也不限制网络**。决策见本地 ADR-0003。

### 严格档（Strict Tier）

不可信代码（PoC、动态验证、RSI replay 验证命令）的一次性隔离档。默认完全断网、
源码只读、独立 scratch、固定资源与时限；产物与证据回传后销毁。

### 沙箱实例（Sandbox Instance）

与角色上下文槽同生命周期的执行边界实例。同一 (角色, 任务上下文) 复用实例；
切换上下文或策略摘要变化时重建。

### 能力矩阵（Capability Matrix）

平台与基础设施实际可提供的能力集合（写范围、读拒绝、资源上限、进程树、断网、
挂载隔离、产物导出）。能力缺口必须通过状态接口与界面显式可见，不得静默降级。

### 能力横幅（Capability Banner）

能力矩阵存在缺口时，在运行详情、治理页或状态接口展示的常驻警示。

### 受控 PoC 运行（PoC Job）

一次 build → run → 取证的受控执行单元，绑定 finding 与任务工作区快照。默认
安全档（断网、只读源、限额限时）可由角色发起；放宽网络/资源/时长或使用非默认
环境需人工批准。

### 已验证（Verified）

由成功的 PoC 作业派生的 finding 验证状态（`verified | pending | failed |
blocked | none`）。它是派生视图，不改变 finding 的手工 `poc_status` 字段。

### 源码快照（Source Snapshot）

严格档作业开始时对任务工作区做的一次性只读副本（排除 `.git`、沙箱 home、
依赖目录），记录 tree hash；作业期间的源码改动不影响本次验证。

### host-unsafe

显式关闭执行沙箱的宿主直跑模式，仅用于开发/排障。必须记审计、状态接口返回
常驻警告；严格档在 host-unsafe 下默认仍不执行。
