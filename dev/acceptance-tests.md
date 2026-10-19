# Anvil 验收测试用例清单

> 覆盖已落地的 M1+M2 核心、**LangGraph 编排控制面**、**任务会话视图**与**角色执行追溯**。
> 标注「**自动化**」的用例已在 `tests/` 落地（pytest，当前 122 passed + 21 skipped）；
> 标注「**手工**」的需在 UI 上验收。
>
> 环境：后端 `python3 run_server.py`（调度器开启）；前端 `npm run dev`（dev）或 build 后由后端托管 `http://127.0.0.1:7317/`。

## 用例总览

| ID | 模块 | 用例 | 优先级 | 类型 |
|---|---|---|---|---|
| ACC-A1 | 运行时 | 注册运行时并列表 | P0 | 自动化 |
| ACC-A2 | 运行时 | 本地自动发现 + 版本探针 | P1 | 自动化 |
| ACC-A3 | 运行时 | 手工注册自定义运行时 | P1 | 自动化 |
| ACC-A4 | 运行时 | 编辑定义后重探（definition_hash 变更） | P2 | 自动化 |
| ACC-B1 | 角色 | 角色 CRUD + 私有 home | P0 | 自动化 |
| ACC-B2 | 角色 | 工具清单超预算返回 422 | P1 | 自动化 |
| ACC-B3 | 角色 | effective-config 预览 | P1 | 自动化 |
| ACC-C1 | 任务 | 任务五态机全路径 | P0 | 自动化 |
| ACC-C2 | 任务 | 并发认领原子性（恰一方成功） | P0 | 自动化 |
| ACC-C3 | 任务 | 依赖守卫（depends_on / 子任务 done） | P0 | 自动化 |
| ACC-C4 | 任务 | 租约心跳与超时回收 | P0 | 自动化 |
| ACC-C5 | 任务 | diff 越界（scope / role access / .anvilignore） | P0 | 自动化 |
| ACC-D1 | 编排 | wake→spawn：运行生命周期拉起子进程 | P0 | 自动化 |
| ACC-D2 | 编排 | submit→interrupt→review approve：run completed | P0 | 自动化 |
| ACC-D3 | 编排 | review reject：run 重新 spawn | P0 | 自动化 |
| ACC-D4 | 编排 | 进程退出：非零→failed / 零→abandoned | P0 | 自动化 |
| ACC-D5 | 编排 | 已 submit 的 run 进程退出不覆盖状态 | P1 | 自动化 |
| ACC-D6 | 编排 | 调度器：派发 + monitor + 租约回收 | P0 | 自动化 |
| ACC-D7 | 编排 | 检查点耐久（重启后仍能 resume） | P1 | 自动化 |
| ACC-D8 | 编排 | 幂等（重复 pump 不二次 spawn） | P1 | 自动化 |
| ACC-D9 | 编排 | 运行日志落盘 + 快照可追溯 | P0 | 自动化 |
| ACC-E1 | 任务会话 | 时间线合并排序（动作+评论+运行事件） | P0 | 自动化 |
| ACC-E2 | 任务会话 | 以任意身份评论并实时刷新 | P0 | 手工 |
| ACC-E3 | 任务会话 | 多角色认领/提交/驳回/评论交互流 | P1 | 手工 |
| ACC-E4 | 任务会话 | agent 提交后自动 @human 通知评审（HITL） | P0 | 自动化 |
| ACC-E5 | 任务会话 | 自然人 @角色 注入注意事项/上下文 | P0 | 自动化 |
| ACC-E6 | 任务会话 | 角色间 @角色 交互（agent 通过 CLI 直接 @提及） | P0 | 自动化 |
| ACC-F1 | 执行追溯 | 角色执行历史列表（run 会话） | P0 | 手工 |
| ACC-F2 | 执行追溯 | run 详情：生命周期时间线 + 日志 + 快照 | P0 | 手工 |
| ACC-G1 | 治理 | 全场冻结/恢复 | P0 | 自动化 |
| ACC-G2 | 治理 | 成本闸门 + 并发闸门 | P1 | 自动化 |
| ACC-G3 | 治理 | 审计日志 | P1 | 自动化 |
| ACC-H1 | 实时 | SSE 事件流（连接 + 事件推送） | P1 | 自动化 |
| ACC-H2 | 实时 | 前端 SSE 驱动刷新（角色灯/时间线/活动流） | P1 | 手工 |
| ACC-I1 | 端到端 | 五子棋场景驱动 4 角色走完整闭环 | P0 | 脚本 |
| ACC-J1 | 多角色 | 任务绑定多角色一起实施（团队协作） | P0 | 自动化 |
| ACC-J2 | 角色治理 | 角色按运行时默认 skill/MCP 勾选加载 | P0 | 自动化 |
| ACC-J3 | 角色治理 | 角色 system prompt + 职责配置与注入 | P1 | 自动化 |
| ACC-J4 | 角色治理 | LLM 自动生成角色画像 | P0 | 手工 |
| ACC-J5 | 编排 | agent 工作交互（思考评论 + @协作角色） | P0 | 自动化 |
| ACC-J6 | 编排 | 运行超时回收（monitor 标 failed） | P1 | 自动化 |
| ACC-J7 | 编排 | 工作流模板库（SOP 预设拆解） | P0 | 自动化 |
| ACC-J8 | 编排 | 依赖感知调度 + 上游完成 @交接下游 | P0 | 自动化 |
| ACC-J9 | 编排 | @ 即拉入（@角色自动唤醒该角色） | P0 | 自动化 |
| ACC-J10 | 编排 | 多轮挂起/resume（角色反复被唤醒续跑） | P0 | 自动化 |
| ACC-K1 | 长程 | reminder 到期触发（cron/at/when） | P0 | 自动化 |
| ACC-K2 | 长程 | 三档重置 + 释放进程 | P1 | 自动化 |
| ACC-K3 | 记忆 | FTS5 检索 + 记忆/经验注入 | P0 | 自动化 |
| ACC-K4 | 记忆 | lessons 升格审批 + 80 条上限 | P0 | 自动化 |
| ACC-K5 | 演化 | proposal 审批真正落配置 | P0 | 自动化 |
| ACC-K6 | 治理 | 真实 git diff 越界校验 + `.anvilignore` | P0 | 自动化 |
| ACC-K7 | 治理 | 系统/运行时并发 + 日成本闸门 | P0 | 自动化 |
| ACC-K8 | 安全 | 运行级会话令牌绑定 role/task（防冒充） | P0 | 自动化 |
| ACC-K9 | 工具 | skill/MCP CRUD + stdio 握商 + 投影 | P0 | 自动化 |
| ACC-K10 | 追溯 | 角色版本/回滚、home 文件读取、metrics | P1 | 自动化 |
| ACC-K11 | 多角色 | 同一任务多角色并行认领、各自提交、全部提交后评审 | P0 | 自动化 |

