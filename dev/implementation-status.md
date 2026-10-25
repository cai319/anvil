# Anvil 实现状态与缺口清单

对照 `../design.md`（v3.0）与 owner 决策（2026-09-16 / 10-17 / 10-18）。
最近更新：2026-10-18。

## 里程碑总览

| 阶段 | 设计范围 | 状态 | 说明 |
|---|---|---|---|
| M1 骨架 | 运行时发现/绑定、角色 home、任务 CRUD、CLI、SSE、大厅/任务详情 | ✅ 基本完成 | 新增 `anvil whoami`；运行时健康检查（`POST /runtimes/{id}/health`）补齐 |
| M2 协作与治理 | 认领/租约/快照/依赖/评审/diff/四层闸门/审计/角色工坊 | ✅ 基本完成 | 本轮补齐真实 git diff 校验、`.anvilignore`、系统/运行时并发闸门、日成本闸门、cancel/reopen、角色 message、版本回滚、home 文件读取、运行级会话令牌 |
| M3 工具治理 | skill/MCP 投影、工具白名单/预算、勾选矩阵、能力门控 | 🚧 部分完成（原定 v2） | 已有 skill/MCP CRUD + stdio 握商、`tools.yaml` 预设、DB→投影合并、allowlist→`permissions.allow`、能力门控展示；条件加载与完整矩阵 UI 未做 |
| M4 长程能力 | 三档 reset、reminder、FTS5、lessons 升格、提案审批、角色主页 | 🚧 大部分完成 | 本轮补齐 reminder 触发、三档 reset、FTS 检索与上下文注入、lessons 审批/80 上限、提案生效、`self edit-instruction`、角色主页/指标/版本 |
| M5 打磨 | 更多运行时、模板、指标、打包、首启引导 | 🚧 部分完成 | 成本/指标接口与前端、`anvil init --with-roles`、PyInstaller 数据清单已补；更多运行时 YAML 与完整首启向导未做 |

## 本轮已补齐（2026-10-18）

### 编排与运行
- 运行级会话令牌：spawn 时生成 `sessions.token`，绑定 role + task；`X-Anvil-Role` 不再可信；
  绑定任务之外的操作返回 403（`deps.get_auth` / `api/tasks._assert_session_task`）。
- 本地 token 轻校验中间件：非测试环境下所有写操作必须携带 `X-Anvil-Session`。
- 投影目标改为读运行时 YAML 的 `projection.instruction/skills/mcp.target`
  （Claude→CLAUDE.md、Codex→AGENTS.md、Gemini→GEMINI.md）。
- 内置运行时新增 Codex CLI / Gemini CLI 定义；Windows 进程管理改为 Popen + Job Object，
  补齐 stdout/stderr 重定向与 exit_code。
- reminder 调度：cron / at / when 三种形态，`governance.tick` 每轮触发并创建无任务 run；
  CLI `anvil reminder add --in/--when/--cron`、`fire-now`。
- 无任务 run 的 `runs.prompt` 注入；角色直接消息 `POST /roles/{slug}/message`。
- 三档重置：`/runs/{id}/restart`、`session-reset`、`full-reset`（自动备份 home），以及 `DELETE /runs/{id}`。

### v1.30 频道知识作用域（2026-10-25）
- 频道（`workspaces`）扩展 `knowledge_repo`；`workspace_members` → `workspace_participants`
  （无 access，仅作自动派发偏好/展示）；任务新增 `archived_at` 与归档 API；归档任务在频道页
  「任务」页签查看，未绑定任务在内置 `#无主题` 视图查看（独立归档页已移除）。
- 新增 `workspace_knowledge` + 版本表 + FTS：知识卡类型化校验、authority（normative/reference）、
  human approved / agent proposed、`expected_version`、已批准卡不可原地改（编辑生成新提案）。
- 新增 `task_knowledge`：任务参与角色共享；子任务知识带 `parent_task_id/source_subtask_id`
  回流父任务视图。
- 个人知识实践门控：`notes/memories` 记录 `practice_*`/`outcome`/`status/source_knowledge`；
  无成功证据写 draft，不进入检索/CIC。
- 子任务 guardrail：Agent 只能创建子任务、必须是父任务参与角色、继承
  `workspace/repo/base_branch` 且不可覆盖，写审计。
- 知识老化/冲突：`review_at`/`expires_at`、freshness 排序、重复/矛盾检测（双向
  `conflicts_json`）、复盘端点 `GET /workspaces/{id}/knowledge/review`、人工裁决
  `POST .../resolve-conflicts`；冲突未裁决不注入。
- CLI：`workspace knowledge list|propose|approve|harvest|review`、`task archive|unarchive|knowledge-*`、
  `memory/note` 实践证据参数、`knowledge card-template|card-validate`。
- v1.30.1 前端反馈修正：频道页新增「任务」页签（含已归档任务）与紧凑响应式表单；
  任务大厅固定展示全部未归档任务，不做频道筛选，仅保留「归档已完成」批量操作。
