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
- 四层闸门口径：角色级（已有）、运行时级、系统级、日成本；`GET/PUT /budget`。
- `POST /tasks/{id}/cancel|reopen`，`GET /tasks/{id}/diff|config-snapshot|bids|activities`。
- 任务列表按 type/tag 过滤；审计按 role/action/时间过滤；成本聚合 `GET /costs`。

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
- 测试：`tests/test_daemon.py`（同进程复用、忙时排队、退出后 --resume 同一 session_id）；
  `ANVIL_PERSISTENT_RUNTIME=0` 可强制回退 one-shot。
- 实测验证：同一角色 @mention 二次唤醒 PID 不变；空闲释放后进程归零，再唤醒 `--resume` 同一 session 且回复上下文正确。

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
- **角色 AI 生成创建**：`POST /roles/auto {brief}` —— 自然人一句话描述 → LLM 生成
  display_name/description/capabilities/responsibilities/system_prompt 并直接创建角色；
  角色工坊新增「AI 生成并创建」输入。`RoleOut` 现暴露 `definition`。
- 测试：`tests/api/test_dispatch_onboarding.py`、`tests/api/test_role_auto.py`。

### 任务大厅 UI 重构（2026-10-19，参考 multica/raft）
- **「＋ 新建任务」弹窗**：任务名称、任务描述、参与成员多选（可滚动列表，显示角色定位）；
  创建后直达任务会话页。大厅工具栏不再内联勾选角色。
- **任务详情页签**：`会话`（聊天室式交互时间线 + 以任意身份评论/@提及）｜`子任务`。
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
- **跨角色协作与挑战**：步骤 run 的提示词注入最近任务会话（含 @ 反馈）；工作流任务上被 @ 的角色
  使用 REPLY_PROTOCOL（只讨论、不 claim/submit）；STEP_PROTOCOL 要求关键产出后主动 @ 相关角色
  交叉确认/挑战、发现上游问题直接指出、并在提交摘要中说明反馈处理结果；Cindy 规划时把交叉评审点
  写进步骤 instructions（gate 步骤需交叉评审证据）。
- **工作流图谱**：任务详情「工作流」页签默认以分层 DAG 展示——同一列=可并行层，箭头=依赖，
  节点颜色=状态（待办/进行中/待审核/完成/驳回/失败），◆=审核门；点击节点查看指令/验收/摘要
  并就地通过/驳回/重试；支持缩放与「适应宽度」。列表视图可切换。
- 已有「跳棋」任务的 10 个子任务已迁移为 10 个 workflow 步骤（S1/S10 为审核门）。

## 验证

```bash
cd dev
python3 -m pytest tests/ -q          # 156 passed, 21 skipped
cd frontend && npm run build         # tsc + vite build
```

> 注意：`dev/.venv` 若未安装 `langgraph`，`tests/conftest.py` 的 `importorskip` 会把全部测试
> 静默 skip。请使用装有 `backend/requirements.txt` 的环境运行测试。