## 详细用例

### A. 运行时管理

#### ACC-A1 注册运行时并列表 【自动化 P0】
- 前置：空库。
- 步骤：`POST /api/v1/runtimes`（id=claude-code，executable_path、status=ready、capabilities）→ `GET /api/v1/runtimes`。
- 预期：返回 201；列表中含 claude-code，字段完整（status/executable_path/capabilities）。

#### ACC-A2 本地自动发现 + 版本探针 【自动化 P1】
- 步骤：`POST /api/v1/runtimes/discover`。
- 预期：扫描 `runtimes/*.yaml`，按 discovery 策略定位可执行并落库，status ∈ ready/unverified/missing。

#### ACC-A3 手工注册自定义运行时 【自动化 P1】
- 步骤：`POST /api/v1/runtimes` 带 `executable`（绝对路径）。
- 预期：写用户目录 YAML + 探针 + 落库，`GET /runtimes/{id}` 返回该定义。

#### ACC-A4 编辑定义后重探 【自动化 P2】
- 步骤：`PUT /api/v1/runtimes/{id}` 改 executable_path → `POST /runtimes/{id}/probe`。
- 预期：可编辑；probe 更新 last_probe_at/last_probe_log；definition_hash 变更被记录。

### B. 角色管理

#### ACC-B1 角色 CRUD + 私有 home 【自动化 P0】
- 步骤：`POST /roles`（slug/display_name/runtime/workspace/tools/concurrency）→ `GET /roles/{slug}` → `PUT` → `DELETE`。
- 预期：创建后 `agents/{slug}/` 生成 home（profile.yaml/instruction.md/MEMORY.md/notes）；CRUD 一致；DELETE 生效。

#### ACC-B2 工具清单超预算返回 422 【自动化 P1】
- 步骤：`PUT /roles/{slug}/tools` 设 max_tool_tokens 小于估算。
- 预期：返回 422 并附 estimate 明细；预算内则 200。