- 知识库页面统一作用域：频道/任务/角色/共享经验/演化提案/效果评估六个 tab；
  跨频道、跨任务聚合列表（人工审计），角色知识 index/search，
  效果评估只展示总体命中覆盖率、累计命中、零命中、治理待处理、任务知识覆盖率；
  频道/任务/角色/共享经验的具体条目行内展示单条命中次数与最近命中。
  删除频道时同步解绑任务/草案，任务进入 `#无主题`。
- 注入默认 `CHANNEL_KNOWLEDGE_ENABLED=0`，按频道试点后再开启；任务知识默认开启。

### 多角色团队任务（2026-10-18 修正）
- 新增 `task_role_claims`（task × role）：**每个参与角色独立认领/工作/提交**，不再由单一
  `assignee` 排他锁死；
- `claim` 对绑定角色（`task_assignees`）的任务允许并行认领；非参与角色仍被拒；
- `submit` 是角色级提交：记录该角色的 summary；**所有参与角色都提交后**任务才进入 `in_review`；
- 角色级 lease/heartbeat，某个角色失联只回收它自己的认领，不拖住其他角色；
- `unclaim` 只释放当前角色；人类可「人工收口并提交评审」；
- UI 任务详情展示每个角色的工作状态，并支持「派发全部参与角色」；
- 调度器 `_wake` 按 (task, role) 判重，不再因为一个角色已有 run 就跳过其他角色；
- `_reconcile` 只处理任务负责人 assignee 的 run，避免非负责人被误推入 reviewing。

### 任务与治理
- submit 时用真实 git 信息计算改动：worktree `git status -uall` + `base...agent/<slug>/<task>` diff；
  支持 `.anvilignore`（仓库根 / 数据目录）与 Anvil 投影产物豁免。
- 四层闸门口径：角色级（已有）、运行时级、系统级、每日 Token 预算；`GET/PUT /budget`。
- `POST /tasks/{id}/cancel|reopen`，`GET /tasks/{id}/diff|config-snapshot|bids|activities`。
- 任务列表按 type/tag 过滤；审计按 role/action/时间过滤；Token 聚合 `GET /tokens`。

### 记忆、知识与演化
- `memory_fts` 真正参与检索（不可用时回退 LIKE）；MEMORY.md + FTS 笔记 + 命中标签的共享经验注入 `CLAUDE.md`。
- lessons：列表/更新/批准/拒绝/删除；审批写入 `repos/<repo>/.anvil/shared/lessons.md`（无 repo 时 `data/shared/`），80 条硬上限。
- proposals：列表/批准（真正落到角色配置）/拒绝。
- `POST /me/instruction` 真正写 `instruction.md`。
- 角色配置版本：`GET /roles/{slug}/versions` + `POST /roles/{slug}/rollback/{version}`。
- `GET /roles/{slug}/home/file|history`、`GET /roles/{slug}/metrics`。
- 创建角色时自动附带每周复盘 reminder（design §8.5，幂等）。

### 工具治理
- `skills` CRUD + 版本 + 兼容性；`mcp_servers` CRUD + stdio 握商（initialize → tools/list）+ 工具缓存。
- 投影时：运行时自身 skill 目录优先，DB skill 兜底写 `SKILL.md`；运行时 MCP 配置与 DB 注册表合并。
- `tools.yaml` 预设（内置 + 用户覆盖）；allowlist → `.claude/settings.json` 的 `permissions.allow`。

### 前端
- 新增「知识库（共享经验 + 提案）」「提醒」「工具（skill/MCP/预设）」页面。
- 角色工坊新增主页（只读文件 + 发指令）、指标、版本回滚、工具配置编辑、裁剪精度展示。
- 治理页新增闸门上限编辑与成本分解；run 详情页新增重启/重置/释放按钮；任务详情支持取消/重新打开与多角色显示。

## 仍未实现 / 明确推迟

| 项 | 原因 / 建议 |
|---|---|
| 向量检索（sqlite-vec） | design 明确推迟到 v2，触发条件是共享经验超 80 条且检索不准 |
| `score` 加权认领 / bids | 默认先到先得；`/bids` 返回空列表 |
| 工具条件加载（按任务条件） | M3 未完成项；当前只有预设 + 静态 allowlist |
| 完整 MCP HTTP/SSE 握商 | 当前 stdio 完整握商，HTTP 只做可达性提示 |
| Windows 真机验收 | Job Object 已改为 Popen + AssignProcessToJobObject，stdout/exit_code 已补；需在 Windows 上跑 L5 |
| 容器/WSL2 强隔离 | design 列为 v4 |
| 默认每周复盘 reminder | ✅ 已实现（创建角色时自动附带） |
| 更多内置运行时 YAML | ✅ 已加 codex-cli / gemini-cli；其余按 §2.2 加 YAML 即可 |
| 完整首启向导 UI | CLI `anvil init --with-roles` 提供最小路径 |

## 本轮已补齐（2026-10-19，P0 可靠性 + P1-0 计划先行）

