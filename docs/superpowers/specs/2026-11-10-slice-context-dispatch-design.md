> **2026-11-11 模型修订**
> dispatch 不是顶层任务。顶层任务仅 human 手动创建；所有 dispatch 都是来源 task 下的
> 派遣子任务（`parent_id=来源 task`、`parent_step_id=NULL`、`type=dispatch`），共享来源
> task 的 workspace/cwd，不进入 Lobby，不阻塞父任务；角色 submit 即 `done`，无人工评审。
> 同角色当前 step 内的并行工作优先使用运行时 subagent；delegation 仅保留跨角色/跨 turn/
> 阻塞式拆工。本文下方涉及“独立顶层任务/派遣子任务卡”的历史描述以本修订为准。

# 切片上下文隔离与派遣子任务 · 设计

- 日期：2026-11-10
- 状态：已实现（Phase 1–4），UI/文档持续补齐
- 相关：`2026-10-23-role-task-contexts-design.md`、`2026-11-10-role-state-machine-design.md`

## 1. 目标与非目标

### 目标

1. workflow 的每个切片（`slice_key`）拥有独立角色上下文；同一角色在切片 A/B
   的工作对话互不污染。
2. 角色被 @ 或收到与当前切片弱相关的请求时，可以新建一张来源 task 下的**派遣子任务卡**，
   在全新上下文中工作；原切片上下文保留。
3. 派遣子任务完成后，若原切片仍在执行且原 run 仍挂起，自动回到原切片上下文。
4. 路由可解释、可测试：身份硬规则优先，歧义时才用轻量词法相关度。
5. 与历史“委派子任务”语义明确区分，不改变其阻塞/验收规则。

### 非目标

- 不把 slice 建成任务卡；slice 是上下文/执行单位，task 是工作空间/记账单位。
- 不引入 embedding 或每次请求调用 LLM 做路由。
- 不做跨机器分布式上下文；仍是一角色一进程、单 turn。
- 不把 dispatch 变成 delegation；dispatch 是来源 task 下的 child，但 `parent_step_id=NULL`，不阻塞父 step/task。

## 2. 术语与关系

| 术语 | 语义 | 关系/阻塞 |
|---|---|---|
| task | 工作空间、工作区、账本单位 | 顶层任务无父；委派子任务有父 |
| slice | workflow 内一片工作（scope/execute/verify/fix/verify2/decide） | 不是任务；同一 slice 各 phase 共享上下文 |
| context key | `task_id` / `task_id::slice::<slice_key>` / `__global__` | daemon 会话槽的主键 |
| 委派子任务（delegation） | `parent_id + parent_step_id`，当前 step 内强依赖拆工 | **阻塞**父 step；派发者验收；负责人不能自验收 |
| 派遣子任务（dispatch） | 来源 task 下的 child，`parent_id=来源 task`、`parent_step_id=NULL`，`type=dispatch` | **不阻塞**来源 workflow；角色 submit 即 done，无人工评审 |
| 侧任务 | 已废弃术语 | 不再使用；统一叫“派遣子任务” |

关键区别：

- 委派子任务解决“同一父步骤内需要拆工/并行/不同权限/独立验收”；
- 派遣子任务解决“与当前切片弱相关、需要新上下文/新任务卡/不阻塞当前工作”；
- 同 task 同 slice 的复核/追问不是派遣，必须复用当前上下文。

## 3. 上下文模型

### 3.1 context key

```text
普通 task / 无 slice 步骤     -> <task_id>
workflow 静态/运行期 slice    -> <task_id>::slice::<slice_key>
reminder / 直聊              -> __global__
```