#### ACC-B3 effective-config 预览 【自动化 P1】
- 步骤：`GET /roles/{slug}/effective-config`。
- 预期：返回 runtime/workspace/tools/concurrency 投影结果。

### C. 任务板与认领

#### ACC-C1 任务五态机全路径 【自动化 P0】
- 步骤：create→claim→submit→review(approve)=done；review(reject)=in_progress→submit→review=done；unclaim→todo。
- 预期：状态迁移符合 todo/in_progress/in_review/done/closed，审计留痕。

#### ACC-C2 并发认领原子性 【自动化 P0】
- 步骤：两个角色 `asyncio.gather` 并发 claim 同一任务。
- 预期：恰一方 200、另一方 409；assignee 为胜者。

#### ACC-C3 依赖守卫 【自动化 P0】
- 步骤：建 A→B 依赖；claim B（A 未 done）→ 409；父任务在子任务 done 前 submit → 409。
- 预期：两个方向守卫正确（claim 查 depends_on、submit 查子任务 parent_id）。

#### ACC-C4 租约心跳与超时回收 【自动化 P0】
- 步骤：claim 后 heartbeat 顺延 lease；伪造过期 lease 后 tick。
- 预期：heartbeat 续租；tick 回收过期 → task 回 todo、run 转 abandoned。

#### ACC-C5 diff 越界 【自动化 P0】
- 步骤：submit 带越界 changed_paths（scope 外 / role read-only repo / .anvilignore 前缀）。
- 预期：409 拒绝；范围内通过。

### D. 编排执行引擎（LangGraph 控制面）

#### ACC-D1 wake→spawn 【自动化 P0】
- 步骤：建任务 + `POST /runs`（或调度器 tick 派发）。
- 预期：run 走 created→projecting→running；`ProcessManager.spawn` 被调用；`native_session_id` 落库；角色灯转绿。

#### ACC-D2 submit→interrupt→approve 【自动化 P0】
- 步骤：wake 后 claim→submit→review(approve=true)。
- 预期：run running→submitted→reviewing（interrupt 挂起）→completed；task done。

#### ACC-D3 review reject 重新 spawn 【自动化 P0】
- 步骤：submit 后 review(approve=false)。
- 预期：run →projecting→running，旧会话被 terminate，重新 spawn。

#### ACC-D4 进程退出 【自动化 P0】
- 步骤：运行中进程退出（非零/零）。
- 预期：非零→failed；零→abandoned（§7.4 退出未 submit 永不 completed）。

#### ACC-D5 已 submit 的 run 退出不覆盖 【自动化 P1】
- 步骤：submit 后 run 处于 reviewing，进程退出事件重入。
- 预期：run 仍 reviewing，不误转 failed/abandoned。

#### ACC-D6 调度器 【自动化 P0】
- 步骤：起调度器，观察 tick：回收租约、为 todo 派发 run+spawn、monitor 探测退出、reconcile 对账。
- 预期：todo 任务被派发并 spawn；进程退出被 monitor 收尾；任务态与 run 态不一致时 reconcile 自愈。

#### ACC-D7 检查点耐久 【自动化 P1】
- 步骤：submit 至 interrupt 挂起 → 换全新 graph/saver 指向同一 graph-db。
- 预期：仍能 `Command(resume)` 恢复并完成。

#### ACC-D8 幂等 【自动化 P1】
- 步骤：同一 run 重复 pump submit/wake。
- 预期：不二次 spawn、不重复迁移。

#### ACC-D9 日志+快照可追溯 【自动化 P0】
- 步骤：wake 后查 `GET /runs/{id}/log`、`/snapshot`、`/timeline`。
- 预期：log 含子进程 stdout/stderr + 头部；snapshot 为冻结 profile；timeline 列出 projecting/running 迁移。

### E. 任务会话视图

#### ACC-E1 时间线合并排序 【自动化 P0】
- 步骤：claim→comment→submit→review 后 `GET /tasks/{id}/timeline`。
- 预期：按时间序返回 claim/comment/submit/review；comment 带 body、review 带 approve。

#### ACC-E2 以任意身份评论并实时刷新 【手工 P0】
- 步骤：任务详情页底部选角色发评论。
- 预期：时间线实时追加（SSE），带头像/角色/时间戳。

#### ACC-E3 多角色交互流 【手工 P1】
- 步骤：backend-dev 认领→评论→提交；qa 驳回；backend-dev 重提；qa 通过；frontend-dev 评论。
- 预期：任务详情页呈现完整 raft 风格交互流，含交接与评审。