### P0 可靠性地基（实测五子棋任务暴露问题的修复）
- **续跑误杀修复（P0-1）**：`mark_spawned` 在每次 spawn（含 @mention 唤醒续跑）时重置 `started_at`；
  之前被唤醒的 run 会在「创建时间+600s」被超时误杀（实测 designer/developer/tester 三 run 同时死亡）。
- **超时单一真相（P0-2）**：spawn 时从运行时 YAML `invocation.timeout_seconds`（默认 3600s）冻结到
  `runs.timeout_seconds`；monitor 优先用 run 级值，`RUN_TIMEOUT_SECONDS` 仅兜底。
- **优雅停机 + 启动自愈（P0-3）**：lifespan shutdown 把活跃 run（running/projecting）终止子进程并转
  **suspended**（非 failed、不写 ended_at，可被 @mention 唤醒续跑）；startup `heal_stale_runs`
  清理崩溃遗留（孤儿进程先终止再转 suspended）。
- **失败分类（P0-4）**：`runs.failure_class`（platform_timeout / agent_exit_N / spawn_error）；
  process_exit 优先采用泵入分类；时间线与 run 详情展示。

### P1-0 计划先行编排（owner 2026-10-18 决策）
- 任务新增 `plan_first` / `planner` 字段（TaskIn/TaskOut/创建透传）。
- `POST /tasks/{id}/plan`：只唤醒 planner，注入 `PLANNING_PROTOCOL`（规划→草案→@human→挂起，不亲自实现）
  + 团队 Roster；其余角色等批准后按依赖自动派发。
- `anvil board draft --task --goal --tree-file`（agent CLI）提交工作流草案；`board drafts` 列表。
- 草案带 `source_task` 关联；**approve 后子任务落在父任务之下**（parent_id=主任务），带
  depends_on / assignees / acceptance / scope_paths；feedback_rules 并入子任务描述。
- 前端：大厅「计划先行」开关 + 规划者下拉；任务详情「生成工作流」按钮 + 待审批草案卡片
  （子任务/分工/依赖/反馈路径预览 + 批准落板）；suspended/失败分类展示。

### Inbox 信息架构重构（2026-10-19，参考 raft Activity feed / action card）
- `GET /api/v1/inbox`（新，只读聚合）：评审请求（in_review 任务）/ @human 提及（每任务最新一条，
  其后有评论或评审即视为已处理）/ 待审工作流草案 / 待审提案（lessons + proposals），created_at 倒序。
- 前端 `/inbox` 页面（导航首位）：tab 分区 + action card 式就地操作（评审通过/驳回、草案批准/驳回、
  lesson/proposal 批准/拒绝不跳页；@human 提及跳任务详情回复）；导航待办数徽章（SSE 实时刷新）。
- 大厅：in_review 看板卡「⏳ 等人类评审 → 收件箱」高亮直达；角色栏状态灯图例
  （灰点=空闲≠故障，design §11.1 未兑现项补齐）。
- 规格：`dev/spec-inbox-ia.md`；E2E 已验证（徽章/条目/tab 计数/高亮/图例）。

### 常驻角色运行时（2026-10-19，owner 架构修正）
- **每角色一个常驻 CLI 会话进程**（claude-code：`--print --input-format stream-json --output-format stream-json --verbose --session-id <uuid>`）：
  唤醒 = Anvil 通过 stdin 投递用户消息（**同一上下文，不新起进程**）；空闲 = 阻塞等待。
- **空闲释放**：空闲超过 `ANVIL_IDLE_RELEASE_MINUTES`（默认 15）优雅退出，`session_id` 持久化在
  `role_sessions`；下次唤醒 `--resume <session_id>` 恢复同一上下文（内存不常驻，上下文仍在）。
- **忙时排队**：一轮未结束时收到新消息进入队列，`result` 事件后合并投递（coalescing）。
- **崩溃/停机**：进程退出触发 `on_exit` → 活跃 run 走 process_exit；停机先关闭常驻进程，
  run 转 suspended；启动时 `role_sessions` 全部标记 dead（session_id 保留）。
- **成本自动记账**：`result` 事件的 `total_cost_usd` 直接写 `cost_ledger`（P3-1 提前落地）。
- **角色级会话令牌**：常驻进程使用绑定 role 的长期 token（task_id=NULL），跨任务可用。
- **Windows UTF-8 管道**：常驻进程 stdin/stdout 显式 UTF-8，stdin `newline="\n"`，避免中文
  prompt 在系统代码页/CRLF 下被 CLI 当成无效 stream-json（表现为 init 后 0 输出）；
  spawn 命令与 stdin 写失败会写入 `role-session.log`。
  reader 解码/管道异常会记录完整 traceback 并终止子进程，不再静默卡死。
  所有 `text=True` 子进程（git / npm / 运行时 health / MCP 握商）也统一 UTF-8，
  避免 Windows 下 diff、会话日志等窗口出现乱码。
