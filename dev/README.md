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
`runs` 运行生命周期八态（`created/projecting/running/submitted/reviewing/completed/failed/abandoned`）是另一概念。

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
**640 passed + 57 skipped**（CLI 契约需要 `ANVIL_BIN` 才运行；未设置时相关用例跳过）。
