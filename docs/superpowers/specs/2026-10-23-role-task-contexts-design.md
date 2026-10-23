# 多任务角色调度与任务上下文绑定 · 设计方案

- 日期：2026-10-23
- 状态：已实施（2026-10-23）
- 范围：常驻运行时（`services/daemon.py`）、角色运行时状态、任务/工作流调度、任务详情「角色运行时」页签

## 1. 问题与目标

一个角色（包括 Cindy/planner）可能同时被多个任务绑定。当前实现里：

- `role_sessions` 以角色为主键，一个角色只有一个 `session_id`；
- 常驻进程按角色复用，忙时新消息进入**同一个队列**；
- 队列里的下一条消息无论属于哪个任务，都直接写进**同一个会话上下文**；
- 进程的 `cwd` 在第一次 spawn 时固定，切换任务时不会换；
- 任务 B 只能看到一个笼统的「运行中」，看不到角色是否正忙在任务 A。

这会造成三个问题：

1. **上下文串味**：任务 B 的工作被注入任务 A 的会话，反之亦然；任务 A 的历史、工具结果、未决问题会污染 B。
2. **状态不可见**：B 里 @ 角色后只能等，无法知道它在忙 A、排队多久、什么时候会轮到自己。
3. **恢复不可靠**：进程空闲释放后只有一个 `--resume` 目标；A/B 交替工作时，谁最后留下就恢复谁，另一个任务的上下文实际上已经丢失。

目标：

- 同一角色**任意时刻只执行一个任务**（串行、无抢占）；
- 角色的忙碌状态**绑定到任务/步骤**，其他任务可以看到「忙碌于任务 A」；
- 任务 B 的 @/步骤派发在角色忙时排队，角色空闲后自动处理；
- 任务切换时使用**该任务自己的上下文**：
  - 第一次进入任务 B：新建会话 + CIC（任务时间线、工作流决策、角色记忆/知识快照）；
  - 再次回到任务 A：恢复 A 的会话，并注入「离开期间发生了什么」的增量；
- 同一任务内继续复用同一进程/会话，避免不必要地重建上下文；
- 保留现有「每角色最多一个活跃运行时进程」的资源约束。

## 2. 业界参考

### 2.1 Raft（raft.build）

Raft 的核心模型是「**一个 agent 就是一个会话**」：角色身份、工作区、记忆跨天跨任务连续；空闲时进程可释放，消息来时唤醒；模型侧用「构造初始上下文（CIC）」在冷唤醒时重建连续性，而不是让模型写摘要；角色有 online/busy/error/offline 状态，共享房间里的消息通过 **agent inbox** 拉取，由角色决定何时读、读到什么程度。

对本案的启发：

- 角色身份、记忆、工作区与「当前在哪个任务上」必须解耦；
- **busy 是一等状态**，不是 run 的副产物；
- 冷唤醒/上下文丢失时，用确定性的 CIC 重建，而不是依赖旧进程还活着；
- 消息应当可查询、可排队，不能因为角色忙就丢掉。

Raft 本身采用「单会话长期存活」；Anvil 的运行时支持 `--resume <session-id>`，因此本案进一步把会话按 `(role, task)` 切成多个**上下文槽**，以获得任务间隔离。这是对 Raft 模型的扩展，不是照搬。

参考：
- <https://raft.build/resources/blog/introducing-raft-where-humans-and-agents-build-together/post.md>
- <https://raft.build/resources/blog/constructed-initial-context/post.md>
- <https://raft.build/resources/blog/is-having-agents-in-the-room-meant-to-be-chaotic/post.md>
- <https://docs.raft.build/features/agents/lifecycle.md>
- <https://docs.raft.build/developers/sdk/external-agents/wake-and-status.md>

### 2.2 Claude Code 会话

Claude Code 的会话文件按 **启动时的 cwd** 分目录存放：`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`。`--resume <session-id>` 只有在相同 cwd（编码后同一 project 目录）下才能找到该会话；cwd 不同会得到新会话。

因此「任务上下文槽」必须同时记录 `session_id` 和 `cwd`（以及投影使用的 `workspace_path`）。只存 session id 不够。