#### ACC-E4 agent 提交后自动 @human 通知评审 【自动化 P0】
- 步骤：派发任务 → agent claim → submit（run 进 reviewing）。
- 预期：任务时间线出现 `kind=mention`、`payload.target=human`、message 含「请评审」的条目，前端高亮为 `@human`。

#### ACC-E5 自然人 @角色 注入上下文 【自动化 P0】
- 步骤：`POST /tasks/{id}/mention` `{target:"developer", message:"注意 limit/offset 边界"}`。
- 预期：时间线出现 `kind=mention`、`target=developer`、`role=human` 的条目；前端以 `@developer` 高亮展示。

#### ACC-E6 角色间 @角色 交互 【自动化 P0】
- 步骤：agent 通过 CLI `anvil task mention <id> --to <role> "消息"`（环境注入 ANVIL_AGENT_SLUG）。
- 预期：时间线出现 `kind=mention`、`target=<role>`、`role=<发起角色>` 的条目；前端以 `@<role>` 高亮。角色职责内事务直接角色间交互，不经过 human。

### F. 角色执行历史与运行追溯

#### ACC-F1 角色执行历史列表 【手工 P0】
- 步骤：大厅点角色卡 → `/agents/{slug}`。
- 预期：列出该角色历次 run，每条含状态徽章、关联任务、起止时间、耗时。

#### ACC-F2 run 详情追溯 【手工 P0】
- 步骤：点某次 run → `/runs/{id}`。
- 预期：展示生命周期时间线、stdout/stderr 日志、配置快照、session/worktree 元信息，SSE 实时刷新。

### G. 治理与成本

#### ACC-G1 全场冻结/恢复 【自动化 P0】
- 步骤：`POST /control/freeze-all` → claim 被拒 → `resume-all` → claim 恢复。
- 预期：冻结态拒新认领（409/423），恢复后放行。

#### ACC-G2 成本闸门 + 并发闸门 【自动化 P1】
- 步骤：注入成本至任务 cost_budget → 新 claim 被拒；角色 max_parallel 达上限 → 新 claim 被拒。
- 预期：软闸门按阈值拦截。

#### ACC-G3 审计日志 【自动化 P1】
- 步骤：若干状态变迁后 `GET /audit`。
- 预期：claim/submit/review/wake/starvation 等动作留痕，可按 task_id 过滤。

### H. 实时事件与前端

#### ACC-H1 SSE 事件流 【自动化 P1】
- 步骤：连 `GET /api/v1/events`，触发 claim/submit/comment。
- 预期：收到 connected + 对应 topic 事件（task/tick），心跳保活。

#### ACC-H2 前端 SSE 驱动刷新 【手工 P1】
- 步骤：开两个页面（大厅 + 某任务会话），在一边操作。
- 预期：另一边角色灯/时间线/活动流实时刷新。

### J. 多角色 / 角色治理 / agent 交互 / 超时回收

#### ACC-J1 任务绑定多角色一起实施 【自动化 P0】
- 步骤：建任务 + `POST /tasks/{id}/assign {roles:[developer,tester]}` → 为每个角色 `POST /runs`。
- 预期：`assignees=['developer','tester']`；两个角色各有一个 run 并行执行；同任务同角色二次派发返回 409。

#### ACC-J2 角色按运行时默认 skill/MCP 勾选加载 【自动化 P0】
- 步骤：`GET /runtimes/{id}/catalog` 拉取该运行时 skill/MCP 目录 → `PUT /roles/{slug}/catalog` 保存勾选 → 派发该角色。
- 预期：spawn 后工作区 `.claude/skills/<name>` 为 symlink、`.claude/settings.json` 含所选 `mcpServers`。

#### ACC-J3 角色 system prompt + 职责配置与注入 【自动化 P1】
- 步骤：`PUT /roles/{slug}/profile` 设 system_prompt + responsibilities → 派发该角色。
- 预期：`CLAUDE.md` managed block 含「系统提示 → 职责 → 指令 → 任务 → 协议」；`GET /roles/{slug}/profile` 读回一致。

#### ACC-J4 LLM 自动生成角色画像 【手工 P0】
- 步骤：角色工坊「指令」tab 输入一句角色描述 → 点「生成」。
- 预期：返回 working_mode / skill_description / responsibilities / system_prompt 并回填可编辑；空描述 422、角色不存在 404。