- 测试：`tests/test_daemon.py`（同进程复用、忙时排队、退出后 --resume 同一 session_id、
  UTF-8 管道、reader 崩溃保护）；
  `ANVIL_PERSISTENT_RUNTIME=0` 可强制回退 one-shot。
- 实测验证：同一角色 @mention 二次唤醒 PID 不变；空闲释放后进程归零，再唤醒 `--resume` 同一 session 且回复上下文正确。

### 会话重置 / 失败后上下文隔离（2026-10-21）
- **restart**：终止角色常驻进程但保留 `role_sessions.session_id`，下一次唤醒 `--resume` 同一上下文。
- **session-reset / full-reset**：终止常驻进程并清空 `role_sessions.session_id`（及
  `last_context_tokens`），下一次 spawn 用 `args_new` 新建上下文；full-reset 另外清空
  MEMORY/notes（先备份）。
- **角色级 reset**：`POST /roles/{slug}/session-reset`（仅自然人），无需存在 run，便于 Cindy 等
  角色在失败/挂起后直接换新上下文。
- **失败自动清理**：`ANVIL_RESET_SESSION_ON_FAILURE=1`（默认）时，进程非零退出后自动清空
  session_id，下一次唤醒新建会话，避免坏 context 被反复 `--resume`；设为 0 可保留。
  常驻会话忙时超时（`platform_timeout`）由调度器显式 `reset_session`，同样换新上下文。
- 测试：`tests/test_daemon.py`（reset/restart/failure auto-reset）、
  `tests/api/test_role_session_reset.py`。

### Cindy 角色感知规划与角色建议（2026-10-22）
- **Roster 扩展**：capabilities / working_mode / skill_description / runtime / 并发上限 /
  当前活跃 run / 写权限仓库 / skills / MCP；Cindy 只使用 Roster 中的角色执行步骤。
- **0 角色也可派发**：`POST /tasks/{id}/dispatch` 在 onboarding 开启时调用 Cindy 先评估角色缺口；
  单角色仍直接执行；`ANVIL_ONBOARDING=0` 且无角色时 409。
- **两类草案**：角色足够 → `kind=workflow`；不足/无角色 → `kind=role_suggestion`
  （assessment / missing_capabilities / suggested_roles 结构化画像）。
- **角色建议审批**：批准后按结构化 definition 创建角色（保留 capabilities/description 等）、
  非破坏性追加 `task_assignees` + claims + workspace membership，并自动重规划；
  驳回则用现有 Roster 重规划；0 角色驳回时 @human 提示先绑定角色。
- **安全/一致性**：角色建议走 `services.roles.create`（HTTP `RoleIn` 不再丢字段）；
  已存在 slug 只绑定不覆盖；创建失败补偿删除；Cindy 不能 claim 任务；规划 run 加
  `[anvil:plan]` 标记，避免已有 workflow 时被误判为 REPLY_PROTOCOL；新 workflow 批准时
  supersede 同任务旧的 running workflow。
- 测试：`tests/api/test_role_suggestion_api.py`、`tests/test_planning.py`、
  `tests/api/test_dispatch_onboarding.py`、`tests/api/test_onboarding_guard.py`、
  `tests/api/test_inbox.py`。

### Workflow 切片闭环与实时进展（2026-10-22）
- **问题**：大型项目（如代码审计）由模型自由生成时容易退化成一条串行长链，
  缺少「执行 → 验证 → 修复 → 决策 → 下一片」的闭环。
- **运行期切片（plan_slices + loop）**：Cindy 不预先切片，只产出工作流骨架——
  一个 `kind=plan_slices` 的切片计划步骤 + 一个 `loop` 闭环模板。切片计划步骤由
  具备项目理解的角色（审计负责人/架构师/tech lead）执行：盘点目录/模块/依赖/风险面后，
  用 `anvil step plan <wf> <step> --file slices.json` 提交切片清单。
- **服务端确定性展开**：`services.workflow.apply_slice_plan` 按 loop 模板为每片生成
  `<plan>.<slice>.scope? → execute[...] → verify → fix → verify2 → decide`；
  下一片依赖上一片 `decide`；`decide` 为 `kind=decision`，
  `on_reject=[fix, verify2]`、`max_rework=2`，approve 后解锁下一片。
  切片计划步骤的下游静态步骤（如最终报告）会自动改挂到最后一片 `decide`。
- **兼容静态切片**：若 human 已给出明确切片清单，仍支持 `slices` 定义，
  `_expand_slices` 直接展开（保留兼容）。
- **结构校验**：`validate_definition` 在草案创建/落板时拒绝「≥6 步、≥2 角色、
  既无 decision 也无 plan_slices」的串行工作流，返回可执行的修正要求；
  planner 可据 409 立即重写。
- **实时进展**：步骤 running/submitted/planned/approved/rework/rejected/failed 自动写入
  任务会话（`activities.kind=step`），前端会话时间线新增「工作流」筛选；决策步骤 prompt
  列出整片各步骤的 summary/acceptance，决策角色据此评估。