参考：
- <https://code.claude.com/docs/en/agent-sdk/session-storage>
- <https://github.com/ppiankov/contextspectre/blob/main/docs/session-architecture.md>
- <https://venturebeat.com/orchestration/claude-codes-tasks-update-lets-agents-work-longer-and-coordinate-across>

### 2.3 AgentScope / 长程任务编排

AgentScope 的 Harness 任务模型把任务身份与连接/进程/上下文窗口解耦：稳定 `task_id`、显式 `WAITING_*` 状态（等待不是失败）、`Continuation` 记录恢复所需的目标/计划/事实/Artifact/预算，上下文重置后从权威状态重建视图；子任务上下文与能力隔离，且「最多一个活跃子任务」的互斥约束很常见。

本案对应：

- `runs` 是执行记录，`workflow_steps` 是任务内的子任务跟踪页；
- 角色忙时，B 的 run 进入等待（排队），不是失败；
- 切回任务时用 Continuation（CIC + 增量）恢复，而不是假设旧进程还在；
- 同一角色串行（最多一个活跃 turn）。

参考：
- <https://java.agentscope.io/v2/zh/blogs/how-to-build-agent-harness/02-task-orchestration>
- <https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%204%20%E7%AB%A0%20%E4%BB%BB%E5%8A%A1%EF%BC%9A%E7%BC%96%E6%8E%92%E3%80%81%E9%95%BF%E7%A8%8B%E6%8E%A8%E8%BF%9B%E4%B8%8E%E5%8D%8F%E4%BD%9C%E6%B5%81%E8%BD%AC.md>

### 2.4 其他已有实践

- **Kubernetes**：liveness/readiness 分离「进程活着」与「任务可推进」；本案区分「角色进程忙」与「run 等消息」。
- **Temporal**：workflow 状态外置、activity 重试退避；本案已有租约、自动恢复、指数退避，不重复造。
- **Claude Code Tasks**：任务状态跨会话持久化，任务列表可被多个会话共享；提示我们把任务 ID 与会话解耦。

## 3. 核心模型

### 3.1 不变量

1. **单角色单 turn**：一个角色最多一个常驻进程、一个正在执行的 turn；不做 turn 内抢占。
2. **任务绑定忙碌**：忙时 `current_task_id` 必有一个值；其他任务看到的是该任务。
3. **上下文按任务隔离**：每个 `(role_slug, task_key)` 有一个上下文槽，槽内保存该任务的 session/cwd/水位。
4. **切换只在 turn 边界**：只有 `result` 事件之后才允许换上下文/换进程；不允许中途杀掉正在思考的 turn。
5. **消息不丢**：忙时消息进入角色队列（带 task/run/spawn 参数），进程崩溃后队列仍可重放。
6. **恢复优先于重建**：有该任务的 session 且 cwd 匹配时 `--resume`；否则新建会话 + CIC。
7. **工作目录一致**：同一任务的后续 run 复用该任务的 `workspace_path`/`cwd`，否则 `--resume` 失效。

### 3.2 task_key

- 有任务：`task_key = task_id`
- 无任务（reminder、角色直聊、系统唤醒）：`task_key = "__global__"`

`__global__` 是角色级上下文，不与任何任务共享。

### 3.3 上下文槽

```sql
CREATE TABLE role_task_sessions (
    role_slug      TEXT NOT NULL,
    task_key       TEXT NOT NULL,              -- task id 或 __global__
    session_id     TEXT NOT NULL DEFAULT '',
    runtime_id     TEXT,
    cwd            TEXT,                       -- Claude Code 会话所属目录
    workspace_path TEXT,
    model_id       TEXT,
    status         TEXT NOT NULL DEFAULT 'idle', -- idle|busy|dead
    context_tokens INTEGER,
    last_seen_activity_id INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (role_slug, task_key)
);
```

`role_sessions` 继续保留为「当前活跃进程」的镜像行，并新增：

```sql
ALTER TABLE role_sessions ADD COLUMN current_task_id TEXT;
ALTER TABLE role_sessions ADD COLUMN current_run_id TEXT;
ALTER TABLE role_sessions ADD COLUMN current_step_id TEXT;
```