#### ACC-J5 agent 工作交互（思考评论 + @协作角色）【自动化 P0】
- 步骤：派发角色（demo agent 或真实 claude）。
- 预期：任务时间线含 claim → comment（思考/进展）→ mention（@下一协作角色）→ submit → @human（评审）。

#### ACC-J6 运行超时回收（monitor 标 failed）【自动化 P1】
- 步骤：造一个超过 `RUN_TIMEOUT_SECONDS` 仍 running 的 run（fake clock）→ 调度器 monitor。
- 预期：run 被 terminate + 标 `failed`；不再永久 running。

#### ACC-J7 工作流模板库（SOP 预设拆解）【自动化 P0】
- 步骤：`GET /board/templates` 取模板清单 → `POST /board/decompose {goal, template}` → approve。
- 预期：不同模板产出不同步骤的任务树，每步带角色指派 + 线性依赖；如 `design-dev-test` → 设计(designer)→实现(developer)→测试(tester)，实现依赖设计、测试依赖实现。

#### ACC-J8 依赖感知调度 + 上游完成 @交接下游 【自动化 P0】
- 步骤：拆解 approval 后，调度器 tick；仅派发无依赖的「设计」；人工 review approve「设计」→ 检查「实现」会话出现 `@developer` 交接提及，且依赖满足后调度器自动派发「实现」。
- 预期：下游任务在上游 done 前保持 todo；上游 done 时自动 @提及下游角色；依赖满足后下游自动派发。

#### ACC-J9 @ 即拉入（@角色自动唤醒该角色）【自动化 P0】
- 步骤：任务绑定多角色；用某角色身份 `POST /tasks/{id}/mention {target:<另一角色>}`。
- 预期：被 @ 的角色若为任务参与角色且无活跃 run，自动创建 run 并拉起 agent；@human 不触发唤醒。

#### ACC-J10 多轮挂起/resume（角色反复被唤醒续跑）【自动化 P0】
- 步骤：任务绑定 pm+developer；派发 pm；观察多轮 @。
- 预期：pm 第1轮 @developer → developer 第1轮 @pm → pm resume 第2轮 @developer → developer resume 第2轮 @pm → … → developer 最后一轮 submit；每轮 prompt 注入会话时间线（最新上下文）；@了角色等响应的 run 保持 running（不标 failed/abandoned）。

## 备注

- 「自动化」用例对应 `tests/` 下的 `test_tasks.py`、`test_m2_governance.py`、`test_orchestration.py`、`test_timeline.py`、`tests/graph/*` 等，可用 `pytest tests/ -q` 一键回归。
- L3 CLI 契约（`test_cli_contract.py`）需设 `ANVIL_BIN` 后运行，属独立验收项。
- 手工用例依赖真实运行时（Claude Code 等）或 demo 假 agent（`data/demo-agent.sh`）演示。

### I. 端到端全流程

#### ACC-I1 五子棋场景驱动 4 角色完整闭环 【脚本 P0】
- 脚本：`dev/scripts/e2e_gomoku.py`（角色感知 demo agent 用 `data/demo-agent.sh`）。
- 前置：后端启动（`ANVIL_AUTH_TOKEN=dev-secret`），`anvil` CLI 已装（`pip install -e dev/agentcli`），`data/repos/gomoku` 已 git init。
- 场景：PM 拆「实现一个五子棋小程序」为 设计→开发→测试 三条依赖任务；逐任务派发到 designer/developer/tester 三个角色。
- 步骤：`python3 scripts/e2e_gomoku.py`。
- 预期（脚本自动断言 + 人工关卡）：
  - 每个任务：编排器拉起对应角色 demo agent 子进程 → agent 通过 `anvil` CLI `claim` → 在 worktree 产出真实文件（`docs/design.md`/`gomoku.html`/`test_gomoku.py`）→ `submit` → **暂停等人工评审**（脚本提示 `http://127.0.0.1:7317/tasks/{id}`，自然人在 UI 点「通过/驳回」，脚本轮询直到任务离开 in_review）→ done。
  - 3 个任务全部 done；3 个 run 全部 completed；每条任务时间线含 claim/submit/review；run 日志含 agent 输出。
  - 前端：大厅可见 4 角色卡 + 3 个 done 任务；`/tasks/T-DEV` 看多角色交互时间线；`/agents/developer` 看该角色执行历史；`/runs/{id}` 看生命周期/日志/快照。