- **图谱折叠**：动态切片不展开全部步骤；图谱用一个「↻ 切片循环」节点表达逻辑闭环，
  节点实时显示总片数/总步数、已完成片数、当前片·阶段（本片 done/total）和进度条；
  点击循环节点会展开**当前片的单一闭环流程**
  （scope → execute → verify → fix → verify2 → decide，当前阶段高亮），
  详情面板自动滚入视野，
  并可在切片列表中切换查看任意一片；列表视图按「循环 → 切片 → 步骤」三级折叠，
  默认只显示骨架。
- **规划协议**：大型/审计类任务必须用 `plan_slices + loop`；切片由执行角色在运行时
  基于对项目的理解完成；`execute` 可拆细（inventory / static-audit / dataflow / poc），
  每步必须 comment 进展、mention 交接、发现问题立即升级。
- 测试：`tests/test_workflow.py`、`tests/api/test_workflow_api.py`、
  `tests/test_planning.py`。

### 子任务 claim / 并发修复（2026-10-22）
- **并发闸门按活跃 run 计数**：只统计 `created/projecting/running` 的 run；
  空闲 `claim`（in_progress 但没有 run）和 `suspended/submitted/reviewing` 会话不再占用
  `max_parallel`，避免角色持有子任务后把自己的并发额度占满、无法被调度。
- **子任务显式指派**：`anvil task create` 新增 `--assignee <角色>` / `--assignees a,b`；
  `_assigned_roles` 在无 team assignees 时也会把单负责人 `assignee` 纳入自动派发。
- **claim 冲突可执行提示**：单负责人任务被其他角色独占时，409 直接说明
  “ask <owner> to unclaim or add you as a team assignee”，不再只返回笼统的 not claimable。
- **协议约束**：`PROTOCOL` / `STEP_PROTOCOL` / `REPLY_PROTOCOL` 均要求
  拆给别人的子任务用 `--assignee` 指定执行者，创建者不要 claim；已 claim 需先
  `anvil task unclaim` 再 @ 对方认领。
- 测试：`tests/test_task_concurrency.py`（空闲 claim 不占额度 / 活跃 run 占额度 /
  独占 claim 提示 / assignee 自动派发）。

### 角色 slug 校验（2026-10-22）
- slug 统一为 `^[a-z0-9][a-z0-9_-]{1,63}$`；`roles.create` 与角色建议审批共用
  `validate_slug`，避免中文/大写/空格/slug 进入 `agents/<slug>/` 路径与分支名。
- `POST /roles`、`/roles/auto`、`/roles/expand` 非法 slug 返回 422（带中文提示）；
  角色工坊实时显示规则并在非法时禁用「新建角色」。
- 测试：`tests/api/test_role_slug_api.py`。

### 任务详情显示优化（2026-10-22）
- 侧栏长描述默认折叠为 6 行；用 ResizeObserver 按实际溢出显示「展开全文 / 收起描述」，
  展开后最高 42vh 并在描述区内滚动，避免把任务信息面板撑得过长。
- 长任务标题支持任意位置换行，状态徽章可随标题换行。

### Windows 子进程输出编码（2026-10-22）
- **one-shot spawn**：Windows 下给子进程注入 `PYTHONIOENCODING=utf-8`、
  `PYTHONUTF8=1`、`PYTHONLEGACYWINDOWSSTDIO=0`，减少 Python 工具按 GBK 输出。
- **常驻会话**：stdin 以 UTF-8 字节帧写入（LF 换行）；stdout/stderr 改为二进制管道，
  reader 用 `decode_child_output` 逐行按 UTF-8 → 本地编码 → GB18030/GBK 回退解码，
  避免中文 Windows 的 GBK 输出被 UTF-8 解码成乱码。
- **运行日志读取**：`get_run_log` 同样使用 `decode_child_output`，兼容 UTF-8 与 GBK
  行混排的历史日志。
- 测试：`tests/test_log_encoding.py`、`tests/test_daemon.py`。

### 主页「实时活动」重构（2026-10-19，owner 反馈）
- 右栏改为两块：**实时会话**（仅活跃 run，含挂起等待；卡片可点击进 `/runs/{id}`）
  + **最近活动（30 分钟）**（过滤心跳/删除，最多 20 条）。
- 完整历史移到独立页面 **`/activity`**（活动历史）：全量审计、按角色/动作过滤、200/500/1000 条。
- 后端 `GET /runs?active=true`：只返回活跃状态（created/projecting/running/submitted/reviewing/suspended）。

### Onboarding 助理 Cindy + 角色 AI 创建（2026-10-19，owner 决策）
- **计划先行默认化**：多角色任务新增统一派发入口 `POST /tasks/{id}/dispatch`：
  自动唤醒内置规划助理 **cindy**（`kind=onboarding`，不占用任务参与角色名额）→ 分析任务并
  设计工作流（阶段顺序、可并行关系 DAG、分工、验收/scope、反馈与升级路径）→ `board draft` →
  `@human` 等待批准。人在收件箱批准后子任务落板，父任务转 `in_progress`（epic），依赖感知派发。
  单角色任务直接派发；已有待审草案时返回 `pending_review` 不重复规划；`ANVIL_ONBOARDING=0` 关闭。