- `role_sessions.session_id` 始终等于当前加载槽的 session id（兼容旧代码/旧测试）；
- `role_sessions.status = starting|idle|busy|dead` 表示进程状态；
- `current_task_id` 仅在忙时有值；空闲时为空，`context_task_id`（内存）表示当前仍加载着哪个槽。

## 4. 运行时状态机

```
                 (空闲，槽仍加载)
   idle ──消息同 task──> busy ──result──> idle
     │                      │
     │                      └─队列下一条是别的 task─> 保存槽 → 杀进程 → 加载目标槽 → spawn → busy
     │
     └─空闲超时/手动 kill─> 保存槽 → dead ──下次消息──> 加载对应槽 → spawn(resume|new+CIC) → busy
```

关键点：

- **空闲不卸载槽**：同一任务连续唤醒时继续用当前进程/上下文，避免频繁 spawn。
- **切换是惰性的**：只有在「下一条要投递的消息属于另一个 task」时才切换；不是一轮结束就立刻卸载。
- **切换需要重开进程**：Claude Code 的 `--resume` 和 cwd 绑定，无法在同一个进程里切到另一个 project 的会话；因此换 task 必须 `kill → spawn(resume target)`。同 task 内则绝不重开。
- **不抢占**：当前 turn 不会被 B 的消息打断；B 只是排队。

## 5. 队列与调度语义

### 5.1 角色队列

队列元素：

```text
(text, run_id, task_id, spawn_spec)
spawn_spec = (executable, args_new, args_resume, ctx, cwd, env,
              model_pool, context_window_floor)
```

之所以把 spawn 参数随消息保存：B 的消息在 A 忙时入队，轮到时需要按 B 的运行时配置/工作区拉起进程，不能复用 A 的。

队列顺序：**FIFO**。这符合「B 等 A 忙完」的直觉；不会因为 A 持续产生消息而饿死 B（B 在 A 完成当前 turn 后即被处理）。

### 5.2 调度器

- `workflow.advance` 已有全局角色忙检查（`runs WHERE role_slug=? AND status IN (created,projecting,running)`），保留；
- 角色忙 A 时，B 的步骤保持 `pending`，不会创建 run；
- B 的 @ 提及会创建 `[anvil:reply]` run 并入队，但不会启动 B 的步骤；
- 角色空闲后，队列先处理已排队的 @；随后 tick 再推进 B 的待办步骤；
- 同一角色在多个 workflow 之间仍串行，不引入并行 turn。

### 5.3 可见性

- `GET /api/v1/roles/{slug}/runtime` 返回：
  - `alive/status/busy_since/current_task_id/queue_len/queue[]`
  - `current_task`（标题/状态，来自 tasks）
  - `contexts[]`（该角色所有任务槽：task_id、是否有 session、context_tokens、last_active_at）
- 任务详情「角色运行时」页签：
  - 角色空闲：`空闲`；
  - 角色忙在本任务：`进行中 · 步骤 X`；
  - 角色忙在别的任务：`忙碌 · 任务 A「标题」`（可点击跳到 A）+ `本任务排队 N 条`；
  - 角色在本任务有上下文槽：显示「已保留上下文 · 上次活跃 … · N tokens」；
  - 角色无本任务槽：显示「首次进入将新建上下文」。
- **实时点灯**（任务头部常驻 + 「角色运行时」列表，SSE tick 驱动）：
  - 🟢 绿：本任务工作中；子任务尚未提交、turn 之间等待下一轮时仍为绿；
  - 🔴 红：正在其他任务上工作（悬停显示任务名与本任务排队数）；
  - 🟠 琥珀：正在切换任务上下文（短暂过渡态）；
  - 🔵 浅蓝：空闲，且本任务上下文已保留（下次进入可快速 resume）；
  - ⚪ 灰：空闲/冷启动，本任务无上下文槽。
