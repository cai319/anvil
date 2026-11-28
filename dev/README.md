# Anvil — 开发者指南

Anvil 是本地运行的多 Agent 编排服务；产品功能、安装（含 Windows）、使用与运维以根 [README](../README.md) 为准。

> 内部规划、设计、ADR、实现状态与问题分析文档仅本地保留，不随仓库发布；v1.51 的
> 澄清、角色门禁、工具治理与大厅性能设计稿在 `docs/superpowers/`，ADR 在 `docs/adr/`，
> 均已加入 `.gitignore`。

## 源码与数据

- `backend/` — FastAPI 后端，SQLite（WAL）+ 文件系统
- `agentcli/` — `anvil` 命令行（Typer），走同一套 REST API
- `frontend/` — React + TypeScript + Vite
- `tests/` — 后端单元/服务、L2 API 集成、LangGraph、CLI 契约与评测套件
- 数据目录与源码分离：`ANVIL_DATA_DIR` 默认 `../data`，含 `agents/<slug>/`、
  `repos/`、`runtimes/*.yaml`、`anvil.db`

## 本地开发

### 后端

```bash
cd dev
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
pip install -e agentcli
cd backend
python run_server.py
```

默认 `127.0.0.1:7317`；`run_server.py` 会启动后台调度器并自动打开浏览器。只起 API：

```bash
uvicorn anvil.main:app --reload --port 7317
```

### 前端

```bash
cd dev/frontend
npm install
npm run dev            # 开发模式
npm run build          # 生产构建（Vite 会自动清空 dist）
npm run clean          # 清理 dist 与 node_modules/.vite
npm run rebuild        # clean + build，发布前推荐
```

版本号来自 `dev/VERSION`，由 Vite 构建时注入；升级版本后先清理再构建。

### 测试

```bash
cd dev
source .venv/bin/activate
pip install pytest pytest-asyncio
pytest tests/ -q
```

当前基线：**1343 passed / 64 skipped**。`dev/.venv` 必须安装 `langgraph`，否则
`conftest` 的 `importorskip` 会静默跳过全部用例；CLI 契约测试需要 `ANVIL_BIN`，
模型评测需要 `ANVIL_COLLAB_EVAL_CMD`。

### CLI

```bash
cd dev
source .venv/bin/activate
pip install -e agentcli
anvil --help

# v1.51：Cindy 澄清 / 角色 RSI / 运行时提案
anvil task clarify $ANVIL_TASK_ID --file brief.json   # 仅更新简报草稿；确认/跳过在 Web
anvil rsi show --role <slug>
anvil runtime proposals
anvil role export <slug> --version <n>
```

角色思考强度通过「角色 → 运行时」或 `PUT /roles/{slug}/runtime` 配置（`effort` 字段），
没有独立 CLI 命令；修改在下次 spawn/resume 生效，运行时需支持 Claude Code `--effort`。

Windows 安装、运行时发现和 `claude.exe` 注意事项见根 README 的「快速开始」；
完整命令见 [docs/cli-reference.md](../docs/cli-reference.md)。

## 关键约定

- 任务五态：`todo/in_progress/in_review/done/closed`；`done` 是评审通过后的成功终态，
  `closed` 是人工终止，可重新打开回 `todo`。
- `runs` 九态：`created/projecting/running/suspended/submitted/reviewing/completed/failed/abandoned`；
  `suspended` 表示等待下一条消息，不是失败。
- 频道（`workspaces`）是任务的上一层归属，不是聊天室；执行工作区是任务运行时的 worktree；
  角色工作区是 `agents/<slug>/` 私有 home。三者不要混用。
- Cindy 澄清五态：`none / in_progress / confirmed / skipped / stale`；`in_progress` 与
  `stale` 时不能创建 workflow / workflow_revision 草案，也不能直接拉起执行 run。
- 角色建议门禁：pending `role_suggestion` 阻塞 workflow 审批；`create_bind` 批准时创建并
  绑定角色，`authorize` 只授权 planner 创建/绑定，完成前审批保持锁定。
- 收件箱 `@human` 提醒按 `(task_id, role_slug)` 各自保留最新一条；只有 `role_slug=human`
  的活动算回应。
- 角色思考强度默认：Cindy `max`，其他角色 `high`；修改不热切换已有常驻进程。
- 完整状态机、dispatch、协作协议、subagent 观测和知识作用域等设计细节见本地设计文档
  （`design.md`、`docs/superpowers/`、`docs/adr/`、`dev/architecture-*` 等，不随仓库发布）。

## 文档入口

- 产品、安装、使用与运维：根 [README](../README.md)
- CLI：[docs/cli-reference.md](../docs/cli-reference.md)
- 问题板：[docs/issue-board.md](../docs/issue-board.md)
- 术语：[GLOSSARY.md](../GLOSSARY.md)
- 变更记录：`docs/v*-release.md`；最新 [v1.51.1](../docs/v1.51.1-release.md)
- 测试说明：[tests/README.md](tests/README.md)