- **前端**：任务详情「派发（先由 Cindy 规划）」；大厅拖拽/指派走 dispatch；
  规划中提示「草案将出现在收件箱」。
- **角色 AI 生成创建**：`POST /roles/auto {brief}` —— 自然人一句话描述 → **默认 CLI 运行时**生成
  display_name/description/capabilities/responsibilities/system_prompt 并直接创建角色；
  角色工坊新增「AI 生成并创建」输入。`RoleOut` 现暴露 `definition`。
- **默认运行时选择**：`GET/PUT /runtimes/default`（PUT 仅自然人）。AI 生成画像/角色经
  `services/utility.py` 调用该运行时；角色详情「生成画像」优先使用角色自己的运行时。
  不再依赖后端单独配置 `ANTHROPIC_*`（owner 2026-10-21）。
- **运行时 YAML 编辑**：`GET/PUT /runtimes/{id}/definition`（PUT 仅自然人）。运行时页可
  新增自定义运行时（如 `codeagent`）或覆盖内置定义；保存后自动探测并落库。YAML 写入
  用户运行时目录（Windows `%APPDATA%\Anvil\runtimes`，macOS `~/.config/Anvil/runtimes`）。
- **角色运行时绑定**：`PUT /roles/{slug}/runtime`（仅自然人）只替换 runtime，保留
  system_prompt/职责/工具等其它定义。角色工坊创建角色时可选任意已注册运行时；角色详情
  「运行时」tab 可改绑。`POST /roles/auto` 会把新角色绑定到实际使用的运行时。
- **角色技能/MCP 定制绑定**：新角色默认 `skills=[]` / `mcp=[]`；角色详情「技能/MCP」tab
  按角色独立勾选并保存，互不影响；「全部不选并保存」可一键清空。投影只使用该角色的勾选项。
- **角色切换/新建状态隔离**：切换或新建角色时清空上一角色的生成画像、profile、日志等前端状态，
  并让进行中的生成响应失效；创建后自动选中新角色。后端画像本身按 role 存储，不继承。
- **跨平台 home 修复**：启动时 `repair_home_paths` 把 `roles.home_path` 重算为
  `AGENTS_DIR/<slug>` 并补齐 home 文件；`roles._row` 读取时也按当前数据目录归一化，
  解决 macOS → Windows 迁移后角色 cwd 指向旧绝对路径的问题。
- **生成画像诊断**：`/roles/{slug}/generate` 校验 4 个字段；缺失/为空时返回 502，
  detail 带缺失字段、耗时与原始输出片段，同时写 `role profile generation incomplete` 日志。
  前端遇到不完整结果时保留原有系统提示/职责，不再用空值覆盖。
- **Windows .cmd wrapper 规避**：运行时发现/调用时优先原生 `.exe`；`path_lookup` 先查
  `claude.exe` / `codex.exe` / `gemini.exe`，`.cmd/.bat` 仅作最后兜底并写入探测日志。
  已有 `.cmd` 注册在 probe/spawn/utility 时自动升级为同目录/原生安装/包内的 `.exe`。
- 测试：`tests/api/test_dispatch_onboarding.py`、`tests/api/test_role_auto.py`、
  `tests/api/test_role_catalog.py`、`tests/api/test_role_generate.py`、
  `tests/api/test_role_profile.py`、`tests/api/test_role_runtime.py`、
  `tests/api/test_runtime_definition.py`、`tests/test_catalog_projection.py`、
  `tests/test_role_home_portability.py`、`tests/test_utility.py`。

### 任务大厅 UI 重构（2026-10-19，参考 multica/raft）
- **「＋ 新建任务」弹窗**：任务名称、任务描述、参与成员多选（可滚动列表，显示角色定位）；
  创建后直达任务会话页。大厅工具栏不再内联勾选角色。
- **任务详情页签**：`会话`（聊天室式交互时间线 + 以 human 身份评论/@提及；输入 @ 弹出角色联想）｜`子任务`。
- **子任务页签**：① Cindy 落板的子任务树（状态 + 直达）；② 每个参与角色的当前会话状态
  （待办/进行中/待确认/完成/关闭）+ 最近 run；③ 点击角色行展开「执行内容（日志）」与
  「历史上下文（时间线）」，并可跳转完整 run 页面。SSE 实时刷新。
- 派发统一走 `dispatch`：多角色 → Cindy 规划；单角色 → 直接执行。

### Workflow 模型（2026-10-19，owner 修正）
- **Task = 人的高层工作空间；Workflow = 步骤状态机**（`workflows` / `workflow_steps`），步骤不再是 task 行。
- 执行引擎（scheduler 每轮推进）：依赖满足且角色空闲 → 创建步骤 run；**同一角色一次只跑一个步骤**；
  跨角色可并行；步骤完成后 `anvil step submit <wf> <step>`；非 gate 自动通过，gate 进入收件箱人工审核；
  全部步骤完成 → 任务 `in_review` 最终评审。