- **状态徽标**（与点灯并列，沿用任务状态的色彩语义）：
  - 蓝「进行中」= 正在本任务（含子任务 turn 间等待）；
  - 红「繁忙」= 正在其他任务，本任务等待其空闲；
  - 灰「待办」= 空闲；绿「完成」= 该角色在本任务的步骤已全部通过；
  - 琥珀「排队中/切换上下文」= 等角色空闲或正在换上下文。
- **任务右侧「任务信息 → 参与角色」chip 顺序**：`点灯 → 状态徽标 → slug`
  （slug 用等宽小字，显示名/忙碌任务/排队数放在 tooltip）；「领队」行使用同一 chip，
  「角色运行时」列表同一顺序。任务顶部 session-meta 保持原来的 `👤 领队 / 👥 成员` 摘要，
  不重复展示状态。
- 时间线新增可选事件：角色忙导致排队时写一条 `kind=queue` 的活动（不 @ 人），让 B 的会话里能看到「已排队，等待角色完成 A」。

## 6. 上下文切换协议

设当前加载槽为 `cur`，下一条消息属于 `target`。

### 6.1 同任务（target == cur）

- 直接写 stdin，复用进程；
- 前缀：高水位提示（沿用 `_context_hint`），不加 CIC/增量。

### 6.2 切换到另一任务

1. 若 `cur` 非空：保存 `cur` 槽（session_id、context_tokens、model、status、last_active_at、last_seen_activity_id）；
2. 若进程存活：`suppress_exit=True`，优雅关闭 stdin 并等待退出；旧 reader 的退出回调不得把它判为失败；
3. 读取 `target` 槽：
   - 有 `session_id` 且 `cwd` 与本次 spawn 的 cwd 一致 → `resume`；
   - 有 `session_id` 但 cwd 不一致 → 视为不可恢复，走新建（CIC），写 audit；
   - 无 session → 新建，`cic_pending=True`；
4. 生成**增量交接**（见 6.3），作为本次消息前缀；
5. `spawn` 目标槽，投递消息；
6. 更新 `role_sessions` 镜像与 `role_task_sessions` 状态。

### 6.3 交接内容

**新建会话（CIC）**：沿用现有 `_build_cic(conn, role, task_id)`：

1. `MEMORY.md` 尾部（≤4000 字符）；
2. 任务相关 FTS 记忆（≤5 条）；
3. 当前 workflow 最近 30 步状态/摘要/反馈；
4. 当前任务最近一次知识快照；
5. 最近 25 条任务消息 + 最近 12 条动作。

**恢复会话（增量）**：

- 以槽内 `last_seen_activity_id` 为游标，取该任务之后的活动（排除角色自己发出的），压缩为：

```text
## 你离开任务期间的新进展
- [human] @you: ...
- [workflow] 步骤 S3 → approved：...
- [role-b] 评论：...
（共 N 条，完整内容用 `anvil inbox check <task>` 拉取）
```

- 同时带上当前 run 的 prompt（步骤说明/反馈/被 @ 的消息），保证角色知道这次要做什么；
- 不重复注入整段任务历史（会话里已有）。

投递成功时把槽游标推进到当前任务最大活动 id，避免下次重复。

### 6.4 会话大小轮换

- 同任务内，`context_tokens >= CIC 阈值`：现有逻辑在 turn 结束时释放进程；下一轮该任务新建会话 + CIC，**只清该任务槽**，不影响其他任务；
- API 溢出/请求体超限：只清当前任务槽并重投，其他任务槽保留；
- 进程崩溃自动 reset：只清当前任务槽。

## 7. 工作区与 cwd 绑定

`runs._create_worktree` 目前每次 run 都建新 worktree，且分支名 `agent/<slug>/<task>` 对同一任务重复创建会失败；即使成功，新 run 的 cwd 也与旧 session 不同，`--resume` 会失效。

本方案：

- `project_node` / `spawn_node` 解析工作区时，优先使用 `(role, task)` 槽里已记录的 `workspace_path`（存在且可访问）；
- 槽不存在时使用本次 run 的 worktree/role home，并把它写入槽；
- 因此同一任务的所有 run 共用同一个工作区/cwd，session 可稳定 resume；
- `cwd` 也写入槽；切换任务时按槽恢复；
- 若工作区被外部删除，槽的 cwd 不可用 → 自动新建上下文（CIC），并在时间线记一条技术事件。