`slice_key` 只允许 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`；任务 id 不得包含
`::slice::`。静态 `slices` 展开时补 `slice_key`，**不补 `source_step_id`**
（`source_step_id` 只用于识别运行期动态切片）。

### 3.2 数据表

`role_task_sessions` 保留 PK `(role_slug, task_key)`，`task_key` 变为不透明
context key，并新增显式列：

```sql
task_id TEXT;
slice_key TEXT;
```

所有 task 级清理/查询必须使用 `task_id` 列，禁止从复合 key 猜 task。
`role_sessions` 新增 `current_context_key/current_slice_key`；`current_task_id`
仍表示 task 语义。

`runs` 新增：

```sql
slice_key TEXT;
wake_note TEXT;              -- 自动回切的一次性 continuation
dispatch_id TEXT;
origin TEXT;                 -- step|mention|dispatch|...
context_return_context_key TEXT;  -- 跨切片临时处理后的返回上下文
context_return_run_id TEXT;
context_return_after_turn INTEGER NOT NULL DEFAULT 0;
context_returned_at TEXT;
```

`tasks` 新增软关联（仅展示/回流，不形成父子关系）：

```sql
source_task_id TEXT;
source_slice_key TEXT;
source_step_id TEXT;
source_workflow_id TEXT;
requested_by TEXT;
dispatch_status TEXT NOT NULL DEFAULT 'none';
dispatch_reason TEXT;
dispatch_reason_json TEXT;
```

`task_dispatches`（回切状态机）：

```sql
id, source_task_id, source_workflow_id, source_step_id, source_slice_key,
requester_run_id, requested_by, target_task_id, target_role, request_hash,
return_context_key, return_run_id, return_step_id, reason_code, reason_json,
status, attempts, returned_at, created_at, updated_at
```

状态：`open → return_ready → returning → returned`；失败/终止：
`no_return/cancelled`。唯一部分索引保证同一目标任务只有一条活跃回切记录，
同一 `(source, slice, role, request_hash)` 自动去重。

`notes` 新增可选 `slice_key`；`NULL` 表示 task-wide。知识快照文件名和元数据
包含 slice，检索时当前 slice + task-wide 优先。

## 4. 路由算法

实现：`backend/anvil/services/context_router.py`。输入：

```python
target_role, source_task_id, source_slice_key, source_step_id, source_run_id,
action_kind, message, explicit_mode, target_task_id, target_slice_key, scope_paths
```

判定顺序：

1. `explicit_mode=dispatch` → 新派遣子任务。
2. `explicit_mode=reuse` → 当前上下文。
3. `explicit_mode=switch` → 指定 task/slice 槽。
4. 目标 context key == 当前 context key → `reuse`（硬规则，同切片复核不得派遣）。
5. 目标 task/slice 已有槽 → `switch`。
6. 目标 task 已有其他槽 → 切到该任务最近使用的槽。
7. 目标是目标 task/slice 的参与者（assignee/planner/assignee 列表/role claim/
   workflow step role）：
   - 是 slice 的 step role → `new_slice`；
   - 只是 task 参与者且请求相关度低 → 允许 `dispatch`；
   - 相关度高 → `new_slice`（task 级或该 slice）。
8. 否则按轻量相关度在已有槽中选择：
   - Jaccard(token 交集) + 任务/slice 标题、描述、scope_paths；
   - 达到复用阈值 → `switch`；否则 `dispatch`。

每次判定写 `audit.action='context.routing'`，包含 mode/context/task/slice/
reasons/score/current context。人工/Agent 可用 `context_mode` 覆盖。

### 示例

| 场景 | 结果 |
|---|---|
| 当前在 slice B，同切片反馈 B 的问题 | `reuse`，不新建任务 |
| 当前在 B，A 是上一轮已有切片的异常反馈 | `switch` 到 A 槽；无槽但有 A 职责则 `new_slice` |
| 当前 debug 任务 D，另一个任务请求无关领域修复 | 低相关/无归属 → `dispatch` 派遣子任务 |
| 来源 task 参与者，但请求不属于其 slice/职责且低相关 | `dispatch`（允许） |

## 5. 派遣子任务流程

### 5.1 创建

API：`POST /api/v1/tasks/{tid}/dispatches`

```json
{
  "target_role": "dev",
  "message": "请处理另一个独立问题",
  "title": "可选标题",
  "acceptance": ["验收 1"],
  "scope_paths": ["src/cache"],
  "slice_key": "A",
  "context_mode": "dispatch",
  "force": false
}
```

服务：`services.dispatch.create_dispatch`。

1. 校验来源 task 非终态、目标角色存在且非 onboarding、请求者是来源 task 参与者
   （human 例外）；目标角色也必须是来源 task 参与者（共享 workspace 的权限边界）。
2. 沿 `tasks.source_task_id` 检查派遣深度，超过 `ANVIL_DISPATCH_MAX_DEPTH`
   拒绝。
3. `request_hash` 去重；同活跃请求直接返回已有任务（`force` 可绕过）。
4. 创建来源 task 下的 child：`type=dispatch`、`parent_id=来源 task`、
   `parent_step_id=NULL`、`assignee=target`、无 `depends_on`；继承来源
   workspace/repo/base_branch，并复用同一个 workspace 路径/cwd。
5. 写软关联字段与 `task_dispatches`；记录目标角色当前上下文/run/step 作为返回点。
6. 为目标任务创建 `origin=dispatch` 的新 run，prompt 只包含请求、验收和来源标识，
   不注入来源切片原始对话；run 使用来源 task 的 cwd，避免回切时 resume 轮换。
7. 来源任务时间线写 `kind=dispatch` 活动；来源 workflow 不阻塞。
8. `pump(run,'wake')`；目标角色忙时进入其队列。

### 5.2 结果回流

派遣任务进入 `done` 时：

- 摘要取提交 claim summary / submit audit / 任务标题，≤1500 字符；
- 来源任务写 `dispatch.completed` 活动（软链接目标 task、状态、摘要）；
- 通知 `requested_by`（非目标角色本人）；
- 结果只以摘要/知识形式回流，不复制派遣任务的原始对话。

### 5.3 自动回切

只在派遣任务 `done` 时触发；`closed/abandoned` 表示终止/失败，进入
`no_return`，不自动 resume。

校验：

1. `return_run_id` 存在且状态为 `suspended`（running 则等待下一轮对账）；
2. `return_step_id` 存在时，对应 step 仍 `running`、workflow 仍 `running`；
3. 原 task 未 `done/closed`、未归档；
4. `return_context_key` 对应槽仍存在。

通过后：

- `open/return_ready → return_ready`；
- 在同一条 suspended run 上写 `runs.wake_note`（不新建 run，保持 step/lease 绑定）；
- scheduler 的 `resolve_dispatch_returns` 执行 `return_ready → returning`，
  `pump(run,'resume')` 成功 → `returned`，写 `returned_at`；失败按 attempts 重试，
  超过 `ANVIL_DISPATCH_RETURN_MAX_ATTEMPTS` → `no_return`。
- `wake_note` 注入 prompt 顶部后，spawn 成功即清空，避免后续 turn 重复携带。
- 回切最多一次；不会因回切再次触发派遣。

### 5.4 跨切片临时处理回切

同 task 从 B 临时切到 A 复核时：

- 在 A 的 run 上记录 `context_return_context_key=B`、`context_return_run_id=B.run`、
  `context_return_after_turn=1`；
- A run 结束（suspended）后，scheduler 的 `resolve_context_returns` 校验 B run
  仍 suspended、B step/task 仍 running/未终止，然后在 B 上写一次性 wake_note 并
  resume；
- 若 B 已提交/评审/完成/closed，清标记不回切；
- 一个临时切换只有一个返回目标，返回后不自动切回 A。

## 6. CIC 与知识隔离

`_build_cic(role, task_id, slice_key)`：

- 保留：角色 MEMORY/PRINCIPLES、角色知识库、任务描述、当前 slice 的 step/
  instructions/acceptance、task-wide 蒸馏知识、共享 lesson、知识索引；
- 排除：其他 slice 的原始 comment/mention/step 对话、其他 slice 的执行 summary；
- 最近消息查询：slice 上下文 = `task_id=? AND (slice_key=? OR slice_key IS NULL)`；
  task 级 = `slice_key IS NULL`；
- workflow 进度只展示当前 slice + task 级/plan_slices 步骤；
- 知识快照按 slice 生成，当前 slice 优先，task-wide 可跨 slice 带入。

mention/comment 写 `activities.slice_key`：Agent 运行级 token 从当前 run 推断；
human/测试旁路写 task-wide。`anvil inbox check` 默认按当前 slice 过滤，后续可加
`--task-wide` 显式拉取。

## 7. 队列与 daemon

- `_Queued(text, run_id, task_id, slice_key, priority)`；
  priority：return=10 / step=20 / findings=40 / mention=50 / default=100；
  同 priority 稳定 FIFO。
- 同一 context key 才复用进程；切换时保存旧槽、杀进程、恢复目标槽。
- `_switch_context_inner` 只 `--resume` 目标槽的 session；cwd 变化或 token 超阈值
  时新建 CIC。
- workspace/cwd 仍按 task 共享：精确 slice 槽 → 同 task 任意槽 → task workspace。
- task 级 reset/delete 清所有 slice 槽；slice 级 reset 只清一个；workspace 仍复用。
- workflow 重启/新 execution：清该 task 的旧 slice 槽（保留 task 级上下文），避免
  新执行 resume 旧对话。
- 孤儿清理、任务删除、workspace busy 检查全部改用 `role_task_sessions.task_id`。

## 8. API / CLI / UI

### API

- `GET /roles/{slug}/runtime`：新增 `context_key/context_slice_key/contexts[]`
  （含 `slice_key/slice_title/context_key`）、`queue_contexts[]`。
- `GET /runs/{id}`：`Slice key/origin/context_return_*`。
- `GET /tasks/{id}`：软关联字段和 `dispatch_status`。
- `POST /tasks/{id}/dispatches`、`GET /tasks/{id}/dispatches`。
- 现有 `POST /tasks/{id}/dispatch` 仍是 human 的“启动/规划”，语义不变；文档/UI
  改称“启动/发起”，不要与派遣子任务混淆。

### CLI

```bash
anvil task dispatch $ANVIL_TASK_ID --to dev --slice A \
  -m "处理另一个领域的问题" --acceptance "复现;修复" 