- API：`GET /tasks/{id}/workflow`、步骤 submit/review/retry；CLI：`anvil step submit|show`、
  `anvil board draft --kind workflow`；Cindy 协议改为产出 workflow（steps + gate），不再拆任务。
- 收件箱新增 `gate` 类型（步骤审核，就地通过/驳回）。
- 大厅只显示高层任务（移除角色卡列）；任务详情页签：会话 / 工作流 / 角色运行时。
- **会话流优化**：任务详情会话支持类型筛选（全部/对话/@提及/运行/评审）、默认只显示最近 40 条
  并可「显示更早」分页、容器固定高度内部滚动（62vh）、仅在接近底部时自动滚动。
  @ 提及消息的发送/存储/渲染会去掉正文开头重复的 `@角色`，只保留标签一次。
- **跨角色协作与挑战**：步骤 run 的提示词注入最近任务会话（含 @ 反馈）；工作流任务上被 @ 的角色
  使用 REPLY_PROTOCOL（只讨论、不 claim/submit）；STEP_PROTOCOL 要求关键产出后主动 @ 相关角色
  交叉确认/挑战、发现上游问题直接指出、并在提交摘要中说明反馈处理结果；Cindy 规划时把交叉评审点
  写进步骤 instructions（gate 步骤需交叉评审证据）。
- **工作流图谱**：任务详情「工作流」页签默认以分层 DAG 展示——同一列=可并行层，箭头=依赖，
  节点颜色=状态（待办/进行中/待审核/完成/驳回/失败），◆=审核门；点击节点查看指令/验收/摘要
  并就地通过/驳回/重试；支持缩放与「适应宽度」。列表视图可切换。
- 已有「跳棋」任务的 10 个子任务已迁移为 10 个 workflow 步骤（S1/S10 为审核门）。

## P0 修复（2026-10-19，安全优先）

### P0-1 人工确认点硬边界（human-only）
- 新增 `deps.require_human`：带角色身份的会话令牌（Agent）调用人工确认端点一律 **403**。
- 覆盖：
  - 任务：`PATCH /tasks/{id}`、`DELETE /tasks/{id}`、`cancel`、`reopen`、`assign`、`review`
  - 工作流：gate `review`、`retry`
  - 草案/知识/提案：`board/drafts/approve|reject`、lesson/proposal `approve|reject`
  - 运行：`restart`、`session-reset`、`full-reset`、`DELETE /runs/{id}`
  - 治理：`PUT /budget`、`freeze-all`、`resume-all`
  - 角色/运行时变更：角色 auto-create/update/tools/catalog/profile/clone/delete/rollback、
    运行时 update/delete
- 实测：Agent 令牌审核任务/修改角色均返回 403；自然人访问收件箱正常。
- 安全回归测试：`tests/api/test_security_human_only.py`。

### P0-2 builder ≠ verifier（第一阶段）
- `workflow_steps.reviewer` 字段：Cindy 规划时为 gate 步骤指定验证角色；
  `create_workflow` 强制 **reviewer ≠ 执行角色**（相同则拒绝创建）。
- Cindy 协议新增「builder 永不是 verifier」与 gate 验证者要求；前端图谱/列表展示验证者。
- 说明：完整「验证步骤先于 gate 通过」的执行链仍属 P1。

### P0-3 上下文注入预算
- 步骤/回复提示词注入最近会话限制 **1800 字符**；普通任务转录限制 **4000 字符**；
  超出保留尾部并显式标注「更早的会话已省略」。
- 测试：`tests/graph/test_prompt_context.py::test_transcript_is_clipped_to_budget`。

## P1 实施（2026-10-19）

### P1-1 Agent Inbox（拉模式）
- 新表 `role_inbox_cursors`（role × task）；`services/agent_inbox.py`：`items`（check，不推进）、
  `commit`（cursor 只前进，at-least-once）、`unread_count`。
- API：`GET /me/inbox?task_id=`、`POST /me/inbox/commit`；CLI：`anvil inbox check|commit`。
- 步骤提示词改为「未读 N 条 + 最近 3 条 + 拉取/提交指针」，不再全量 push 会话；STEP_PROTOCOL
  要求先 `anvil inbox check`、处理完 `commit` cursor。

### P1-2 Held Draft（简化）
- `comment` / `mention` 支持 `expect_activity_id` 与 `force`；Agent 基于旧版本发送时返回
  **409 held + 期间新内容**，可选择重写 / 原样（force）/ 沉默；人类消息不受影响。
- CLI：`anvil task comment|mention --expect-activity <id> [--force]`。

### P1-3 CIC 冷唤醒（简化）
- `role_sessions.last_context_tokens` 记录结果事件 token；空闲释放后若超过
  `ANVIL_CIC_THRESHOLD_TOKENS`（默认 128K）→ 不 `--resume`，新建会话并注入构造上下文：
  MEMORY.md 头部 / 最近 25 条消息 / 最近 12 条动作；不宣布重置、不列未决问题。