> 不在本方案内重构 git worktree 的创建策略（分支复用/清理）；只需保证「同一任务同一工作区」这一约束。

## 8. API 与数据迁移

### 8.1 迁移

- 建 `role_task_sessions`；
- 给 `role_sessions` 加 `current_task_id/current_run_id/current_step_id`；
- 启动时把旧的 `role_sessions.session_id` 迁移到 `__global__` 槽（保守做法：旧会话不属于任何明确任务，不能乱绑）；
- `_cleanup_orphans` 删除任务时同时删 `role_task_sessions` 中对应 task_key。

### 8.2 端点

```text
GET  /api/v1/roles/{slug}/runtime          # 角色当前进程 + 当前任务 + 队列 + 所有任务上下文槽
POST /api/v1/roles/{slug}/session-reset    # 清空该角色所有任务上下文（保留 MEMORY）
POST /api/v1/runs/{rid}/session-reset      # 只清该 run 所属任务的上下文
POST /api/v1/runs/{rid}/full-reset         # 清所有上下文 + workspace
POST /api/v1/runs/{rid}/restart            # 保留所有任务上下文，重开进程
POST /api/v1/me/session/reset              # 只清当前任务槽（角色自主换上下文）
```

### 8.3 角色删除

`roles.delete` 同步删除 `role_task_sessions`。

## 9. 错误与边界

| 场景 | 行为 |
|---|---|
| 角色忙 A，B @ 它 | 创建 reply run 并入队；B 时间线显示「已排队」；不失败、不 @human |
| B 的 workflow 步骤等待 | 步骤保持 pending；角色空闲后 tick 自动启动 |
| 切换时旧进程退出 | 视为计划内切换，不触发 `on_exit`、不判 failed；只在 audit/日志记录 |
| 目标槽 session 损坏/找不到 | 新建 CIC 会话，写 `session.resume_failed` audit；不阻塞任务 |
| cwd 不匹配 | 新建 CIC 会话；旧槽保留为 `dead`，下次仍可尝试（如果 cwd 恢复） |
| 进程崩溃 | 只清当前任务槽；队列保留，后续消息触发重放 |
| 任务被删除 | 丢弃该任务队列项、删槽；若正忙该任务则杀进程；忙别的任务不动 |
| 上下文溢出 | 只换当前任务会话；其他任务槽不受影响 |
| 服务重启 | 所有槽持久化；`_heal_db` 把 role_sessions 标 dead；下次唤醒按槽 resume |
| 同一角色高并发 @ | 队列 FIFO；单进程串行；不创建第二个进程 |

## 10. 备选方案与取舍

### 方案 A：单会话 + 任务切换时注入提示（Raft 近似）

- 只保留一个 session，切任务时前缀「你上次在做 A，现在做 B」。
- 优点：实现最小、切换零成本、上下文连续。
- 缺点：任务间上下文互相污染；长会话成本高；无法真正恢复 A 的细节。
- 结论：不满足本案「新上下文 / 恢复绑定上下文」的要求。

### 方案 B：每个 `(role, task)` 常驻一个进程，并行保活，串行执行

- 优点：切换瞬时，两个上下文都热。
- 缺点：违背「每角色最多一个活跃进程」；资源与订阅占用翻倍；进程生命周期管理复杂。
- 结论：暂不采用；未来可作为高频切换的优化项（例如 LRU 保留 2 个热进程）。

### 方案 C（推荐）：单进程 + 按任务槽懒切换

- 同一任务内复用进程；只有下一条消息属于不同任务才 `kill → resume`。
- 优点：满足隔离与恢复；资源约束不变；实现可渐进。
- 缺点：切换有一次进程启动/冷缓存成本（可接受；任务粒度通常远大于 turn）。
- 结论：采用。

## 11. 测试计划

### 11.1 单元/集成（pytest）