anvil task mention $ANVIL_TASK_ID --to dev "同切片复核" --context-mode auto
```

### UI

- TaskDetail：任务头显示“来自任务 X / 切片 A”软关联 chip 和 dispatch 状态；
  “委派/派遣任务”页签同时列出 delegation 与派遣子任务，只展示/追踪已创建任务，
  不提供人工派遣表单；人工派遣在任务会话中 `@目标角色` 完成，Agent/CLI 仍可调用
  `POST /tasks/{id}/dispatches`。
- Lobby：派遣子任务不显示；来源 task 的“委派/派遣任务”页签展示。
- RoleSessions：同一 task 多 slice 时列出每个 slice 的上下文（当前/已保留/无会话），
  优先展示当前 slice 的 run。
- 图例文案：复用当前上下文 / 切换到切片上下文 / 新建切片上下文 / 已派遣派遣子任务。

## 9. 实施状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | DB 迁移、context key、daemon 槽、静态 slice key、cleanup/workspace | 完成 |
| Phase 2 | CIC slice 过滤、mention/comment slice 标记、snapshot slice、notes slice_key | 完成（基础） |
| Phase 3 | context_router、dispatch service/API/CLI、协议 | 完成（自动 mention 派遣默认关闭） |
| Phase 4 | task_dispatches、wake_note、自动回切、跨切片回切、scheduler 对账 | 完成 |
| Phase 5 | UI/文档/验收 | 进行中 |

Agent 之间的自动 mention 派遣由 `ANVIL_DISPATCH_AUTO_FROM_MENTION=1` 开启；人类 @ 和显式 `context_mode=dispatch` 始终创建派遣子任务；未开启时 Agent 路由
仍会选择 reuse/switch/new_slice，dispatch 降级为本任务内回复 run，并写 audit。

## 10. 测试

- `tests/test_slice_context.py`：context key、静态 slice key、迁移回填、CIC 隔离、
  workflow 重启清 slice 槽。
- `tests/test_daemon.py`：A/B slice 各独立 session、slice reset、workspace 复用。
- `tests/test_context_router.py`：同切片 reuse、已知切片 switch、无归属 dispatch、
  参与者低相关 dispatch、显式覆盖。
- `tests/test_dispatch_task.py`：child 模型/软关联/新 run/共享 workspace/幂等/深度/权限/自动 done。
- `tests/test_auto_return.py`：done 回切、已提交不回切、重复不重复 resume、
  跨切片临时回切一次。
- `tests/api/test_dispatch_api.py`：端点、软关联、幂等、参与者校验。
- 前端 `npm run build`。

## 11. 风险与边界

1. 旧 DB 迁移面大：已用显式 `task_id` 列 + 幂等迁移 + 回填测试降低风险。
2. 静态 slice 旧执行只能按 `<slice>.<phase>` 启发式回填；无法安全推导的保留
   task 级上下文。
3. 相关度阈值需要按真实消息调参；先用 audit 观测，必要时用 `context_mode` 覆盖。
4. 自动回切只对 persistent runtime 的 suspended run 生效；one-shot 无 session
   可 resume，记录为 `no_return`。
5. 派遣深度默认 2，防止 A→B→A 嵌套；去重防止重复建卡。
6. 结果回流只带摘要，不复制原文；来源 task 删除后派遣任务保留并标记
   `cancelled/no_return`。

## 12. 验收标准

- [x] 同角色同 task 的 A/B slice 使用不同 context key，切换后各自 resume。
- [x] 同 slice 的 scope/execute/verify/fix/verify2/decide 共享上下文。
- [x] task reset/delete 清全部 slice 槽；slice reset 只清当前 slice。
- [x] 同 task 同 slice 复核 → reuse，不新建任务。
- [x] 同 task 已知 A slice、当前 B → 切到 A 上下文。
- [x] 跨领域/低相关 → 派遣子任务卡，原任务不阻塞。
- [x] 派遣完成且原切片 running/run suspended → 自动回切一次；已提交不回切。
- [x] 委派子任务阻塞/验收语义不变。
- [x] 新 slice CIC 不含其他 slice 原始消息。
- [x] 全量后端测试与前端构建通过。

## 13. 与角色状态机的联动约束（2026-11-10 修订）

这些约束用于避免派遣/返切被原有 scheduler 看门狗破坏：

1. **看门狗必须识别外部等待**：`_nudge_stalled` 跳过以下 run：
   - `task_dispatches.return_run_id` 指向它且 dispatch 处于
     `open/return_ready/returning`；
   - 它被其他 run 的 `context_return_run_id` 指向且 `context_return_after_turn=1`；
   - 同角色还有非终态的派遣子任务；
   - 同角色正在其他 task 执行 turn，或 daemon 正忙/有队列。
2. **reset 作用域必须精确**：崩溃、超时、run session reset、self reset-context
   只清当前 `(task, slice)`；task id 不再隐式代表“全部 slice”。任务删除才用
   `all_slices=True`。
3. **warm 上下文与 inbox 按 slice 过滤**：
   - step/reply/normal prompt 的最近消息只注入当前 slice + task-wide；
   - `role_inbox_cursors` 主键改为 `(role, task, slice_key)`，`''` 表示 task-wide；
   - `anvil inbox check/commit` 自动取当前 run 的 slice，`--task-wide` 显式越界。
4. **priority 接线**：`spawn_node` 按 run 类型传队列优先级：
   `return=10 / step=20 / findings=40 / mention=50 / default=100`。
5. **wake_note 生命周期**：追加合并而不是 `COALESCE` 只保留第一条；只有 daemon
   真正写入 stdin 后才清空，排队/服务重启期间保留。同 return run 的多个派遣
   结果共享一次 resume。
6. **跨 task/跨切片 mention**：目标 run 的 transcript 看不到来源 task 的消息时，
   必须把请求写入 `wake_note` 后 resume，不能只 resume 旧 prompt。
7. **临时回切收尾**：临时 run 进入 `failed/abandoned/completed` 也要处理
   `context_return_*`，带失败原因/结果摘要返回原上下文；不能留下永久死标记。
8. **findings 强制 task-level**：findings 唤醒只复用 `slice_key IS NULL` 的 run，
   绝不 resume 切片 step/reply run。
9. **one-shot 限制**：没有 `rs:` 常驻会话的 run 不自动回切（标记 `no_return`），
   避免 resume 出无上下文的新进程。
10. **迁移**：`schema.slice_context_v2` 补 `notes.slice_key`、
    `context_manifests.slice_key`，并把 inbox cursor 迁移到三列主键。已发布的
    v1 不修改。
