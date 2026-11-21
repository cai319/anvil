# subagent_hint 契约设计（D4）

> 状态：草案（2026-11-21）
> 前置：`2026-11-21-runtime-subagent-observability-design.md`
> 决策（已与用户确认）：能力感知规划；服务端硬校验；前端展示/编辑；
> hint 冲突一律自动降级为 false + 审计，不拒绝草案。

## 1. 定义

`workflow_steps.subagent_hint`（0/1）是**规划期提示**：告诉该步骤角色，可以在
**同一 turn 内**使用运行时原生只读子代理做并行调查。它不创建 Anvil 工作单元，
不阻塞步骤，不唤醒任何人。跨 turn 工作必须用委派/派遣。

## 2. 载体

必须全部覆盖：

- flat steps：`steps[].subagent_hint`
- 静态 slices：切片级 + `scope/execute/verify/fix/verify2/decide` 阶段级
  （切片级 true 强制全阶段 true）
- 动态切片 loop：`loop.<phase>.subagent_hint`
- external nodes（旁路验证）：`external_nodes[].subagent_hint`
- revision `patch_steps[].fields.subagent_hint`
- revision 生成步骤（`_insert_revision_steps`）

## 2.1 注入位置（明确）

本批仍沿用既有契约：`SUBAGENT_PROTOCOL` / `SUBAGENT_AUDIT_PROTOCOL` 写入**投影文件**
（CLAUDE.md/AGENTS.md/GEMINI.md），不在每 turn 的 stdin prompt 中重复注入。
投影失败由 daemon 的 `projection.error` 审计兜底；执行侧 runtime 能力漂移时，
`_projection_protocol` 不注入并写一次性 `subagent_hint.skipped`（按 run+step 去重）。
“每 turn 重发协议”作为后续可选增强，不在本批范围。

## 3. 能力解析（服务端）

与运行时注入同一口径，逐级回退：

1. 角色定义 `runtime.id` → `runtimes.capabilities.supports_subagents`（DB）；
2. 内置 runtime YAML `capabilities.supports_subagents`；
3. 角色未绑定 runtime → `config.DEFAULT_RUNTIME`（claude-code）。

`supports_subagents=false` 时：

- `hint=true` **一律降为 0**（不拒绝草案、不报 422）；
- 写事件 `subagent_hint.downgraded`，payload 含 `where/role/runtime/requested/
  effective/reason`；
- 同样适用于 revision patch 与新生成的修订步骤；
- 已落库的旧 hint 不主动回写（读取侧仍以运行时注入条件为准）；
- 执行侧发现 hint=true 但当前 run 快照不支持 → 不注入 + `subagent_hint.skipped`
  一次性审计（run+step 去重），不修改 DB、不阻塞执行。

## 4. 规划期能力感知

- `planning.build_roster` 的 runtime 字段增加 `supports_subagents`；
- `render_roster` 输出“支持 subagent / 不支持 subagent”；
- `PLANNING_PROTOCOL`：写 true 前必须确认该阶段角色 runtime 支持；不支持写 false；
  服务端会自动降级并审计；
- `REVISION_PROTOCOL`：修改/新增步骤 hint 时同样先确认能力。

## 5. 提示词拆分

- `SUBAGENT_PROTOCOL`：通用只读调查（turn 级、拆/不拆、单一问题、2–3 个、
  确定性工具、JSON `question/conclusion/confidence/evidence/open_questions`、
  父角色自行聚合、不写文件）。
- `SUBAGENT_AUDIT_PROTOCOL`：安全审计 checker 专用（condition_id、verdict、
  call_chain、TP/FP、条件逻辑聚合、不投票）。
- 注入规则：hint=1 且 runtime 支持 → 通用协议；若任务启用问题板且角色是
  reviewer → 追加审计协议。

## 6. 前端

- `WorkflowStep.subagent_hint`：步骤卡显示“子代理调查”徽章（运行中只读）。
- 模板/草案编辑器：步骤面板与 loop 阶段面板各有一个 hint 复选框；
  编译回具体定义时原样携带；运行中步骤不可编辑。
- 草案保存后服务端重新校验；不支持的 true 会以 false 形式回存并留审计。

## 7. 模型级门控

当前只做 **runtime 级**门控（角色绑定 runtime 的能力）。模型级能力不单独建模：
同一 runtime 内不同模型的 subagent 行为由运行时自己决定；Anvil 不猜测。
文档明确：runtime 声明支持即视为支持。

## 8. 测试

- flat step / loop phase：unsupported runtime → hint 0 + 审计；supported → 保留。
- static slice 与 external node 走同一 `_effective_hint`。
- revision 插入步骤与 patch 字段 → 降级。
- roster 输出 supports_subagents。
- 协议拆分：通用协议不含 verdict/TP/FP；reviewer 步骤追加审计协议。
- 前端 build。