1. 同任务连续唤醒：进程 PID 不变、argv 只有一次 `new`。
2. A→B 切换：PID 变化；B 第一次 `new`；B 的 CIC 含 B 的任务标题/步骤。
3. A→B→A：第三次 argv 为 `resume <A 的 session id>`；B 槽仍保留。
4. 忙 A 时投递 B：B 不立即写入 stdin；A turn 结束后进程切换到 B。
5. 槽持久化：进程 idle release 后，A/B 各自 resume 自己的 session。
6. 模块级 reset：
   - `run.session-reset(A)` 只清 A 槽，B 槽可 resume；
   - `role.session-reset` 清全部槽；
   - `/me/session/reset` 只清当前任务。
7. 删除任务：清对应槽与队列；忙别的任务不受影响。
8. 上下文溢出：只清当前任务槽，其他槽保留。
9. 工作区稳定：同一任务第二次 run 仍使用槽里的 cwd/workspace。
10. API：`GET /roles/{slug}/runtime` 返回 current_task、queue、contexts。

### 11.2 回归

- 现有 `dev/tests/test_daemon.py` 全部通过；
- `dev/tests/test_workflow.py`、`dev/tests/api/test_workflow_api.py` 通过；
- 前端 `npm run build` 通过。

## 12. 实施顺序

1. DB 表/迁移/孤儿清理；
2. daemon 槽与队列模型（核心）；
3. reset/overflow/cancel/delete 路径；
4. 工作区稳定解析；
5. runtime 状态 API + 前端「角色运行时」页签；
6. 测试补齐与回归；
7. 在 `design.md` 增补实现口径，README/operations 如有必要同步。

## 13. 验收标准

- [ ] 一个角色同时绑定多个任务时，任意时刻只执行一个 turn；
- [ ] 任务 B 能看到角色「忙碌于任务 A」，并能看到本任务排队数；
- [ ] 角色完成 A 后自动处理 B，B 使用自己的上下文（首次 CIC）；
- [ ] 角色回到 A 时恢复 A 之前的会话，并看到离开期间的增量；
- [ ] 同一任务内不会因为消息连续而反复重开进程；
- [ ] 服务重启/进程崩溃后，A/B 的上下文仍可分别恢复；
- [ ] 现有 daemon/workflow 测试不回归。

## 14. 实施记录（2026-10-23）

| 设计点 | 落地位置 |
|---|---|
| `role_task_sessions` 表 + `role_sessions` 当前任务字段 | `dev/backend/anvil/db.py` |
| 任务上下文槽、队列元素携带 spawn spec | `services/daemon.py`（`_SpawnSpec` / `_Queued` / `_slot_*`） |
| turn 边界惰性切换、resume/CIC/增量交接 | `daemon.py::_flush_next_locked / _switch_context_locked / _send_locked` |
| 空闲释放保留各任务槽 | `daemon.py::_idle_loop`、`_on_turn_result` |
| 崩溃/溢出只清当前任务槽 | `daemon.py::_reader_loop`、`api/memory.py` |
| run/任务级 reset 与角色级 reset 区分 | `api/runs.py`、`api/roles.py`、`daemon.py::reset_session` |
| 删除任务清理槽（含 daemon 未启用） | `services/tasks.py`、`main.py::_cleanup_orphans` |
| 同一 `(role, task)` 复用 workspace/cwd | `graph/nodes.py::_run_workspace_path` |
| 角色忙闲/当前任务/队列/上下文 API | `api/roles.py::GET /roles/{slug}/runtime` |
| 任务详情「子任务/角色运行时」展示忙碌任务与排队 | `frontend/src/pages/RoleSessions.tsx` |
| 右侧「任务信息 → 参与角色」实时点灯 + 列表点灯 | `frontend/src/roleLive.ts`、`useRoleRuntimes.ts`、`TaskDetail.tsx`、`RoleSessions.tsx`、`style.css` |
| 设计口径同步 | `design.md` §6.1 / §6.3 |

验证：

- `python3 -m pytest`：368 passed, 22 skipped；
- `npm run build`（`dev/frontend`）：通过；
- 新增 daemon 用例覆盖 A/B 切换、排队、按任务 reset、空闲释放后分别 resume、
  首次进入任务的 CIC、切回任务的增量交接。
