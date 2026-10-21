# Anvil · 本地多 Agent 编排系统

Anvil 是一个**单机、单用户**的本地多 Agent 协作编排服务。它不重新实现 Agent，也不代理模型调用，而是把你本机已安装的 CLI Agent（Claude Code、Codex CLI、Gemini CLI 等）抽象成带持久工作区与记忆的**角色**，用任务板、工作流和认领机制让多个角色并行干活，并把过程沉淀成可复用的文件。

- **后端**：FastAPI + Uvicorn，asyncio 单进程，SQLite（WAL）+ 文件系统
- **前端**：React 18 + TypeScript + Vite
- **Agent CLI**：`anvil`（Typer），Agent 在自己的工具循环里反向调用它上报状态
- **监听地址**：`http://127.0.0.1:7317`（仅回环，默认不对外）

---

## 目录

1. [项目介绍](#1-项目介绍)
2. [架构与目录](#2-架构与目录)
3. [环境要求](#3-环境要求)
4. [快速开始：macOS](#4-快速开始macos)
5. [快速开始：Windows 10/11](#5-快速开始windows-1011)
6. [前端开发模式](#6-前端开发模式)
7. [第一次使用](#7-第一次使用)
8. [打包为可执行程序](#8-打包为可执行程序)
9. [配置参考](#9-配置参考)
10. [CLI 速查](#10-cli-速查)
11. [测试](#11-测试)
12. [数据备份、迁移与重置](#12-数据备份迁移与重置)
13. [卸载与清理资源](#13-卸载与清理资源)
14. [常见问题](#14-常见问题)
15. [安全说明](#15-安全说明)
16. [相关文档](#16-相关文档)

---

## 1. 项目介绍

### 1.1 它解决什么问题

单独使用 CLI Agent 时，每次会话都是一次性的：上下文用完即弃、角色定位靠每次手打提示词、多个任务只能在同一个终端里排队。当工作量超过一个人一次能盯住的规模时，缺的不是模型能力，而是**编排、持久化与分工**。

Anvil 提供这一层能力：

- 把本机 CLI Agent 注册成**运行时**，再绑定成可复用的**角色**；
- 每个角色拥有私有 home（自我定义、记忆、草稿、日志）和可选的共享 Git 仓库；
- 任务板负责认领、租约、依赖、评审与审计；
- 角色的空闲/唤醒/续跑由编排器管理，经验以文件形式沉淀并反哺角色定义。

### 1.2 核心特性

| 能力 | 说明 |
|---|---|
| 运行时发现 | 内置 Claude Code / Codex CLI / Gemini CLI 定义，按 PATH、npm 全局包、固定路径自动发现；也可用 YAML 扩展 |
| 角色与私有 home | 每个角色独立目录 `data/agents/<slug>/`，含 `instruction.md`、`MEMORY.md`、`notes/`、日志 |
| 常驻角色会话 | 每个角色一个长期存活的 CLI 会话进程；唤醒 = 投递消息（同一上下文）；空闲 15 分钟优雅退出，下次 `--resume` 恢复 |
| 任务板 | 五态任务状态机（`todo / in_progress / in_review / done / closed`）、认领/租约/心跳、多角色独立认领 |
| 计划先行 | 多角色任务先由内置规划助理 **Cindy** 分析并产出工作流草案，人类在收件箱批准后按依赖执行 |
| 工作流引擎 | 步骤状态机（DAG、可并行层、审核门 gate、验证者 ≠ 执行者），同一角色串行、跨角色并行 |
| 记忆与知识 | `MEMORY.md` + FTS5 检索 + 共享 lessons（80 条上限）+ 提案审批 |
| 工具治理 | skill / MCP 投影、工具白名单与 token 预算、系统/运行时/角色/日成本四层闸门 |
| 可观测性 | SSE 实时事件、审计、运行日志、Token 消耗统计、全场冻结 |
| 人机边界 | 评审、审批、删除、重置等人工确认点只接受自然人身份，Agent 令牌调用返回 403 |

### 1.3 不做什么

- 不代理模型调用，不存储模型凭据（凭据归各 CLI Agent 自己管理）；
- 不实现 ReAct 循环、工具注册、文件编辑——这些由运行时自己完成；
- 不做账号体系、多人协作、手机端；
- v1 不做向量检索（只用 SQLite FTS5），不做容器级强隔离。

### 1.4 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + Uvicorn + asyncio |
| 存储 | SQLite（WAL）+ 文件系统 |
| 编排 | LangGraph + SQLite checkpointer |
| 前端 | Vite + React 18 + TypeScript |
| CLI | Typer + httpx |
| 打包 | PyInstaller（单目录分发） |

---

## 2. 架构与目录

### 2.1 运行架构

```
┌──────────────────────────────────────────────┐
│  Web UI (React)        ← SSE ← REST           │
└───────────────────────┬──────────────────────┘
                        │ 127.0.0.1:7317
┌───────────────────────▼──────────────────────┐
│  FastAPI 应用层                               │
│  运行时注册 / 角色投影 / 任务板 / 调度器       │
│  工作流引擎 / 记忆 / 工具治理 / 审计           │
└──────┬─────────────────────────┬─────────────┘
       │                         │
┌──────▼──────┐          ┌───────▼────────────┐
│ SQLite(WAL) │          │ 文件系统            │
│ 状态/索引   │          │ data/agents/<slug>/ │
│             │          │ data/repos/         │
└─────────────┘          │ data/worktrees/     │
       │                 └────────────────────┘
       │ spawn / stdin
┌──────▼───────────────────────────────────────┐
│ Agent 子进程（claude / codex / gemini ...）    │
│   ↑ 反向调用 anvil CLI → HTTP → 编排器         │
└──────────────────────────────────────────────┘
```

**状态在数据库，内容与真相在文件系统。** 当两者冲突时以文件为准。

### 2.2 仓库结构

```
aliceAgent/
├── design.md                    # 设计文档 v3.0（架构与协议的权威来源）
├── optimization-roadmap.md      # 优化路线图
├── README.md                    # 本文件
├── data/                        # 运行时数据（默认数据目录，见 2.3）
├── recovered/                   # 历史任务产物的源码恢复样例（非 Anvil 本体）
└── dev/                         # 源码树
    ├── backend/                 # FastAPI 后端
    │   ├── anvil/
    │   │   ├── api/             # REST 端点
    │   │   ├── services/        # 角色/任务/运行/工作流/记忆/治理
    │   │   ├── graph/           # LangGraph 编排图与提示词渲染
    │   │   ├── platform/        # Windows / macOS 进程与凭据抽象
    │   │   ├── runtimes/        # 内置运行时 YAML
    │   │   ├── config.py        # 环境变量与路径
    │   │   ├── db.py            # SQLite schema
    │   │   └── server.py        # 生产入口（uvicorn + 自动开浏览器）
    │   ├── run_server.py        # `python run_server.py` 入口
    │   └── requirements.txt
    ├── agentcli/                # `anvil` 命令行
    ├── frontend/                # React + Vite 前端
    ├── packaging/               # PyInstaller spec 与构建脚本
    ├── tests/                   # 后端 L2/L3 测试
    └── implementation-status.md # 实现状态与缺口清单（随代码更新）
```

### 2.3 数据目录

默认数据目录与源码树分离：

| 运行方式 | 默认数据目录 |
|---|---|
| 源码开发（`dev/backend/run_server.py`） | `<项目根>/data/` |
| PyInstaller 打包产物 | `<anvil-dist 的父目录>/data/` |

目录内容：

```
data/
├── agents/<slug>/          # 角色私有 home（instruction.md、MEMORY.md、notes/、logs/）
├── repos/<name>/           # 共享 Git 仓库（可选）
├── worktrees/<run_id>/     # 每次运行的 Git worktree（任务配置了 repo 时）
├── runtimes/               # 当前不参与运行时加载；用户运行时覆盖见第 9 节
├── anvil.db                # 任务/角色/运行/会话/审计等状态与 FTS 索引
├── anvil-graph.db          # LangGraph 检查点
└── backups/                # 角色 home 备份（full-reset 时生成）
```

> 运行时 YAML 的真正覆盖目录不是 `data/runtimes/`，而是用户的配置目录：
> Windows `%APPDATA%\Anvil\runtimes\`，macOS `~/.config/Anvil/runtimes/`。
> 可用 `ANVIL_USER_RUNTIMES_DIR` 覆盖，详见第 9 节。

### 2.4 一次任务的生命周期（简述）

1. 人类在大厅创建任务，选择参与角色并派发；
2. **单角色任务**：直接唤醒该角色；
   **多角色任务**：先唤醒规划助理 Cindy，分析任务并提交工作流草案；
3. 人类在收件箱批准草案，工作流步骤按依赖关系派发；
4. 角色在私有 home（或 Git worktree）中执行，通过 `anvil` CLI 上报进展、@ 其他角色；
5. 步骤完成后 `anvil step submit`；审核门（gate）进入收件箱等待人工通过；
6. 全部步骤完成 → 任务进入 `in_review` → 人类最终评审 → `done`。

---

## 3. 环境要求

### 3.1 必需

| 组件 | 版本 / 说明 |
|---|---|
| 操作系统 | **Windows 10/11**（设计首要平台）或 **macOS**。后端进程管理层目前只实现 Windows 与 macOS，Linux 尚未支持 |
| Python | **3.11+**（`backend/pyproject.toml` 要求） |
| Node.js / npm | **Node 18+**（推荐 20/22 LTS）。仅用于构建前端；若已有 `frontend/dist` 可跳过 |
| Git | 2.30+，需在 PATH 中（Anvil 通过 `git` 子进程管理工作区） |
| CLI Agent 运行时 | 至少安装一个自带工具循环的 CLI Agent，推荐 **Claude Code**（默认运行时） |
| 项目磁盘空间 | 建议 ≥ 2 GB（依赖 + 前端构建 + 角色工作区） |

### 3.2 平台补充

- **Windows**
  - 必须额外安装 [`pywin32`](https://pypi.org/project/pywin32/)（`pip install pywin32`）：后端在 Windows 上使用 Job Object 管理 Agent 进程树，缺少它会在启动调度器时报 `No module named 'win32job'`。
- **macOS**
  - 建议使用 Homebrew 或 python.org 的 Python 3.11+；系统自带的旧 Python 可能不满足版本要求。
  - 首次运行 `npm install` 需要 Node/npm 可用。

### 3.3 模型与凭据

Anvil 不保存模型凭据，所有模型调用都由运行时自己完成：

- **Claude Code** 使用它自己的登录态或环境变量（`ANTHROPIC_*`）。请先在终端确认 `claude --version` 与一次最小调用可用。
- **Codex CLI / Gemini CLI** 同理，先确认 `codex --version` / `gemini --version` 可用。
- 如果使用环境变量认证，请在**启动后端的同一个 shell** 中导出变量——Anvil 会把环境继承给 Agent 子进程（仅过滤 `CLAUDE_CODE_*` 等编排器会话变量）。
- 角色工坊的「AI 生成并创建」和角色详情里的「生成画像」也**通过 CLI 运行时执行**，不再由后端直连模型 API，因此复用运行时自己的登录态，不需要额外配置 `ANTHROPIC_*`：
  - 默认使用「运行时」页通过 **设为默认** 选定的运行时；
  - 角色详情「生成画像」优先使用该角色绑定的运行时；
  - 没有可用运行时会返回明确错误，请先在「运行时」页扫描并确认至少一个 `ready`。

> 运行时自身的模型/网关配置（例如 Claude Code 的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`）仍由运行时自己读取；Anvil 不保存、不转发这些凭据。

---

## 4. 快速开始（macOS）

以下命令在项目根目录（下文记为 `<项目根>`）执行。

### 4.1 安装后端依赖

```bash
cd <项目根>/dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r backend/requirements.txt
```

### 4.2 安装 `anvil` CLI（重要）

```bash
# 仍在同一个 venv 中
pip install -e agentcli
anvil --help
```

> **为什么必须装进同一个 venv？**
> Agent 子进程需要能直接执行 `anvil` 命令。后端启动时会继承当前 shell 的 `PATH`；在已激活的 venv 中启动后端，`.venv/bin` 就在 PATH 里，Agent 才能找到 `anvil`。

### 4.3 构建前端（首次或前端改动后）

```bash
cd frontend
npm install
npm run build
cd ..
```

构建产物在 `dev/frontend/dist/`，后端会自动把它作为静态站点服务。若跳过这一步，`http://127.0.0.1:7317` 将没有页面（API 仍然可用）。

### 4.4 启动后端

```bash
cd backend
python run_server.py
```

- 默认监听 `127.0.0.1:7317`；
- 约 1.5 秒后自动打开浏览器；
- 启动日志中看到 `Application startup complete` 即成功；
- **不要**用 `uvicorn anvil.main:app --reload` 作为日常运行方式：该入口不启动后台调度器（仅用于调试 API）。

### 4.5 首启初始化（另开一个终端）

```bash
cd <项目根>/dev
source .venv/bin/activate

# 扫描运行时 → 创建默认频道 main → 导入 developer / tester / reviewer 三个角色
anvil init --with-roles
```

预期输出类似：

```
发现 3 个运行时，其中 1 个 ready：
  - Claude Code (claude-code) → ready
  - Codex CLI (codex-cli) → missing
  - Gemini CLI (gemini-cli) → missing
已创建默认频道：main
  已导入角色：开发工程师 (developer)
  已导入角色：测试工程师 (tester)
  已导入角色：评审员 (reviewer)
```

- `anvil init` 需要后端已启动；
- 若没有 `ready` 的运行时，先安装 CLI Agent 并确认它在 PATH 中，再执行一次 `anvil init`；
- 打开「运行时」页，确认目标运行时为 `ready`，并点击「默认」列的 **设为默认**；
  AI 生成画像/角色会使用这个默认运行时；
- Cindy（规划助理）不需要手动创建，首次派发多角色任务时自动创建。

### 4.6 打开界面

浏览器访问 **http://127.0.0.1:7317**。导航包含：收件箱、任务大厅、角色工坊、运行时、目标拆解、频道、知识库、提醒、工具、治理。

### 4.7 停止

在后端终端按 `Ctrl+C`。Anvil 会优雅停机：终止活跃 Agent 子进程，把未完成运行标记为 `suspended`（下次可续跑），并保留角色 `session_id`。

---

## 5. 快速开始（Windows 10/11）

以下以 **PowerShell** 为例；CMD 的差异见本节末尾。

### 5.1 安装后端依赖

```powershell
cd C:\path\to\aliceAgent\dev

# 如果 py 启动器不可用，改用 python -m venv .venv
py -3.11 -m venv .venv

# 若提示“无法加载文件 ...Activate.ps1，因为在此系统上禁止运行脚本”：
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r backend\requirements.txt

# Windows 必需：Job Object 进程管理
pip install pywin32
```

### 5.2 安装 `anvil` CLI

```powershell
# 仍在同一个 venv 中
pip install -e agentcli
anvil --help
```

确认 `.\.venv\Scripts\anvil.exe` 存在，并且当前 shell 中 `anvil` 可执行。

### 5.3 构建前端

```powershell
cd frontend
npm install
npm run build
cd ..
```

若 `npm` 不可用，请先安装 Node.js 18+ 并重新打开终端。

### 5.4 启动后端

```powershell
cd backend
python run_server.py
```

- 默认监听 `127.0.0.1:7317`，约 1.5 秒后自动打开默认浏览器；
- 仅绑定回环地址，不会触发 Windows 防火墙入站规则；
- 首次启动会在 `<项目根>\data\` 下创建数据库与目录。

### 5.5 首启初始化（另开一个 PowerShell）

```powershell
cd C:\path\to\aliceAgent\dev
.\.venv\Scripts\Activate.ps1

anvil init --with-roles
```

### 5.6 确认运行时

```powershell
claude --version
```

若 `claude` 不在 PATH：

- Claude Code 原生安装通常在 `%USERPROFILE%\.local\bin`；
- npm 全局安装通常在 `%APPDATA%\npm`；
- 把对应目录加入用户 PATH，重新打开终端后再执行 `anvil init`。

然后在界面「运行时」页点击 **扫描本地运行时**，或重新执行 `anvil init`；
确认目标运行时为 `ready` 后，在「默认」列点击 **设为默认**，AI 生成画像/角色会使用它。

### 5.7 CMD 用户

```bat
cd /d C:\path\to\aliceAgent\dev
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -r backend\requirements.txt
pip install pywin32
pip install -e agentcli

cd frontend
npm install
npm run build
cd ..

cd backend
python run_server.py
```

---

## 6. 前端开发模式

需要改前端时，不必每次 `npm run build`，直接使用 Vite 开发服务器：

```bash
# 终端 1：后端
cd <项目根>/dev
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
cd backend
python run_server.py

# 终端 2：前端
cd <项目根>/dev/frontend
npm install
npm run dev
```

- Vite 默认端口 `5173`，并把 `/api` 代理到 `http://127.0.0.1:7317`；
- 打开 `http://localhost:5173`；
- 生产使用时仍建议 `npm run build`，由后端在 7317 端口统一服务（同源，无代理）。

> 注意：`frontend/vite.config.ts` 中的代理地址硬编码为 7317。如果改了后端端口，需要同步修改该文件。

---

## 7. 第一次使用

### 7.1 确认运行时

打开「运行时」页：

1. 点击 **扫描本地运行时**；
2. 状态应为 `ready`（绿色）；`missing` 表示未安装或不在 PATH；
3. 在页面顶部的 **默认运行时** 下拉框选择，或在「默认」列点击 **设为默认**；
   它用于没有角色参与的 AI 工具调用。不设置时使用 `ANVIL_DEFAULT_RUNTIME`
   （默认 `claude-code`），不可用时退到第一个 `ready` 运行时；
4. 可点击 **健康检查** 做一次最小调用（会消耗少量模型额度）。

内置运行时定义位于 `dev/backend/anvil/runtimes/`：

| ID | 显示名 | 常驻会话 | 说明 |
|---|---|---|---|
| `claude-code` | Claude Code | ✅ | 常用默认运行时；支持 stream-json 常驻会话与 `--resume` |
| `codex-cli` | Codex CLI | ❌ | 回退 one-shot 模式 |
| `gemini-cli` | Gemini CLI | ❌ | 回退 one-shot 模式 |

#### 自定义运行时（如 `codeagent`）

如果本机有一个 Claude Code 风格的自定义 CLI（例如 `codeagent`），在「运行时」页点击
**新增自定义运行时**，直接编辑 YAML 即可：

1. 修改 `id`、`display_name`；
2. 把 `discovery.strategies[0].command` 改成你的命令名（或在 YAML 中写固定路径）；
3. 按你的 CLI 调整 `invocation.args`（**必须包含 `{{ prompt }}`**；`{{ permission_mode }}`
   会由 Anvil 渲染）；
4. 如果该 CLI 读取的指令文件不是 `CLAUDE.md`，修改 `projection.instruction.target`；
5. 点击 **保存并探测**；状态变为 `ready` 后即可使用。点击 **设为默认**，AI 生成画像/角色
   就会使用它。

默认模板（Claude 风格，界面中可一键重置）：

```yaml
id: codeagent
display_name: codeagent
version_range: ">=0.1.0"

discovery:
  strategies:
    - type: path_lookup
      command: codeagent
  version_probe:
    args: ["--version"]
    parse: 'regex:(\d+\.\d+\.\d+)'

invocation:
  args:
    - "--print"
    - "--permission-mode"
    - "{{ permission_mode }}"
    - "{{ prompt }}"
  cwd: "{{ workspace_path }}"
  timeout_seconds: 3600
  exit_code_success: [0]

projection:
  instruction:
    target: "{{ workspace_path }}/CLAUDE.md"
    format: markdown
    strategy: overwrite_managed_block
```

保存后 YAML 会写入用户运行时目录：

- Windows：`%APPDATA%\Anvil\runtimes\<id>.yaml`
- macOS：`~/.config/Anvil/runtimes/<id>.yaml`

编辑内置运行时同样会生成/覆盖该用户文件，内置 YAML 本身不会被修改。也可以手动把 YAML
放到上述目录，然后点击「扫描本地运行时」。

### 7.2 准备角色

两种方式：

- **模板角色**：`anvil init --with-roles` 已导入 `developer` / `tester` / `reviewer`；
- **界面创建**：角色工坊 → 新建角色，填写 slug 与显示名，并在工具栏的运行时下拉框中选择
  该角色要绑定的运行时（可绑定任意已注册运行时）。创建后可在角色详情页继续编辑系统提示、
  工具等配置。也可以用「AI 生成并创建」根据一句话描述生成画像：它使用上方选择的运行时；
  角色详情里的「生成画像」优先使用该角色自己的运行时。详见 [3.3 节](#33-模型与凭据)。

创建后可在角色详情 →「运行时」tab 更改绑定。该操作只替换 `runtime`，不会影响
`system_prompt`、职责、工具等其它配置。

角色的技能/MCP **默认全部不勾选**。在角色详情 →「技能/MCP」tab 中按该角色独立勾选并保存：
可用项来自该角色绑定的运行时，未勾选的项目不会由 Anvil 投影到该角色的 workspace；
「全部不选并保存」可一键清空。切换角色时不会沿用上一个角色的选择，各角色的选择互不影响。

> **默认运行时的用途**：没有角色参与的 AI 工具调用（例如「AI 生成并创建」、角色未绑定
> 运行时时的「生成画像」）统一使用「运行时」页选定的默认运行时。角色执行任务时始终使用
> 角色自己绑定的运行时。

每个角色会自动获得：

- 私有 home：`data/agents/<slug>/`
- `instruction.md`（角色指令）、`MEMORY.md`（长期记忆）、`notes/`
- 每周复盘 reminder（幂等，周一 09:00）

### 7.3 创建并派发任务

1. 任务大厅 → **＋ 新建任务**；
2. 填写名称与描述，选择参与成员（可多选）；
3. 创建后进入任务详情，点击 **派发**：
   - **单角色**：直接唤醒该角色执行；
   - **多角色**：先由 Cindy 产出工作流草案，草案出现在 **收件箱**；
4. 在收件箱批准草案后，步骤按依赖关系自动派发。

### 7.4 收件箱与人工确认

收件箱聚合以下待办：

- `in_review` 任务（最终评审）
- `@human` 提及
- 待审工作流草案
- 工作流步骤审核门（gate）
- 待审 lesson / 提案
- 操作卡片（Action Cards，Agent 申请、人类批准后执行）

所有审批都必须由**自然人**执行。Agent 令牌调用审批端点会被拒绝（HTTP 403）；这是刻意的 HITL 边界，不要用脚本代替人工点击。

### 7.5 角色在哪里写文件？

| 场景 | 工作目录（cwd） |
|---|---|
| 任务没有配置 `repo` | 角色私有 home：`data/agents/<slug>/` |
| 任务配置了 `repo`，且 `data/repos/<repo>/` 是 Git 仓库 | 独立 worktree：`data/worktrees/<run_id>/`，分支 `agent/<slug>/<task>` |

要在共享仓库上协作：

```bash
# 把一个 Git 仓库放到数据目录（示例）
cd <项目根>/data/repos
git clone <仓库地址> myrepo
```

然后通过 API 在**派发前**给任务设置 `repo`（当前 Web 新建任务表单未暴露该字段；派发后修改不会影响已创建的运行）：

```bash
curl -X PATCH http://127.0.0.1:7317/api/v1/tasks/<task-id> \
  -H 'X-Anvil-Session: anvil' \
  -H 'Content-Type: application/json' \
  -d '{"repo":"myrepo"}'
```

运行日志位于 `data/agents/<slug>/logs/run-<run_id>.log`。

---

## 8. 打包为可执行程序

打包使用 PyInstaller **单目录**（onedir）模式，产物包含 `anvil` 与 `anvil-server` 两个可执行文件，前端静态资源与内置运行时 YAML 一并打入。

### 8.1 macOS（以及脚本标称的 Linux）

```bash
cd <项目根>/dev
bash packaging/build.sh
```

脚本会：

1. 在 `dev/.venv` 创建虚拟环境；
2. 安装 `backend/requirements.txt` 与 PyInstaller；
3. 构建前端；
4. 执行 `pyinstaller packaging/anvil.spec`。

产物：`dev/dist/anvil-dist/`（`anvil`、`anvil-server`、`_internal/`）。

> 说明：`build.sh` 注释虽写 macOS/Linux，但后端进程管理层目前只实现 Windows/macOS；Linux 运行时会报 `Unsupported platform`。

### 8.2 Windows

```bat
cd /d <项目根>\dev
packaging\build.bat
```

`build.bat` 的等价手动步骤（推荐先装 `pywin32`，脚本本身未包含）：

```powershell
cd <项目根>\dev
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt pywin32 pyinstaller

cd frontend
npm install
npm run build
cd ..

pyinstaller --clean --noconfirm packaging\anvil.spec
```

产物：`dev\dist\anvil-dist\`（`anvil.exe`、`anvil-server.exe`、`_internal\`）。

### 8.3 运行打包产物

```bash
# macOS
cd <项目根>/dev/dist/anvil-dist
./anvil-server        # 自动打开浏览器 http://127.0.0.1:7317
```

```powershell
# Windows
cd C:\path\to\aliceAgent\dev\dist\anvil-dist
.\anvil-server.exe
```

重要：

- 打包版的数据目录是 **`anvil-dist` 的父目录下的 `data/`**（例如 `dev/dist/data/`）。需要迁移时整体移动 `anvil-dist/` 与 `data/`；
- Agent 运行时会反向调用 `anvil`，所以要把 `anvil-dist` 目录加入 PATH（或在 Agent 的 shell 中可用），否则角色无法认领/提交任务；
- 打包后的前端已内嵌，无需 Node.js。

---

## 9. 配置参考

所有配置都通过环境变量读取（`dev/backend/anvil/config.py`）。

### 9.1 常用变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANVIL_HOST` | `127.0.0.1` | 监听地址 |
| `ANVIL_PORT` | `7317` | 监听端口 |
| `ANVIL_DATA_DIR` | 开发：`<项目根>/data`；打包：`<anvil-dist 父目录>/data` | 数据根目录 |
| `ANVIL_SQLITE_PATH` | `$ANVIL_DATA_DIR/anvil.db` | 状态数据库 |
| `ANVIL_AGENTS_DIR` | `$ANVIL_DATA_DIR/agents` | 角色 home 根目录 |
| `ANVIL_REPOS_DIR` | `$ANVIL_DATA_DIR/repos` | 共享仓库根目录 |
| `ANVIL_GRAPH_DB_PATH` | `$ANVIL_DATA_DIR/anvil-graph.db` | LangGraph 检查点 |
| `ANVIL_AUTH_TOKEN` | 空 | 设置后，`X-Anvil-Session` 必须等于该值；为空时只校验请求头存在 |
| `ANVIL_USER_RUNTIMES_DIR` | Windows `%APPDATA%\Anvil\runtimes`；macOS `~/.config/Anvil/runtimes` | 用户运行时 YAML 覆盖目录 |
| `ANVIL_DEFAULT_RUNTIME` | `claude-code` | 默认运行时 ID |
| `ANVIL_DEFAULT_PERMISSION_MODE` | `bypassPermissions` | 角色未显式配置时的权限模式 |
| `ANVIL_PERSISTENT_RUNTIME` | `auto` | `auto`：生产入口启用常驻会话；`0` 强制回退 one-shot |
| `ANVIL_IDLE_RELEASE_MINUTES` | `15` | 角色空闲释放时间（分钟） |
| `ANVIL_SCHEDULER_INTERVAL` | `5.0` | 调度器扫描间隔（秒） |
| `ANVIL_RUN_TIMEOUT` | `600` | 运行超时兜底（秒）；运行时 YAML 的 `timeout_seconds` 优先 |
| `ANVIL_UTILITY_TIMEOUT` | `120` | AI 生成画像/角色（经 CLI 运行时）的超时（秒） |
| `ANVIL_STEP_NUDGE_SECONDS` | `600` | 工作流步骤停滞唤醒阈值 |
| `ANVIL_STEP_MAX_NUDGES` | `3` | 步骤最大唤醒次数 |
| `ANVIL_ONBOARDING` | `1` | 多角色任务是否启用 Cindy 规划 |
| `ANVIL_AUTO_DISPATCH` | `0` | 是否自动派发 todo 任务 |
| `ANVIL_CLEANER` | `1` | 是否启用 `MEMORY.md` 体积自修剪提醒 |
| `ANVIL_CLEANER_MAX_BYTES` | `12000` | 记忆文件提醒阈值 |
| `ANVIL_CIC_THRESHOLD_TOKENS` | `128000` | 冷唤醒构造上下文的阈值 |
| `ANVIL_MAX_ROLES` | `20` | 角色总数上限 |
| `ANVIL_AGENT_ROLE_EXPAND_DAILY` | `3` | Agent 每日自行扩展角色配额 |
| `ANVIL_TEST_AUTH` | 空 | 仅测试用：`1` 时绕过会话校验，生产不要设置 |

### 9.2 设置方式

以下示例均在 `dev/backend` 目录执行（先激活 venv）。

macOS：

```bash
export ANVIL_DATA_DIR="$HOME/AnvilData"
export ANVIL_PORT=7317
python run_server.py
```

Windows PowerShell：

```powershell
$env:ANVIL_DATA_DIR = "D:\AnvilData"
$env:ANVIL_PORT = "7317"
python run_server.py
```

Windows CMD：

```bat
set ANVIL_DATA_DIR=D:\AnvilData
set ANVIL_PORT=7317
python run_server.py
```

### 9.3 鉴权

- 默认 `ANVIL_AUTH_TOKEN` 为空：所有写操作仍必须带 `X-Anvil-Session`，但值不校验。Web 端默认发送 `anvil-web`，CLI 默认发送 `anvil`。
- 设置了 `ANVIL_AUTH_TOKEN` 后：
  - CLI：`export ANVIL_TOKEN=<token>`（Windows：`$env:ANVIL_TOKEN="<token>"`）；
  - Web：构建时设置 `VITE_ANVIL_TOKEN`，或在浏览器控制台执行
    `localStorage.setItem("anvil-session", "<token>")` 后刷新。
- CLI 还会读取 `ANVIL_ENDPOINT`（默认 `http://127.0.0.1:7317`）、`ANVIL_AGENT_SLUG`、`ANVIL_TASK_ID`。

### 9.4 修改端口时的额外步骤

端口并非只改 `ANVIL_PORT` 就够——三个运行时 YAML 的 `invocation.env.ANVIL_ENDPOINT` 与 CLI 默认值都硬编码了 7317：

1. 后端：`ANVIL_PORT=7318 python run_server.py`
   （Windows PowerShell 先执行 `$env:ANVIL_PORT="7318"`，再 `python run_server.py`）；
2. CLI：`export ANVIL_ENDPOINT=http://127.0.0.1:7318`；
3. 运行时 YAML：把内置定义**完整复制**到用户运行时目录，修改其中的 `ANVIL_ENDPOINT`
   （用户定义是整体覆盖，不做字段合并；否则 Agent 子进程仍会调用 7317）；
4. 前端开发模式：同步修改 `frontend/vite.config.ts` 的代理目标。

除非确有需要，建议保持 7317。

---

## 10. CLI 速查

`anvil` 是给 Agent 与运维人员共用的命令行，所有命令走同一套 REST API。

### 10.1 人类/运维常用

```bash
anvil init --with-roles                 # 首启：发现运行时、建频道、导入模板角色
anvil task list                         # 任务列表
anvil task show <task-id>               # 任务详情
anvil task create --title "..." --description "..."
anvil task comment <task-id> "说明"      # 以当前身份评论
anvil task mention <task-id> --to human "请确认"   # @ 提及
anvil whoami                            # 当前会话身份
anvil ws status <workspace-id>          # 工作区文件与 git 状态
anvil ws diff <workspace-id>            # 工作区 diff
```

### 10.2 Agent 工作流常用

```bash
anvil task claim $ANVIL_TASK_ID                     # 认领
anvil task heartbeat                                # 续租
anvil inbox check $ANVIL_TASK_ID                    # 拉取未读消息
anvil inbox commit $ANVIL_TASK_ID --up-to <msg-id>  # 提交 cursor
anvil task comment $ANVIL_TASK_ID "进展"
anvil task mention $ANVIL_TASK_ID --to <role> "请确认接口"
anvil task submit $ANVIL_TASK_ID "完成摘要"          # 整任务提交
anvil step submit <workflow-id> <step-id> --summary "步骤完成"  # 工作流步骤提交
anvil board draft --task $ANVIL_TASK_ID --goal "..." --tree-file wf.json --kind workflow
anvil role expand --brief "需要一个安全评审角色"       # Agent 扩展团队（有配额）
anvil action prepare --kind task.cancel --task $ANVIL_TASK_ID --reason "..."  # 危险操作申请
```

### 10.3 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 通用失败 |
| 2 | 参数/用法错误 |
| 3 | 状态冲突（如任务已被认领） |
| 4 | 无权限 |

---

## 11. 测试

### 11.1 后端测试

在 `dev/` 下执行，使用与后端一致的虚拟环境：

```bash
cd <项目根>/dev
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install pytest pytest-asyncio  # requirements.txt 不含测试依赖
pytest tests/ -q
```

> **重要**：`tests/conftest.py` 对 `anvil.main` 使用 `importorskip`。如果 venv 中没有安装
> `langgraph`（或没有安装 `backend/requirements.txt`），测试会被**静默跳过**而不是报错。
> 看到大量 `skipped` 时先检查依赖。

测试分为：

- `tests/api/`：FastAPI 集成测试（L2）；
- `tests/graph/`：LangGraph 编排、检查点、渲染；
- `tests/cli/`：CLI 契约测试（需要 `ANVIL_BIN` 时才运行）。

### 11.2 前端构建校验

```bash
cd <项目根>/dev/frontend
npm run build      # tsc + vite build
```

### 11.3 当前状态

实现进度、已实现/未实现清单与最新测试数量以
[`dev/implementation-status.md`](dev/implementation-status.md) 为准（文档会随代码更新）。

---

## 12. 数据备份、迁移与重置

### 12.1 备份

1. 停止后端（`Ctrl+C`），确保没有 Agent 子进程；
2. 复制整个数据目录（默认 `<项目根>/data/` 或打包版的 `dist/data/`）。

需要保留的核心内容：

- `agents/`：角色身份、记忆、日志、产出；
- `repos/`：共享 Git 仓库；
- `anvil.db` / `anvil-graph.db`：状态与检查点；
- `backups/`：历史重置产生的备份。

### 12.2 迁移到另一台机器

1. 复制源码（或打包产物）与数据目录；
2. 在新机器安装 Python/Node/CLI Agent；
3. 用 `ANVIL_DATA_DIR` 指向复制过来的数据目录（或放到默认位置）；
4. 在新机器上重新执行运行时发现（界面「扫描本地运行时」，或再次运行 `anvil init`），因为可执行文件路径会变化。

### 12.3 重置

- **不要直接删除 `anvil.db` 而保留 `agents/`**：状态与文件会不一致；
- 想要“干净重来”时，停止后端后整体移走/删除数据目录，再重新 `anvil init`；
- 角色级重置可在界面 Run 详情使用 `session-reset`（保留身份与记忆）或 `full-reset`（自动备份 home 后重置）。

---

## 13. 卸载与清理资源

> **警告**：删除数据目录不可恢复。执行前请先按第 12 节备份需要保留的角色 home、共享仓库
> 与数据库。Anvil 不会自动删除你机器上的 Claude Code / Codex / Gemini 等运行时。

### 13.1 停止服务与 Agent 进程

macOS：

```bash
# 找到监听 7317 的进程
lsof -nP -iTCP:7317 -sTCP:LISTEN

kill <PID>          # 优先优雅停止
# 若 15 秒后仍未退出
kill -9 <PID>
```

Windows PowerShell：

```powershell
netstat -ano | findstr :7317
taskkill /PID <PID> /F
```

- 优雅停止会终止角色常驻进程，并把活跃 run 转成 `suspended`；
- 如果强制结束，建议检查是否有残留的角色 CLI 进程：
  - macOS：`ps aux | grep -i claude`，确认没有 cwd 在 `<项目根>/data/agents` 下的进程；
  - Windows：在任务管理器中检查 `claude.exe` / `codex.exe` / `gemini.exe` 等，确认没有
    Anvil 角色进程。

### 13.2 删除开发环境（源码模式）

```bash
cd <项目根>/dev

# 1) 卸载 anvil CLI（editable 安装）
.venv/bin/pip uninstall -y anvil-cli

# 2) 删除虚拟环境、前端依赖与构建产物
rm -rf .venv frontend/node_modules frontend/dist

# 3) 清理 Python 缓存
find . -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf .pytest_cache
```

Windows PowerShell：

```powershell
cd C:\path\to\aliceAgent\dev

.\.venv\Scripts\pip.exe uninstall -y anvil-cli

Remove-Item -Recurse -Force .venv, frontend\node_modules, frontend\dist -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Remove-Item -Recurse -Force .pytest_cache -ErrorAction SilentlyContinue
```

> 如果曾在其它 Python 环境执行过 `pip install -e agentcli`（例如系统 Python），请在该环境
> 也执行一次 `pip uninstall -y anvil-cli`。
> `pip uninstall` 只删除命令注册，源码目录仍需手动删除。

### 13.3 删除运行时数据

默认数据目录：

| 运行方式 | 位置 |
|---|---|
| 源码模式 | `<项目根>/data/` |
| 打包模式 | `<anvil-dist 父目录>/data/`（例如 `dev/dist/data`） |

如果设置过 `ANVIL_DATA_DIR`，请删除该变量指向的目录。

macOS：

```bash
rm -rf <项目根>/data
```

Windows PowerShell：

```powershell
Remove-Item -Recurse -Force C:\path\to\aliceAgent\data
```

数据目录包含：

- `agents/`：角色 home、记忆、日志、产出；
- `repos/`：共享 Git 仓库；
- `worktrees/`：运行 worktree；
- `anvil.db` / `anvil-graph.db`：状态与检查点；
- `backups/`：历史重置产生的备份。

如果只想清理运行状态、保留代码产出：保留 `data/repos/` 和需要的 `data/agents/<slug>/`，
删除数据库、`worktrees/` 等其它内容，再重新执行 `anvil init`。

### 13.4 删除用户运行时定义

自定义/覆盖的运行时 YAML：

- Windows：`%APPDATA%\Anvil\runtimes`
- macOS：`~/.config/Anvil/runtimes`

```bash
rm -rf ~/.config/Anvil/runtimes          # macOS
```

```powershell
Remove-Item -Recurse -Force "$env:APPDATA\Anvil\runtimes"
```

如果设置过 `ANVIL_USER_RUNTIMES_DIR`，删除该变量指向的目录。

### 13.5 删除打包产物

```bash
rm -rf <项目根>/dev/dist <项目根>/dev/build
```

```powershell
Remove-Item -Recurse -Force C:\path\to\aliceAgent\dev\dist, C:\path\to\aliceAgent\dev\build
```

- 如果曾把 `anvil-dist` 加入 PATH，请从用户环境变量中移除该目录；
- 如果创建过桌面快捷方式、计划任务或系统服务，也一并删除。

### 13.6 清理环境变量与临时文件

删除为 Anvil 设置的 `ANVIL_*` 环境变量，尤其是：

- `ANVIL_DATA_DIR`
- `ANVIL_AUTH_TOKEN`
- `ANVIL_USER_RUNTIMES_DIR`
- `ANVIL_PORT`

Windows 用户环境变量可通过「系统属性 → 环境变量」删除，或：

```powershell
[Environment]::SetEnvironmentVariable("ANVIL_DATA_DIR", $null, "User")
[Environment]::SetEnvironmentVariable("ANVIL_AUTH_TOKEN", $null, "User")
```

清理临时日志：

```bash
rm -f /tmp/anvil-*.log                   # macOS
```

```powershell
Remove-Item "$env:TEMP\anvil-*.log" -ErrorAction SilentlyContinue
```

确认端口已释放：

```bash
lsof -nP -iTCP:7317 -sTCP:LISTEN         # 应无输出
```

```powershell
netstat -ano | findstr :7317              # 应无输出
```

### 13.7 彻底删除源码目录

```bash
rm -rf <项目根>
```

```powershell
Remove-Item -Recurse -Force C:\path\to\aliceAgent
```

> 这会同时删除源码、数据目录和所有未备份内容；执行前请确认备份已完成。

### 13.8 关于 Claude Code 等运行时

Anvil 只是调用本机已安装的 CLI Agent，卸载 Anvil **不需要**卸载它们。如果确实要移除，
请按各工具自己的文档操作（例如 Claude Code 的 npm 安装可用
`npm uninstall -g @anthropic-ai/claude-code`；原生安装按其官方卸载说明）。

---

## 14. 常见问题

### Q1. 浏览器打开 7317 是 404 / 空白页

前端尚未构建。执行：

```bash
cd dev/frontend && npm install && npm run build
```

然后重启后端。开发时也可以用 `npm run dev` 访问 5173。

### Q2. `anvil init` 提示“后端未启动”

先启动 `python run_server.py`，确认 `http://127.0.0.1:7317/api/v1/tasks` 可访问，再运行 CLI。

### Q3. 运行时状态是 `missing`

- 确认 CLI Agent 已安装：`claude --version` / `codex --version` / `gemini --version`；
- 确认它在当前 shell 的 PATH 中（Windows 注意重新打开终端使 PATH 生效）；
- 在界面「运行时」页重新点击 **扫描本地运行时**；
- Windows 常见安装位置（新版会自动尝试）：
  - 原生安装：`%USERPROFILE%\.local\bin\claude.exe`（该目录默认不在 PATH 中）；
  - npm 全局安装：`%APPDATA%\npm\claude.cmd`；
  - 旧版本地安装：`%USERPROFILE%\.claude\local\claude.exe`；
  - Bun：`%USERPROFILE%\.bun\bin\claude.exe`；
  - WinGet：`%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe`。
- 若仍为 `missing`，查看状态列下方的**探测日志**，它会列出已尝试的 PATH、npm 全局包与固定路径。

### Q4. Windows 启动报 `No module named 'win32job'`

```powershell
pip install pywin32
```

确认安装在后端使用的同一个 venv 中。

### Q5. 角色不执行 / 一直“等待授权”

- 默认权限模式是 `bypassPermissions`（`ANVIL_DEFAULT_PERMISSION_MODE`），适用于本地单用户环境；
- 如果角色被显式配置成 `default`，在 `--print` 非交互模式下会停在等待授权；
- 查看 `data/agents/<slug>/logs/run-<run_id>.log` 与运行详情页的失败分类（`platform_timeout` / `agent_exit_N` / `spawn_error`）。

### Q6. 角色状态显示灰色

灰色 = 空闲，不是故障。常驻会话在空闲超过 `ANVIL_IDLE_RELEASE_MINUTES`（默认 15 分钟）后会退出，`session_id` 保留，下次唤醒用 `--resume` 恢复上下文。

### Q7. 端口 7317 被占用

macOS：

```bash
lsof -nP -iTCP:7317 -sTCP:LISTEN
```

Windows：

```powershell
netstat -ano | findstr :7317
```

优先停止旧实例；确需换端口时，按 [9.4 节](#94-修改端口时的额外步骤) 同步修改运行时 YAML 与 CLI 的 `ANVIL_ENDPOINT`。

### Q8. 写操作返回 `401 missing X-Anvil-Session`

- Web：确认请求经由本应用页面发出；若设置了 `ANVIL_AUTH_TOKEN`，按 [9.3 节](#93-鉴权) 配置浏览器 token；
- CLI：设置 `ANVIL_TOKEN`；默认空 token 时 CLI 会自动带 `X-Anvil-Session: anvil`。

### Q9. 审批/删除操作返回 403

说明当前请求带的是 Agent 角色身份。人工确认点只接受自然人（不带 `X-Anvil-Role` 或 `X-Anvil-Role: human`）。请退出 Agent 环境，用界面或普通终端执行。

### Q10. 测试全部 skipped

venv 缺少 `langgraph`。执行 `pip install -r backend/requirements.txt`，再运行测试。

### Q11. 修改了任务但 Agent 看不到最新配置

运行使用**配置快照**：配置在 run 创建时冻结，中途修改角色/工具不影响已开始的运行。等该 run 结束后重新派发，或使用 run 详情页的重启/重置操作。

### Q12. Claude Code 能对话但 Agent 调不到 `anvil`

`anvil` 不在 Agent 子进程的 PATH 中。确保：

- 开发模式：`pip install -e agentcli` 装在启动后端所用的 venv 中，并且后端从该 venv 启动；
- 打包模式：把 `anvil-dist` 目录加入 PATH，或把 `anvil` 复制到系统 PATH 中的目录。

### Q13. 「AI 生成并创建 / 生成画像」失败

该功能现在通过 **CLI 运行时**执行，不再直连模型 API。错误信息会显示后端返回的 `detail`，常见原因：

1. **没有可用运行时**：到「运行时」页扫描，确认至少一个 `ready`，并在「默认」列点击 **设为默认**。
2. **运行时未登录/不可用**：先在终端执行 `claude --version` 并做一次最小调用；Codex / Gemini 同理。运行时自身的登录态或 `ANTHROPIC_*` 配置由它自己读取。
3. **角色自己的运行时不可用**：角色详情「生成画像」优先使用角色绑定的运行时；可在角色配置里改回默认运行时。
4. **超时**：默认 120 秒，可用 `ANVIL_UTILITY_TIMEOUT` 调整。
5. **退出码/无输出**：错误信息会带上运行时的 stderr/退出码；据此检查运行时安装或登录。

> 该功能与 Agent 任务执行共用同一套运行时；只要运行时的健康检查能通过，生成画像通常也能工作。

---

## 15. 安全说明

Anvil 面向**本机、单用户**场景，默认假设只有你一个人使用这台机器：

- 服务绑定 `127.0.0.1`，不要改成 `0.0.0.0` 暴露到局域网或公网；
- 默认 token 只做“存在性”轻校验，不是强认证；需要更强隔离时设置 `ANVIL_AUTH_TOKEN`；
- 默认权限模式为 `bypassPermissions`：Agent 可以在工作目录执行命令、读写文件。请只绑定你信任的运行时与角色；
- 评审、审批、删除、重置、预算修改等人工确认点由 `require_human` 强制限制，Agent 无法代替人类操作；
- 不要在 `ANVIL_TEST_AUTH=1` 下运行生产实例。

---

## 16. 相关文档

| 文档 | 内容 |
|---|---|
| [`design.md`](design.md) | 设计文档 v3.0：架构、运行时抽象、角色/工作区/任务板/编排/API/前端 |
| [`dev/README.md`](dev/README.md) | 源码树约定与开发快速开始 |
| [`dev/implementation-status.md`](dev/implementation-status.md) | 各里程碑实现状态、已修复项与未实现清单 |
| [`dev/tests/README.md`](dev/tests/README.md) | 测试骨架与契约说明 |
| [`optimization-roadmap.md`](optimization-roadmap.md) | 优化路线图 |
| `dev/acceptance-tests.md` | 验收测试场景 |
| `dev/spec-inbox-ia.md` | 收件箱信息架构规格 |

---

**当前状态**：项目仍在活跃开发中。运行方式与配置以本文件与 `dev/implementation-status.md` 为准；遇到行为差异时，优先查看后端启动日志与 `data/agents/<slug>/logs/` 下的运行日志。
