# Anvil — 本地多 Agent 编排服务

Anvil 是一个本地运行的多 Agent 编排服务：纳管运行时（Claude Code 等）、管理角色与私有工作区、
提供带租约的任务板、记忆（FTS5）、reminder 与治理审计。

- **backend/** — FastAPI 后端（状态存 SQLite，文件是真相层）
- **agentcli/** — `anvil` 命令行（Typer），走同一套 REST API（`X-Anvil-Session` 鉴权）
- **frontend/** — React + TypeScript（大厅 + 任务详情 + 频道 + 知识库 + 角色工坊）

## 目录约定

- 源码树：`dev/`（backend / cli / frontend）
- 数据目录（与源码分离，见 SE v1.0）：`ANVIL_DATA_DIR` 默认 `../data`（即 `aliceAgent/data/`），
  内含 `agents/<slug>/`（角色私有 home）、`repos/`（共享仓库）、`runtimes/*.yaml`（运行时定义）、`anvil.db`。

## 快速开始

```bash
# 后端（默认 127.0.0.1:7317；run_server.py 会启动后台调度器并自动开浏览器）
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 run_server.py
# 或只起 API（不启调度器）：uvicorn anvil.main:app --reload --port 7317

# 测试（后端 L2 集成 + LangGraph + L3 CLI 契约骨架）
cd ..
pip install pytest pytest-asyncio
pytest tests/ -q
# 注意：dev/.venv 必须安装 langgraph，否则 conftest 的 importorskip 会静默跳过全部用例。

# CLI（另一终端，需后端已启动）
cd agentcli
pip install -e .
anvil --help

# 前端
cd ../frontend
npm install
npm run dev
```

状态机口径（SE v1.0 修正后）：任务五态 `todo/in_progress/in_review/done/closed`；
`done` 是评审通过后的成功终态；`closed` 表示人工终止，非终态可终止，已终止可重新打开回 `todo`。
`runs` 运行生命周期九态（`created/projecting/running/suspended/submitted/reviewing/completed/failed/abandoned`）是另一概念；`suspended` 表示等待下一条消息，不是失败。完整口径见 `../docs/superpowers/specs/2026-11-10-role-state-machine-design.md`。

## 术语：频道 / 执行工作区 / 角色工作区

v1.30 起明确区分三个容易混淆的概念：

- **频道**（`workspaces` 表、`/channels` 页面、任务的 `workspace_id`）：任务的上一层归属，
  承载频道级知识作用域与资产（默认 repo、knowledge repo、参与角色）。频道**不是聊天室**，
  多 Agent 协作仍然只发生在任务时间线。
- **执行工作区**：任务运行时的 worktree/路径，由任务的 `repo`/`base_branch` 决定；
  子任务继承父任务，不可覆盖。
- **角色工作区**（`agents/<slug>/`）：角色私有 home，含 `MEMORY.md`、`notes/`、工具配置。

知识作用域为 `global / channel / task / role`：频道约定对**绑定该频道**的任务是“约束”，
对只引用其 knowledge repo 的任务是“参考”；任务知识在参与角色间共享；角色知识私有，
必须通过成功实践 + 提炼写入（无证据只能写 draft）。`/knowledge` 页面按六个页签统一浏览
频道知识、任务知识、角色知识、共享经验、演化提案与总体效果评估；单条命中次数显示在
对应知识条目行内，页面浏览不计命中。

设计细节见 `../docs/v1.30-channel-knowledge.md`。

## 问题板（Issue Board）

新建任务时可勾选「启用问题板」。启用后任务详情增加「问题板」页签，集中收集本任务排查出的
安全问题：任务审核者通过 `anvil finding ...` 按固定字段（风险类型/严重等级/模块/函数/
文件行号/前置条件/推理链/风险描述/攻击路径/PoC/修复优先级/修复建议/置信度）填写并提交；
自然人在页面上逐条确认真实问题或误报。问题板完成且所有 finding 已确认后，任务才能最终通过。
Cindy 在规划时从已选角色中指定 reviewer，并保证 workflow 覆盖该角色。

设计细节见 `../docs/issue-board.md`，CLI 用法见 `../docs/cli-reference.md` §5.2。

## 当前实现状态

详见 `implementation-status.md`（各里程碑的已实现 / 未实现清单）。当前自动化测试：
**970 passed + 61 skipped**（CLI 契约需要 `ANVIL_BIN` 才运行；模型评测需要
`ANVIL_COLLAB_EVAL_CMD`；未设置时相关用例跳过）。

切片上下文与派遣子任务默认已启用显式入口（`anvil task dispatch` /
`POST /tasks/{id}/dispatches`）。`anvil task mention` 默认复用/切换当前切片上下文，
可用 `--slice/--context-mode` 覆盖；`anvil inbox check` 默认只拉当前切片，
`--task-wide` 才拉全任务。Agent 之间 mention 自动转为派遣子任务默认关闭；人类 @ 与显式 context_mode 不受此开关限制：
Agent 之间的自动 mention 派遣由 `ANVIL_DISPATCH_AUTO_FROM_MENTION=1` 开启；人类 @ 与显式 `context_mode=dispatch` 不受开关限制；派遣深度上限
`ANVIL_DISPATCH_MAX_DEPTH`（默认 2），回切重试上限
`ANVIL_DISPATCH_RETURN_MAX_ATTEMPTS`（默认 5）。设计见
`../docs/superpowers/specs/2026-11-10-slice-context-dispatch-design.md`。

委派工单（`anvil task create --parent ... --parent-step ...`）由 Anvil 管理为阻塞式
child task：负责人在 `submit`、派发者在 `accept/reject`、human 在 `cancel` 时，都会通过
持久化唤醒队列（`wake_requests`）主动唤醒父 step 或负责人，不等 10 分钟看门狗。看门狗能
识别“父 step 正在等委派”，不会误判停滞；等待有上限（`ANVIL_DELEGATION_WAIT_SECONDS`，
默认 21600 秒），超时后恢复 nudge，要求父 step 上报或 @human 处理。`closed/cancelled`
的委派视为已解除，不再永久阻塞父 step。

slice/step 角色的协作方式由 `COLLABORATION_PROTOCOL` 统一判断：先查 workflow 是否已排定
该产出；阻塞当前 step 时按“自己做 / 同角色只读 subagent / 仅评审 mention / 另一角色独立交付
委派”选择；不阻塞时只在需要独立上下文且不等待的情况下 dispatch；需要改 workflow/角色/顺序
则 @cindy。同角色委派、把 blocking 工作 dispatch 都会被协议禁止。未来步骤的上下文用
`anvil task note --for-step` 写入 task-wide 系统备注（不唤醒），后续 slice/step 启动时带入。
最终 workflow 审批会自动把 workflow 中未绑定的现有角色加入任务参与者，并阻止 human 指定
但未安排的角色；审批前可用 `GET /board/drafts/{id}/participants` 预览，任务详情草案卡会
显示将新增/缺失的角色。human 收件箱新增「委派验收」页签，可直接通过或打回委派工单。
设计见 `../docs/superpowers/specs/2026-11-21-collaboration-routing-design.md`。

运行时原生 subagent 只做观测：`run_subagents` 记录父运行时归属（role/run/task、
proc_generation、native_session_id、parent_tool_use_id、slice/step），turn 结束或进程
退出后统一收尾为 `interrupted`；Anvil 不调度、不唤醒、不把其状态纳入 workflow/lease
决策。只读查询入口：`GET /runs/{id}/subagents`、`GET /roles/{slug}/subagents`、
`GET /tasks/{id}/subagents` 与 `anvil subagent list --run|--role|--task`。设计见
`../docs/superpowers/specs/2026-11-21-runtime-subagent-observability-design.md`。

`subagent_hint` 是规划期提示：角色 runtime 不支持原生 subagent 时，服务端会把所有
载体的 `true` 自动降为 `false` 并写审计（不拒绝草案）；Cindy 的 Roster 会标注每个角色
runtime 是否支持 subagent。设计见
`../docs/superpowers/specs/2026-11-21-subagent-hint-contract-design.md`。

## 最近版本

- `../docs/v1.45.2-release.md` — 收件箱委派验收、审批参与者预览、评测实跑
- `../docs/v1.45.1-release.md` — 角色删除关闭委派、dispatch 通知补投、subagent 幽灵收尾
- `../docs/v1.45.0-release.md` — 协作方式判断、角色一致性、D1–D4 收尾
- `../docs/v1.44.1-release.md` — context_return 死标记修复与看门狗有界等待
- `../docs/v1.44.0-release.md` — 旁路验证节点、按步骤 subagent 提示、Windows 编码加固
- CLI 全量用法见 `../docs/cli-reference.md`