- 阈值可配；测试覆盖构造内容与三负规则。

### P1-4 身份保鲜（轻量）
- 周复盘 prompt 增加「检查 description 是否漂移；用 lesson propose + @human，不自行改写」。

## P2 实施（2026-10-19）

### P2-1 契约先行（contract-first）
- `workflow_steps.contract`：Cindy 规划时为实现步骤写不变量/接口承诺；创建时落库。
- 步骤提示词自动注入**上游步骤契约** + 本步骤契约（违反视为失败）；规划协议要求实现步骤必须写 contract。
- 前端工作流图谱/列表展示契约。

### P2-2 验证矩阵
- 工作流定义顶层 `verification`（cell / method / verifier_role）；Cindy 协议要求产出；
- `GET /tasks/{id}/workflow` 返回；前端工作流页签展示矩阵。

### P2-3 失败归因
- `workflow_steps.failure_reason`：调度器把 run 失败映射为 `environment`（timeout/spawn）或
  `agent_error`；步骤失败通知带归因，前端展示。

### P2-4 Action Cards（HITL 一等原语）
- 新表 `action_cards`；服务/API/CLI：Agent 只能 `anvil action prepare`（白名单 `task.cancel`、
  `task.reopen`），自然人批准后**以人的身份执行**；拒绝则不执行。
- 收件箱新增「操作申请」类型（就地批准执行/拒绝）；批准端点 human-only。
- 测试：`tests/api/test_action_cards.py`。

## Raft 式子任务与团队扩展（2026-10-19）

- **临时子任务**：Agent 用 `anvil task create --title ... --description ... --parent $ANVIL_TASK_ID`
  创建子任务；自动继承父任务的 repo/workspace/base_branch。创建给其他角色时用
  `--assignee <role>`（团队用 `--assignees a,b`）显式指派，**创建者不要先 claim**；
  单负责人任务一旦被 claim 就是排他的，需先 `anvil task unclaim` 才能转交。
  子任务**不进入大厅**，只在父任务详情新增的「子任务」页签展示（状态/认领角色/直达）。
- **API**：`GET /tasks?parent_id=` 过滤子任务。
- **Agent 自行扩展团队**：`POST /roles/expand` / `anvil role expand --brief "..."`：
  创建新 agent 并**继承创建者的 runtime/permission_mode**；新角色为普通 member，不能设置 tools/高权限字段；
  每日配额 `ANVIL_AGENT_ROLE_EXPAND_DAILY`（默认 3）、总角色上限 `ANVIL_MAX_ROLES`（默认 20）；
  创建动作写 audit `role_expand`，并在关联任务里 @human 通知。
- **协议**：`PROTOCOL` / `STEP_PROTOCOL` / `REPLY_PROTOCOL` 均要求拆分给其他角色的子任务用
  `--assignee` / `--assignees` 显式指派，创建者不要 claim；已 claim 需先 unclaim 再交接。
  同时保留「扩展团队 → role expand」指引。
- 测试：`tests/api/test_subtasks.py`、`tests/api/test_role_expand.py`。

## Token 统计（2026-10-19，替代不准确的「今日成本」）

- 新表 `token_usage`：daemon 从 CLI `result.usage`（兼容 `modelUsage` 聚合）记录
  input/output/cache_read/cache_creation，带 role/task/run 维度。
- `GET /tokens` 聚合：总输入（含 cache）/ 总输出 / 总 token / 今日 / 按角色 / 按日。
- 前端顶栏：**「Token 总消耗（入 X / 出 Y）」**；治理页展示累计 Token；成本账本保留用于预算闸门。
- 说明：历史运行未保存 token 用量，统计从下一次 agent 回合开始累积；成本字段不再作为顶栏展示。

## P3 实施（2026-10-19）

### P3-1 Cleaner（记忆自修剪）
- 每轮 tick 本地检查角色 `MEMORY.md` 体积（零 token）；超过 `ANVIL_CLEANER_MAX_BYTES`（默认 12KB）
  且当天未提醒 → 生成一条自修剪 reminder（合并重复、删过时、lesson propose、≤300 行）；
  `settings cleaner:<role>` 保证每天至多一次；`ANVIL_CLEANER=0` 关闭。
- 测试：`tests/test_cleaner.py`。

### 尚未实施（P3 其余）
- trace coverage / mutation testing 接入、CIC 多模型 judge 评测、Activity Unread/Saved、DM UI。

## 验证

```bash
cd dev
python3 -m pytest tests/ -q          # 242 passed, 21 skipped
cd frontend && npm run build         # tsc + vite build
```

> 注意：`dev/.venv` 若未安装 `langgraph`，`tests/conftest.py` 的 `importorskip` 会把全部测试
> 静默 skip。请使用装有 `backend/requirements.txt` 的环境运行测试。
