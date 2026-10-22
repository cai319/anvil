# Anvil — 本地多 Agent 协作编排系统

**设计文档 v3.0**
平台：Windows 10/11 · 单机单用户 · 后端 Python · 前端 Web

---

## 第一章 概述与设计目标

### 1.1 问题陈述

现有的 CLI Agent（Claude Code、Codex CLI、Gemini CLI 等）单独使用时能力已经足够强，但每次会话都是一次性的：上下文用完即弃，经验不沉淀，角色定位靠每次手打提示词重建，多个任务只能串行地在同一个终端里排队。当工作量超过一个人一次性能盯住的规模时，缺的不是模型能力，而是**编排、持久化与分工**。

Anvil 要解决的就是这一层。它不重新实现 Agent，不代理模型调用，不自己写工具循环。它做的是：把本机已装的 CLI Agent 运行时抽象成可复用的**角色**，给这些角色各自的持久工作区和记忆，用一块**任务板**加**认领机制**让它们并行地各自领活干，并让长期运行中积累的经验以文件形式沉淀下来、反哺角色定义本身。

### 1.2 核心设计立场

整个系统建立在六个决定性判断之上。这六条决定了后面所有章节的形态，理解它们比理解任何具体接口都重要。

**其一，Agent 是驾驶员，不是被调用的函数。** Anvil 不去解析 Agent 的 stdout 猜它干完了没有。相反，Anvil 向 Agent 暴露一个命令行工具 `anvil`，Agent 在自己的工具循环里主动执行 `anvil task claim`、`anvil task submit`、`anvil memory append`。编排器只负责启动进程、注入环境、等待声明。这是 raft 已经验证过的做法，也是唯一能在"不接管工具循环"的前提下拿到可靠状态的做法。输出解析是脆的，主动声明是稳的。

**其二，只支持自带工具循环的 CLI Agent 作为执行体。** 裸的 OpenAI 兼容端点不作为 worker 接入，因为那意味着 Anvil 得自己实现 ReAct 循环、工具注册、文件编辑、diff 应用——那是另一个项目。裸端点（含本机 Ollama）只承担 **utility 角色**：摘要、打标签、生成嵌入、格式化——这类无需工具调用的内务活。

**其三，两种工作区并存。** 每个角色有一个**私有 home**，放它的自我定义、记忆、草稿、日志，不与他人共享，也不进 Git 主干。另有一个或多个**共享 Git 仓库**作为真正的产出地，多个 Agent 通过分支和提交在其中协作。这一点比 raft 更进一步——raft 的工作空间是纯私有且不可迁移的，共享靠频道和附件；Anvil 认为对单人多 Agent 场景来说，共享代码仓库是刚需。

**其四，进程不持久，记忆持久。** 一个角色空闲时它没有任何进程活着，占用为零。被唤醒时拉起子进程、投影配置、注入上下文、执行、退出。角色的连续性完全由磁盘上的文件承载，不由内存承载。安静的角色不等于坏掉的角色。

> **owner 2026-10-19 修正**：改为**常驻会话优先**——每个角色一个长期存活的 CLI 会话进程（claude-code 用 `--input-format stream-json` 双向流），唤醒 = 向 stdin 投递消息（同一上下文，不再重启进程）；空闲 = 阻塞等待；**空闲超过 `idle_release_minutes`（默认 15）优雅退出并保留 session_id**，下次唤醒用 `--resume <session_id>` 恢复同一上下文。`runs` 仍是一次执行记录。不支持流式协议的运行时回退 one-shot 模型。动机：实测 one-shot 模型下每次 @mention 唤醒都新建进程，上下文不连续且易叠出孤儿进程（同一角色 3-5 个）。

**其五，工具清单是带预算的一等配置。** skill 与 MCP 的**能力**完全继承自运行时，Anvil 不自造工具体系；但**加载哪些**由 Anvil 决定并强制约束。默认全量加载会让上下文里塞进几十个工具描述，几千 token 的固定开销，直接压缩有效工作空间。所以工具清单支持白名单裁剪、按任务条件加载、以及预设组合，并配一个 token 硬预算。

**其六，单进程编排，并发来自子进程。** 后端是一个 asyncio 单进程，不引入 Celery、Redis、消息队列。真正吃 CPU 和上下文的是各个 Agent 子进程，编排器本身只是个状态机加进程管家。单机单用户场景下，任何分布式组件都是净负债。

### 1.3 范围界定

**做：** 运行时发现与绑定、角色定义与投影、私有与共享工作区、任务板与认领、并发编排与租约、Agent 侧 CLI、文件优先的记忆与知识库、受控的角色演化、Web 操作界面、Reminder 定时唤醒、成本与 token 观测。

**不做：** 账号体系与软件注册（单人本机，无此必要）、多人协作与团队移交、手机端、模型 API 代理转发、自研工具循环、容器级强隔离（列为 v4）、向量检索（v1 只用 SQLite FTS5，v2 再评估）。

### 1.4 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 后端框架 | FastAPI + Uvicorn | 自带 OpenAPI，前端可直接生成类型 |
| 并发模型 | asyncio 单进程 | 无需分布式，子进程即并发单元 |
| 存储 | SQLite（WAL 模式） | 单文件、零运维、事务足够 |
| ORM | SQLAlchemy 2.0 async | 成熟，async 支持已稳定 |
| Git | pygit2 | 绑定 libgit2，无需 shell 出 git |
| 校验 | Pydantic v2 | 配置 schema 与 API 模型统一 |
| 密钥 | keyring（Windows 凭据管理器） | 不落明文 |
| 全文检索 | SQLite FTS5 | 内建，无外部依赖 |
| 前端 | Vite + React 18 + TypeScript | 标准化工程 |
| 组件 | shadcn/ui + Tailwind | 可控、无重型运行时 |
| 数据层 | TanStack Query + Zustand | 服务端状态与本地状态分离 |
| 图表 / 拖拽 | Recharts / dnd-kit | 任务板拖拽与成本图 |
| 实时推送 | SSE | 单向为主，比 WebSocket 轻 |
| 打包 | PyInstaller | 单 exe，前端静态资源内嵌 |
| 客户端生成 | openapi-typescript | 消除手写接口类型 |

服务监听 `127.0.0.1:7317`，仅绑定回环地址，请求携带本地 token 做轻量校验（防止本机其他程序误触，而非防御真实攻击者）。

### 1.5 顶层架构

```
┌─────────────────────────────────────────────────────┐
│  Web UI (Vite/React)  ← SSE ← REST                  │
└────────────────────────┬────────────────────────────┘
                         │ 127.0.0.1:7317
┌────────────────────────▼────────────────────────────┐
│  FastAPI 应用层                                      │
│  ├─ Runtime Registry   运行时发现 / 探针 / 定义加载   │
│  ├─ Profile Service    角色 CRUD / 投影 / 快照        │
│  ├─ Task Board         任务状态机 / 认领 / 依赖        │
│  ├─ Orchestrator       调度 / 并发闸门 / 租约 / 唤醒   │
│  ├─ Workspace Service  私有 home / Git 共享仓库       │
│  ├─ Memory Service     文件记忆 / FTS 索引 / 升格      │
│  ├─ Evolution Service  自主编辑区 / 审批队列          │
│  └─ Agent CLI Endpoint （anvil 命令的服务端）         │
└──────┬──────────────────────────────┬───────────────┘
       │                              │
┌──────▼──────┐              ┌────────▼─────────┐
│ SQLite(WAL) │              │ 文件系统          │
│ 状态/事件/  │              │ agents/<slug>/   │
│ FTS 索引    │              │ repos/<name>/    │
└─────────────┘              │ runtimes/*.yaml  │
                             └──────────────────┘
       │ spawn
┌──────▼──────────────────────────────────────────────┐
│ Agent 子进程（claude / codex / gemini …）             │
│   ↑ 反向调用 anvil CLI → HTTP → 编排器                │
└─────────────────────────────────────────────────────┘
```

数据库存的是**状态与索引**，文件系统存的是**内容与真相**。当两者冲突时以文件为准，数据库可重建。这条规则让系统在崩溃后总能靠重扫恢复。

---

## 第二章 Agent 运行时抽象与绑定

### 2.1 什么算一个运行时

一个运行时是本机上一个**可执行的、自带工具循环的 CLI Agent 程序**。判定标准有三条：能通过命令行接受一段任务描述、能自主读写文件并执行命令、能在完成后退出并返回状态码。满足这三条的就能接入，包括 Claude Code、Codex CLI、Gemini CLI、Copilot CLI、Cursor CLI、Kimi CLI、OpenCode 等。

模型订阅与 API key 归用户自己，Anvil 不做任何代理中转，也不存储模型凭据（凭据由运行时自己管，Anvil 最多在启动子进程时从 Windows 凭据管理器取出注入环境变量）。

### 2.2 运行时定义 YAML

这是全系统的枢纽机制。每种运行时对应一份声明式 YAML，放在 `runtimes/` 下。新增一种运行时支持，**绝大多数情况下只需要加一份 YAML 加一个 schema 转换函数**，不动核心代码。

```yaml
# runtimes/claude-code.yaml
id: claude-code
display_name: Claude Code
version_range: ">=1.0.0"

discovery:
  # 按顺序尝试，命中即止
  strategies:
    - type: path_lookup
      command: claude
    - type: npm_global
      package: "@anthropic-ai/claude-code"
    - type: fixed_path
      candidates:
        - "%LOCALAPPDATA%\\Programs\\claude\\claude.exe"
  version_probe:
    args: ["--version"]
    parse: "regex:(\\d+\\.\\d+\\.\\d+)"

invocation:
  # {{ }} 由编排器在启动前渲染
  args:
    - "--print"
    - "--permission-mode"
    - "{{ permission_mode }}"
    - "{{ prompt }}"
  cwd: "{{ workspace_path }}"
  env:
    ANTHROPIC_API_KEY: "{{ secret:anthropic_key }}"
    ANVIL_TASK_ID: "{{ task_id }}"
    ANVIL_AGENT_SLUG: "{{ agent_slug }}"
    ANVIL_ENDPOINT: "http://127.0.0.1:7317"
    ANVIL_TOKEN: "{{ session_token }}"
  timeout_seconds: 3600
  exit_code_success: [0]

capabilities:
  managed_mcp: true            # 运行时自身支持挂载 MCP server
  managed_skills: true         # 支持 skill/命令目录
  tool_level_grant: exact      # exact | server | inherit
  session_resume: true         # 支持带会话 id 续跑
  streaming_output: true
  supports_subagents: true
  model_selection: true
  thinking_effort: true

# ── projection：本运行时在何处、以何格式读取配置 ──
projection:
  instruction:
    target: "{{ workspace_path }}/CLAUDE.md"
    format: markdown
    strategy: overwrite_managed_block   # 只覆盖 Anvil 托管的标记块
  skills:
    target: "{{ workspace_path }}/.claude/skills/"
    format: directory_of_md
    strategy: symlink_or_copy
  mcp:
    target: "{{ workspace_path }}/.claude/settings.json"
    format: json
    pointer: "mcpServers"
    schema: claude_mcp_v1               # 对应一个转换函数
  permissions:
    target: "{{ workspace_path }}/.claude/settings.json"
    pointer: "permissions"
    schema: claude_permissions_v1
  tool_filter:
    mechanism: allowed_tools_field
    pointer: "permissions.allow"
    supports_deny: true

health:
  probe_prompt: "Reply with exactly: ANVIL_OK"
  expect_contains: "ANVIL_OK"
  timeout_seconds: 60
```

`projection` 段是关键。它把"Anvil 的角色定义"翻译成"这个运行时能读懂的磁盘状态"。Anvil 内部只有一套角色模型，投影层负责差异化落地。`schema:` 字段指向一个 Python 转换函数（如 `claude_mcp_v1`），处理各运行时 MCP 配置格式的细微差别。

### 2.3 工具裁剪精度分级

不同运行时对"只加载部分工具"的支持程度差别很大，这个差别必须对用户明示，不能假装统一。

| `tool_level_grant` | 含义 | UI 呈现 |
|---|---|---|
| `exact` | 可按单个工具名精确允许/拒绝 | ✅ 精确裁剪，勾选即生效 |
| `server` | 只能整个 MCP server 开关，不能选其中单个工具 | ⚠️ 服务器级，最小粒度是整个 server |
| `inherit` | 无法裁剪，运行时读全局配置 | ⛔ 继承全局，Anvil 无法约束，只能靠提示词告知 |

界面上直接把这三档标在角色配置页的工具区，配文字说明。对 `inherit` 档的运行时，Anvil 仍然会在指令里写清"本轮只应使用以下工具"，但这只是软约束，不做任何承诺。**明确告诉用户哪里管不住，比让用户误以为管住了要重要得多。**

### 2.4 绑定流程

1. **扫描**：遍历 `runtimes/` 下所有 YAML，按 `discovery.strategies` 依次尝试定位可执行文件。
2. **探针**：命中后执行 `version_probe`，解析版本号，与 `version_range` 比对。版本超出范围时标记为"已发现但未验证"，允许用户强制启用。
3. **健康检查**：执行 `health.probe_prompt`，在临时目录里跑一次最小任务，确认能正常返回。这一步会真实消耗一次极小的额度。
4. **落库**：记入 `runtimes` 表，状态为 `ready` / `unverified` / `unhealthy` / `missing`。
5. **手动补录**：扫描不到的运行时允许手填可执行路径，走同样的探针与健康检查。

绑定结果在"运行时"页以卡片列出，每张卡显示路径、版本、能力矩阵、裁剪精度、上次健康检查时间。用户可随时重新探测。

### 2.5 数据模型

```sql
CREATE TABLE runtimes (
  id              TEXT PRIMARY KEY,       -- claude-code
  display_name    TEXT NOT NULL,
  executable_path TEXT,
  detected_version TEXT,
  status          TEXT NOT NULL,          -- ready|unverified|unhealthy|missing
  capabilities    TEXT NOT NULL,          -- JSON 快照
  definition_hash TEXT NOT NULL,          -- YAML 内容哈希，变更时提示重探
  last_probe_at   TIMESTAMP,
  last_probe_log  TEXT,
  enabled         INTEGER NOT NULL DEFAULT 1
);
```

`definition_hash` 的作用是：当用户手改了 YAML，系统能察觉并提示相关角色需要重新投影。

### 2.6 运行时配置接口（本地搜索 + 手工定制）

（owner 2026-09-16 决策：运行时需同时支持**本地自动搜索**与**人工定制化配置**两条路径。）配置接口分两层：

**第一层 · 发现策略（本地自动搜索）**——`discovery.strategies` 是有序列表，命中即止：

| type | 用途 | 关键字段 |
|---|---|---|
| `path_lookup` | 在 PATH 中找可执行 | `command` |
| `npm_global` | npm/pnpm/bun 全局包根 | `package` |
| `fixed_path` | 固定候选路径（含环境变量占位） | `candidates[]` |
| `manual` | 人工指定绝对路径（兜底，见第二层） | `path` |

**第二层 · 手工定制化配置**——内置库未覆盖或需覆盖自动发现时，手填一份定义（最小必填 + 可选扩展）：

```yaml
# runtimes/custom.yaml —— 手工注册最小接口
id: my-agent
display_name: 我的 Agent
executable: "C:/tools/my-agent.exe"      # 必填：绝对路径
invocation:
  args: ["-p", "{{prompt}}", "--cwd={{cwd}}"]
  env: {MY_KEY: "{{secret:my_key}}"}
  timeout_seconds: 1800
capabilities:
  tool_level_grant: inherit               # 默认最小，逐项按实测调高
projection: {}                            # 默认不投影（运行时读自身全局配置）
```

手工注册与内置运行时走同一套探针（`/runtimes/{id}/probe`）与健康检查。四个接口字段：`executable`（路径）、`invocation`（参数/环境模板）、`capabilities`（能力矩阵，默认保守）、`projection`（投影规则，默认空）。

**注册/配置 REST 面**（对齐第十章）：

```
POST   /api/v1/runtimes/discover             触发本地搜索，返回候选清单
POST   /api/v1/runtimes                      手工注册（body = 上表结构化 JSON）
GET    /api/v1/runtimes/{id}/capabilities    前端据此决定显示哪些配置项
PUT    /api/v1/runtimes/{id}                 编辑定义（definition_hash 变更 → 提示重探）
POST   /api/v1/runtimes/{id}/probe           版本探针 + 健康检查
DELETE /api/v1/runtimes/{id}                 删除自定义运行时（内置项仅 disable）
```

**存储**：内置定义在 `runtimes/*.yaml`（随包分发），用户自定义/覆盖在 `%APPDATA%/Anvil/runtimes/*.yaml`；`runtimes` 表只存发现态（path/version/status/capabilities/definition_hash），真相在 YAML。

---

## 第三章 角色模型

> **实现口径（owner 2026-10-18 决策）**：角色在 v1 基础上补齐了三点——① `system_prompt`（系统级行为准则）+ `responsibilities`（职责列表），注入 `CLAUDE.md` managed block；② **skill/MCP 治理（M3）**：角色绑定运行时后，按该运行时自身的默认 skill/MCP 目录（YAML `catalog` 段）勾选加载，spawn 时把勾选的 skill symlink 到 `.claude/skills/`、把 MCP 写入 `.claude/settings.json`；③ **LLM 自动生成画像**（`POST /roles/{id}/generate`）：输入一句描述，调环境端点自动生成工作模式/技能描述/职责/系统提示，且 system_prompt 内置「主动评论同步进展、@提及协作角色」的交互行为。
>
> **实现口径变更（owner 2026-10-21）**：③ 改为通过 **CLI 运行时**执行（运行时页选定的默认运行时；角色详情优先用角色自身绑定的运行时），复用运行时自己的登录态/网关配置，**不再直连模型 API**，也不再要求后端单独配置 `ANTHROPIC_*`。

### 3.1 角色是什么

一个角色（Agent Profile）= 一个绑定的运行时 + 一份职责定义 + 一份工具清单 + 一个私有工作区 + 一份持续演化的记忆。它是长期存在的实体，不是一次调用的参数集。

角色的价值在于**复用与积累**：你不需要每次都告诉它"你是负责数据库迁移的工程师，注意我们用 Alembic，历史上踩过 XX 坑"，这些沉淀在它自己的定义和记忆里。

### 3.2 字段定义

```yaml
# agents/backend-dev/profile.yaml
slug: backend-dev                 # 主键，不可改
display_name: 后端工程师
avatar_color: "#2563eb"

runtime:
  id: claude-code
  model: claude-sonnet-4-6        # 可选，运行时支持时生效
  thinking_effort: medium         # low|medium|high
  cli_extra_args: []              # 直接拼进 argv

# ── 两类文本，用途严格区分 ──
description: |
  负责服务端 API 与数据库层。熟悉本项目的 Alembic 迁移惯例。
  【此字段仅供人看，不进入任何提示词】

instruction_file: instruction.md  # 真正注入运行时的内容

responsibilities:                 # 结构化职责，用于任务匹配与打分
  - api-design
  - database
  - backend-testing

capabilities:                     # 领域权重，认领打分时使用
  python: 0.9
  sql: 0.85
  frontend: 0.15

workspace:
  home: agents/backend-dev/       # 私有，自动创建
  repos:                          # 可访问的共享仓库
    - name: main-project
      access: write
      branch_prefix: agent/backend-dev/
    - name: docs
      access: read

tools:
  preset: backend-default         # 引用 tools.yaml 中的预设
  max_tool_tokens: 3500           # 硬上限

concurrency:
  max_parallel: 2                 # 该角色同时最多几个运行

claim:
  auto_claim: true
  accept_task_types: [feature, bugfix, refactor]
  reject_task_tags: [frontend-only, design]

reminders:
  - cron: "0 9 * * 1"
    prompt: 回顾上周完成的任务，整理经验到 MEMORY.md，并检查自己的 profile 描述是否还准确。

lifecycle:
  session_reset_after_tasks: 10   # 每 N 个任务后清一次上下文
  idle_release_minutes: 15
```

**`description` 与 `instruction` 的区分是从 multica 借来的，很值得照搬。** 描述用来在界面上让你一眼认出这个角色是干嘛的，写得再长也不花 token；指令才是真正注入的内容，要精简。混在一起会导致人为了看清而写长描述，白白吃掉每一轮的上下文。

### 3.3 配置快照

**修改角色配置不影响已经开始的运行。** 一个运行在被创建（认领成功）的那一刻，会把当时的完整 profile 序列化成快照存入 `runs.profile_snapshot`。子进程从快照读配置，而不是从当前配置读。

这样做的原因很实际：一个任务跑了四十分钟，你中途改了工具清单，如果热生效，会出现"同一次运行前后半段行为不一致"的诡异现象，事后完全无法复盘。快照让每次运行都是可解释的。这一点也与 multica 的做法一致。

### 3.4 角色目录布局

```
agents/backend-dev/
├── profile.yaml         # 配置（可被 Anvil 与用户编辑）
├── instruction.md       # 注入运行时的指令
├── MEMORY.md            # 长期记忆，Agent 自主维护
├── notes/               # 自由笔记，Agent 自主维护
│   ├── alembic-gotchas.md
│   └── 2026-w37-review.md
├── scratch/             # 临时草稿，不纳入记忆索引，可清空
├── skills/              # 该角色专属 skill（投影到运行时）
├── logs/
│   └── run-<id>.log     # 每次运行的完整 stdout/stderr
└── .anvil/
    ├── projection.lock  # 上次投影的内容哈希，避免重复写
    └── pending/         # 待审批的自我修改提案
```

私有 home 不进 Git 主干，但整个 `agents/` 目录可以单独做一个本地 Git 仓库自动提交，作为角色演化的历史留痕——这样你随时能看到"这个角色的记忆在过去两周长成了什么样"。

### 3.5 投影过程

启动一次运行前，编排器执行投影：

1. 从快照读取 profile。
2. 渲染 `instruction.md` + 本次任务上下文 + 记忆摘要 → 按 `projection.instruction` 写入目标文件。写入采用 **managed block 策略**：只覆盖 `<!-- ANVIL:BEGIN -->` 与 `<!-- ANVIL:END -->` 之间的内容，块外用户手写的部分保留。
3. 解析工具清单 → 生成裁剪后的 MCP 配置与 allow/deny 列表 → 按 schema 转换写入。
4. 复制或软链 skill 目录。
5. 写权限配置。
6. 计算全部投影内容的哈希，与 `projection.lock` 比对，无变化则跳过写盘（减少无谓的文件变动）。

投影失败视为运行启动失败，任务退回 `todo` 并记录事件，不静默降级。

---

## 第四章 工作区模型

### 4.1 两类工作区

**私有 home**（第 3.4 节的目录）是角色的"自我"所在。它不共享、不迁移，是角色记忆和身份的物理载体。raft 的做法是工作空间纯私有且明确不可迁移，Anvil 在私有部分沿用这一判断——记忆与角色是绑定的，试图"把 A 的工作区给 B 用"在语义上就说不通。

**共享 Git 仓库** 是产出的地方。多个角色对同一仓库有各自的分支命名空间（`agent/<slug>/<task-id>`），互不覆盖。仓库通过 pygit2 操作，Anvil 负责建分支、检查 diff 范围、合并到主干。

```
repos/
├── main-project/           # 真正的 Git 仓库（bare 或普通均可）
└── docs/
worktrees/                  # 每次运行的独立 worktree
└── run-01H8.../            # 运行结束后按策略保留或清理
```

**每次运行使用独立的 Git worktree**，而不是让多个 Agent 在同一个工作目录里互相踩。worktree 从目标分支创建，运行结束后 diff 被审查、提交、合并，然后按配置保留 N 天以便复盘。

### 4.2 共享区结构

在共享仓库中，Anvil 约定一个 `.anvil/` 目录承载跨角色协作物：

```
main-project/
├── .anvil/
│   ├── shared/
│   │   ├── lessons.md          # 跨角色经验，上限 80 条
│   │   ├── conventions.md      # 项目约定（人写为主）
│   │   └── glossary.md         # 术语表
│   └── handoff/                # 任务间交接说明
│       └── T-1042.md
└── （项目本身的文件）
```

`lessons.md` 是唯一的跨角色知识共享点，设 80 条硬上限（详见第八章）。上限的意义是强迫淘汰：一个无上限增长的经验文件最终会变成没人读的垃圾场，且每次注入都吃掉大量 token。

### 4.3 diff 越界校验

Agent 提交时，Anvil 比对本次运行的实际改动文件列表与任务声明的允许范围，越界则拒绝合并、任务转入 `in review` 并标记异常。

允许范围来自三处的交集：任务的 `scope_paths` 字段、角色的仓库 `access` 权限、以及全局的 `.anvilignore` 保护清单（如 `.git/`、`.anvil/shared/`、CI 配置、密钥文件默认禁改）。

必须诚实说明这道防线的位置：**它在提交时生效，不在执行中生效。** Anvil 不接管工具循环，因此无法在 Agent 读取或修改文件的那一刻拦截。三道防线分别是提示词告知（软）、运行时权限配置约束（取决于运行时能力）、提交时 diff 校验（硬，但事后）。第三道能保证不该改的东西进不了主干，但**Agent 执行过程中读了什么、在 worktree 里试过什么，管不住**。真正的强隔离需要 WSL2 或容器，列为 v4。

### 4.4 上下文注入预算

每次运行注入给 Agent 的上下文按固定优先级组装，并有整体预算：

| 段 | 内容 | 典型预算 |
|---|---|---|
| 身份 | instruction.md | 800 tok |
| 任务 | 标题、描述、验收标准、scope | 600 tok |
| 记忆 | MEMORY.md 摘要 + FTS 检索到的相关 notes 片段 | 1200 tok |
| 共享 | lessons.md 中与任务标签匹配的条目 | 500 tok |
| 协议 | anvil CLI 用法说明 | 400 tok |
| 工具 | 工具描述（由运行时自身注入） | ≤ max_tool_tokens |

超预算时按优先级从后往前截断，记忆段优先保留 FTS 得分最高的片段。截断事件记入日志——如果某个角色频繁截断，说明它的记忆需要整理了，这本身是个有用信号。

---

## 第五章 任务板与认领机制

> **实现口径（owner 2026-10-18 决策）**：任务支持**绑定多角色一起实施（团队协作）**——新增 `task_assignees`（任务↔角色多对多）；「指派并执行」为每个勾选的角色并行派发一个 run（同任务同角色最多一个活跃 run），agent 各自认领/干活/提交。`assignee` 仍是单一「lead」（认领者），`assignees` 是完整团队。
> **PM 规划 + 依赖感知 + @交接（流水线协调）**：目标拆解走**工作流模板库**（SOP 预设 `design-dev-test`/`dev-test`/`research-dev`/`fix-test`），产出带线性 `depends_on` + 角色指派的任务树。调度器**依赖感知派发**——只派发「所有 `depends_on` 已 done」的任务（有绑定角色的按角色派发，无角色的自动挑 eligible agent）。任务 `review approve → done` 时自动 `@提及` 依赖它的下游任务的参与角色（「上游任务 X 已完成，可以开始工作」），作为「可以开工」的交接信号——对应 raft「上游完成 @下游才开工」的模式。
> **Workflow 模型（owner 2026-10-19 修正）**：Task 是人类的高层工作空间；Cindy 产出的是 **workflow（步骤状态机）**，不是子任务树。步骤不是独立 task 行（存 `workflows` / `workflow_steps`）：跨角色可并行，**同一角色的多个步骤由引擎串行调度**；`gate=1` 的步骤提交后进入收件箱等待自然人审核；全部步骤完成 → 任务进入最终评审。任务大厅只展示高层任务；角色运行时（会话）状态收敛在任务详情的「角色运行时」页签。
> **Onboarding 助理 Cindy（owner 2026-10-19）**：多角色任务派发前，先由内置的人类规划助理 `cindy`（`kind=onboarding`，非任务参与角色）分析任务并设计协作工作流——谁主导、谁执行哪些子任务、阶段顺序与可并行关系（DAG）、验收标准/scope、反馈与升级路径。草案经人在收件箱批准后子任务落板，依赖感知派发；父任务转 `in_progress` 作为 epic。单角色任务直接派发。`POST /tasks/{id}/dispatch` 为统一入口，`ANVIL_ONBOARDING=0` 可关闭。

### 5.1 状态机

沿用 raft 已验证的五态设计，不自创：

```
todo ──claim──> in progress ──submit──> in review ──approve──> done ──> closed
  ▲                  │                      │
  └──── unclaim ─────┘                      └── reject ──> in progress
  ▲                                                              │
  └──────────────────── 租约超时回收 ─────────────────────────────┘
```

关键约束：**任何时刻一个任务最多一个负责人。** 认领是排他的，通过数据库事务保证。`unclaim` 允许 Agent 主动放弃（发现自己不适合、或阻塞在别的任务上），放弃后任务回到 `todo`，其他角色可以领。认领失败不是错误——Agent 收到"已被他人领取"后应当去看别的任务，这是正常流程。

Agent 可以创建子任务。子任务与父任务构成依赖，父任务在所有子任务 `done` 前不可 `submit`。

### 5.2 任务字段

```yaml
id: T-1042
title: 为 /api/orders 增加分页
type: feature                    # feature|bugfix|refactor|research|chore|review
status: todo
priority: 2                      # 0 最高
tags: [backend, api]
repo: main-project
base_branch: main
scope_paths:                     # diff 越界校验依据
  - "src/api/orders/**"
  - "tests/api/test_orders.py"
description: |
  ...
acceptance:                      # 验收标准，注入给 Agent
  - 支持 limit/offset，默认 20，上限 100
  - 补充分页边界测试
depends_on: [T-1039]
assignee: null
lease_expires_at: null
created_by: human                # human | agent:<slug>
parent_id: null
cost_budget_usd: 2.0             # 可选，超出则闸门拦截
```

### 5.3 认领策略

**默认先到先得。** 编排器把满足条件的任务列表暴露出去，Agent 主动 `anvil task claim T-1042`，第一个成功的拿到。这是最简单也最不容易出错的机制。

**加权打分是可选项，不是默认。** 开启后，编排器在派发唤醒提示时优先通知得分最高的角色，但不阻止其他角色认领。打分公式：

$$\text{score} = w_c \cdot \text{cap}(a, t) + w_r \cdot \text{resp}(a, t) + w_l \cdot \text{load}(a) + w_h \cdot \text{hist}(a, t)$$

其中 `cap` 是角色 capabilities 权重与任务 tags 的加权匹配度，`resp` 是职责命中，`load` 是当前负载的反向项，`hist` 是历史上该角色处理同类任务的成功率。默认权重 `0.4 / 0.3 / 0.2 / 0.1`。

必须说清：**这个公式没有先例背书，是我拟的。** 参数需要你在实际使用中调。如果发现打分总在做出反直觉的选择，先怀疑公式而不是怀疑自己的判断，直接关掉退回先到先得完全可以接受。

### 5.4 认领的事务实现

```sql
BEGIN IMMEDIATE;
UPDATE tasks
   SET status = 'in progress',
       assignee = :slug,
       lease_expires_at = :now_plus_30min,
       claimed_at = :now
 WHERE id = :task_id
   AND status = 'todo'
   AND assignee IS NULL
   AND NOT EXISTS (
        SELECT 1 FROM tasks d
         WHERE d.id IN (SELECT depends_on FROM task_deps WHERE task_id = :task_id)
           AND d.status NOT IN ('done','closed')
   );
-- affected rows = 0 → 认领失败
COMMIT;
```

SQLite 的 `BEGIN IMMEDIATE` 加上单进程编排，足以保证排他性。不需要额外的锁服务。

> **实现口径（owner 2026-10-22）**：单负责人任务与团队任务走不同 claim 路径——
> 单负责人任务一旦 `assignee` 被占用就是排他的，其他角色不能 claim；团队任务按
> `task_assignees` 逐角色独立 claim。创建给其他角色的子任务时应使用
> `anvil task create ... --assignee <role>`（团队用 `--assignees a,b`）显式指派，
> 创建者不要先 claim；若已 claim 需要转交，先 `anvil task unclaim <id>` 再 @ 对方。
> claim 冲突返回 409，并直接给出 owner / unclaim / team assignee 的处理指引。

### 5.5 租约与心跳

任务被领走后持有一个**租约**：租期 30 分钟，Agent 每 2 分钟通过 `anvil task heartbeat` 续租，连续缺失 3 个周期（6 分钟）判定失联，租约回收，任务退回 `todo` 并在事件流留痕。

这套参数（30/2/3）**是我拟的，没有外部先例**。逻辑是：租期要长于常见的单次长思考，心跳要密于人能容忍的卡死时间，容忍 3 次是为了避开偶发的网络或调度抖动。实测中如果发现长任务频繁被误回收，先把租期拉长而不是把心跳加密。

被回收的运行会被标记 `abandoned`，其 worktree 保留供检查——很多时候失联意味着 Agent 卡在某个循环里，那个 worktree 是唯一的证据。

### 5.6 并发闸门

四层闸门，取最严者：

1. **角色级** `concurrency.max_parallel`，默认 2
2. **运行时级** 同一运行时的全局并发上限，默认 4（避免同一订阅被打爆）
3. **系统级** 全局并发上限，默认 6
4. **成本级** 当日累计花费超阈值时停止新建运行

两级并发上限的思路来自 multica（角色级默认 6、守护进程级默认 20，取较小值），Anvil 因为是单机个人使用，默认值调得更保守。成本闸门是我加的，没有先例，但对个人用户来说防止一晚上烧掉几百块是有实际价值的。

> **实现口径（owner 2026-10-22）**：角色/系统/运行时并发只统计
> `created/projecting/running` 的 run；空闲 claim 与 `suspended/submitted/reviewing`
> 会话不占 `max_parallel`。否则一个角色持有多个没有 run 的子任务 claim 就会把自己的
> 并发额度占满，导致后续任务无法被唤醒。

---

## 第六章 编排与执行引擎

> **实现口径（owner 2026-10-17 决策）**：编排器用 **LangGraph 作为编排控制面**落地，而非自建工具循环。运行生命周期是一个 `StateGraph`（节点 `project`/`spawn`/`submit`/`await_review`/`review`/`process_exit`），用 `SqliteSaver` 检查点持久化（独立 `anvil-graph.db`）、`interrupt()`/`Command(resume)` 做人机评审；**事件驱动**——外部 `claim`/`submit`/`heartbeat`/进程退出作为同一 thread_id 的图重入点，子进程 fire-and-forget 拉起后图即返回，节点绝不为子进程生命周期阻塞。CLI Agent 仍作为子进程执行，「Agent 是驾驶员」立场不变。调度循环由 `anvil/scheduler.py` 后台线程每 5 秒驱动：回收租约 → 派发 wake（spawn）→ monitor（探测退出 + 超时回收）→ reconcile（对账自愈崩溃窗口）。任务 5 态仍留在 `services.tasks`，图节点只 `UPDATE runs` 运行 8 态。**超时回收**：monitor 对超过 `RUN_TIMEOUT_SECONDS`（默认 600s）仍 `running` 的 run 自动 terminate 并标 `failed`，防 agent 卡死永久占用。SSE 事件总线用非阻塞轮询（`get_nowait` + keep-alive），不按连接开线程，避免导航频繁时线程池耗尽。
> **多轮挂起/resume（owner 2026-10-18 决策）**：agent 可被**反复唤醒续跑**。① `@ 即拉入`：@ 的是角色且为任务参与角色时，若该角色有挂起中的 run → 触发 `resume` 事件（清空 `native_session_id` → 重新投影 → 重新 spawn）；否则新起 run。② **会话上下文注入**：spawn 的 prompt 注入任务会话完整时间线（评论/@提及/认领/提交），使每轮续跑都读到最新内容。③ **挂起不误判**：agent @了角色等待响应时，`process_exit` 保持 `running`（挂起），不标 failed/abandoned。协议约定「@了别人等响应就挂起（不 submit），被 @回来再继续，最终完成才 submit」。

### 6.1 调度循环

编排器是一个 asyncio 任务，每 5 秒扫描一轮：

```python
async def tick():
    reclaim_expired_leases()          # 回收失联租约
    fire_due_reminders()              # 触发到期的 reminder
    for agent in eligible_agents():   # 通过全部四层闸门的角色
        tasks = matchable_tasks(agent)
        if not tasks:
            continue
        await wake(agent, hint=summarize(tasks))
```

`wake()` 做的事：创建 run 记录、冻结 profile 快照、准备 worktree、执行投影、拉起子进程。**唤醒提示只含任务 id 与标题，不含任务正文**——正文由 Agent 自己通过 `anvil task show` 取。这样编排器不承担内容组装的责任，也让 Agent 的信息获取路径统一。

> **Workflow 运行期切片（owner 2026-10-22）**：大型任务不再由 Cindy 预先切分。
> Cindy 只定义工作流骨架——一个 `kind=plan_slices` 切片计划步骤 + 顶层 `loop` 模板；
> 切片计划步骤由具备项目理解的角色运行，盘点目录/模块/依赖/风险面后通过
> `anvil step plan <wf> <step> --file slices.json` 提交切片。服务端按 loop 模板为每片
> 确定性展开 `scope? → execute[...] → verify → fix → verify2 → decide`：
> 下一片依赖上一片 `decide`，decision 打回时 `fix/verify2` 重做；
> 依赖切片计划步骤的静态下游步骤自动改挂到最后一片 `decide`。
> 切片步骤带 `source_step_id`/`slice_key`，状态变化实时写入任务会话（`activities.kind=step`）。
> 图谱只画一个 `↻ 切片循环` 节点，点击展开当前片的单一闭环流程。

### 6.2 运行生命周期

```
created → projecting → running → submitted → reviewing → completed
                          │                                  ▲
                          ├── failed（非零退出/超时） ─────────┘
                          └── abandoned（租约回收）
```

该 8 态由 LangGraph `StateGraph` 承载：`project`→`spawn`（拉起子进程）→ 图结束；`submit` 事件重入 → `submit`→`await_review`（`interrupt()` 挂起等人审）→`review`；`process_exit` 事件重入 → `failed`/`abandoned`（依据 run 自身状态，非 task 状态）。每次状态迁移落为 `activities`（kind=`run`）事件供时间线消费。

子进程的 stdout/stderr 全量落盘到 `agents/<slug>/logs/run-<id>.log`（`ProcessManager.spawn` 扩展 `stdout`/`stderr` 重定向），通过 `GET /runs/{id}/log` 读取；日志不入库（避免 SQLite 膨胀），库里只存文件路径。逐行实时推送 SSE 留作后续。

### 6.3 三档重置

照搬 raft 的分级，因为这个分级本身就是对的：

| 档位 | 动作 | 用途 |
|---|---|---|
| `restart` | 结束进程，保留会话 id，下次续跑 | 进程卡死但上下文还有用 |
| `session reset` | 丢弃会话，保留 MEMORY.md 与 notes | 上下文污染了，但经验要留 |
| `full reset` | 会话与记忆全清，仅保留 profile.yaml | 角色定义要重来 |

`full reset` 前自动把 `agents/<slug>/` 打包备份到 `backups/`，因为这个操作不可逆而且人经常后悔。

### 6.4 Reminder 机制

Reminder 是一等公民，不是附属功能。三种形态：

- **定时**（cron 表达式）：每周一早九点回顾整理
- **延时**（一次性）：两小时后检查 CI 是否通过
- **条件**：当任务 T-1039 转为 done 时唤醒我

Reminder 可由人设置，**也可由 Agent 自己设置**（`anvil reminder add`）。这一点从 raft 借来，它的文档就举了"让 Agent 设每周 reminder 检查更新自己的描述"这个例子。让 Agent 自己安排自己的复盘节奏，比人手动催要可持续得多。

到期的 reminder 生成一个特殊的 run（无关联任务），把 reminder 的 prompt 作为任务描述注入。

### 6.5 成本与 token 观测

每次运行结束后，从运行时输出中尽力解析 token 用量与花费（各运行时格式不同，由 YAML 里的解析规则处理；解析不到则记为未知）。汇总维度：按角色、按运行时、按任务类型、按日。前端提供日/周成本折线图与角色成本排行。

这套观测的准确性受限于运行时是否报告用量，做不到精确。它的用途是发现异常趋势（"这个角色的平均 token 消耗翻倍了"），而不是财务核算。

---

## 第七章 Agent CLI 契约

### 7.1 设计意图

`anvil` 是一个极小的 Python CLI，随 Anvil 一起安装，通过环境变量拿到 endpoint、token、当前 task_id 与 agent_slug，向本地 HTTP 接口发请求。Agent 在自己的工具循环里像用任何其他命令一样调用它。

这是"Agent 是驾驶员"这一立场的具体落地。所有状态变更都由 Agent 显式声明，编排器从不猜测。

### 7.2 命令集

```
# 任务
anvil task list [--type T] [--tag T] [--limit N]
anvil task show <id>
anvil task claim <id>                    # 排他，失败返回非零
anvil task unclaim <id> --reason "..."
anvil task heartbeat                     # 续租当前任务
anvil task submit --summary "..." [--branch <name>]
anvil task comment <id> "..."
anvil task mention <id> --to <role> "..."   # @提及角色（角色间协作，不经 human）
anvil task create --title "..." --type T [--parent <id>] \
                  [--assignee <role> | --assignees a,b]  # 子任务显式指派执行者
anvil task block <id> --on <other-id> --reason "..."

# 工作流
anvil step show <workflow-id>
anvil step submit <workflow-id> <step-id> --summary "..."
anvil step plan <workflow-id> <plan-step-id> --file slices.json  # 运行期切片计划
anvil step decide <workflow-id> <decision-step-id> --approve|--reject --feedback "..."
anvil board draft --task <id> --goal "..." --tree-file wf.json --kind workflow

# 记忆
anvil memory append "..."                # 追加到 MEMORY.md
anvil memory search "查询词"             # FTS5 检索自己的 notes + shared
anvil note write <path> --content-file f
anvil lesson propose "..." --tags a,b    # 提议升格到 shared/lessons.md

# 自我
anvil self show                          # 当前 profile（快照）
anvil self edit-instruction --file f     # 可自主，写入 pending 或直接生效
anvil self propose --field tools --file f  # 需审批项走提案

# 提醒
anvil reminder add --cron "0 9 * * 1" --prompt "..."
anvil reminder add --in 2h --prompt "..."
anvil reminder list / remove <id>

# 工作区
anvil ws status                          # 当前 worktree 的 git 状态与 scope
anvil ws diff                            # 本次改动，含越界警告
```

### 7.3 输出约定

所有命令支持 `--json`，默认输出人类可读文本（Agent 读文本反而更省 token，JSON 的括号引号是纯开销）。退出码：`0` 成功，`1` 通用失败，`2` 参数错误，`3` 状态冲突（如认领失败），`4` 无权限（如 scope 越界）。

`3` 与 `1` 分开很重要：认领失败是**预期内的正常结果**，Agent 应当据此转向其他任务，而不是当作错误重试。这一点要在注入的协议说明里写清楚。

### 7.4 注入给 Agent 的协议说明

投影时写入指令的一段固定文本（约 400 token），大意如下：

> 你在 Anvil 环境中运行。当前环境变量 `ANVIL_TASK_ID` 指示你被分配的任务。
> 第一步必须执行 `anvil task claim $ANVIL_TASK_ID` 认领任务（否则无法提交）。
> 再执行 `anvil task show $ANVIL_TASK_ID` 读取完整要求。
> 在当前工作目录（cwd）中完成你的产出（写代码/文档/测试）。
> 需要与其它角色协作或澄清时，执行 `anvil task mention $ANVIL_TASK_ID --to <角色slug> "消息"` @提及对方（角色间直接交互，不经过人类）。
> 开始/关键节点用 `anvil task comment` 同步进展；需要下游/验证角色介入时用 `anvil task mention` 交接。
> 拆分给其他角色的子任务用 `anvil task create ... --assignee <角色>`（团队用 `--assignees a,b`）指定执行者；**不要 claim 你拆给别人的子任务**，已 claim 需先 `anvil task unclaim <id>` 再 @ 对方。
> 工作流步骤完成后用 `anvil step submit`；decision 步骤用 `anvil step decide --approve|--reject`；plan_slices 步骤用 `anvil step plan --file slices.json`。
> 长任务中每隔若干分钟执行 `anvil task heartbeat` 续租，否则任务会被回收。
> 完成后必须执行 `anvil task submit $ANVIL_TASK_ID "完成摘要"` 声明完成，仅仅退出进程不算完成。
> 发现自己不适合这个任务时执行 `anvil task unclaim` 并说明理由，这是正常操作。
> 学到值得长期记住的东西时用 `anvil memory append` 记下；判断对其他角色也有用时用 `anvil lesson propose`。
> 只修改 scope 范围内的文件，越界的提交会被拒绝。

---

## 第八章 记忆与知识库

### 8.1 立场：文件优先

**这一章在设计过程中被大幅简化过，简化的理由值得写进文档。**

最初的方案是重型的：四层知识分类、向量检索、置信度衰减、冲突消解、一个专职"档案员"角色定期整理。看过 raft 的实际做法后我把它砍掉了。raft 的记忆就是工作空间里的普通 Markdown 文件（`MEMORY.md`、`notes/`），Agent 自己维护自己整理，没有向量库、没有分层、没有置信度。它的文档甚至警告用户不要直接在磁盘上改这些文件，要通过对话让 Agent 自己改。

它是对的。理由有三：Markdown 文件人可以直接读和审计，向量库不行；Agent 天然擅长读写和重构 Markdown，让它维护自己的笔记比让它调用一套检索 API 更自然；置信度衰减这类机制的参数无法验证，最后只会变成一堆没人理解的魔法数字。

所以 v1 的记忆系统是：**文件是真相，SQLite FTS5 是索引，向量检索推迟到 v2 且需要证据支持。**

### 8.2 三个层次

| 层 | 位置 | 谁维护 | 上限 |
|---|---|---|---|
| 私有记忆 | `agents/<slug>/MEMORY.md` | 该 Agent 自主 | 建议 300 行，超出时提示整理 |
| 私有笔记 | `agents/<slug>/notes/*.md` | 该 Agent 自主 | 无硬限，FTS 索引 |
| 共享经验 | `repos/<r>/.anvil/shared/lessons.md` | 提案 + 审批 | **80 条硬上限** |

私有层完全自治，Anvil 不干预内容，只做索引和"该整理了"的提醒。共享层是唯一需要把关的地方，因为它会注入给所有角色，一条错误的共享经验会污染整个系统。

### 8.3 升格流程

Agent 执行 `anvil lesson propose "..."` 后：

1. 提案进入待审队列，前端"知识库"页显示。
2. 系统自动做一次 FTS 相似检索，若与现有条目高度重合则并列显示，方便你判断是重复还是补充。
3. 你批准、编辑后批准、或拒绝。
4. 批准后追加到 `lessons.md`，带标签与来源任务 id。
5. 若已达 80 条上限，强制要求先淘汰一条才能加入——界面上按"最后命中时间"排序，最久未被检索命中的排在最前，方便选。

**80 条这个数字是我定的，没有先例。** 逻辑是：注入时按标签筛选后通常命中 5-10 条，全量 80 条大约 3000-4000 token，正好是一个能整体浏览又不至于失控的量级。你实际用起来发现太少就改，改这个上限没有任何技术成本。

### 8.4 检索

FTS5 虚拟表索引所有 `notes/*.md` 与 `lessons.md` 的分段（按二级标题切段）。查询时：

```sql
SELECT path, snippet(mem_fts, 2, '[', ']', '…', 32) AS s, bm25(mem_fts) AS score
  FROM mem_fts
 WHERE mem_fts MATCH :q
   AND (owner = :slug OR owner = 'shared')
 ORDER BY score LIMIT 8;
```

中文分词用 FTS5 的 `unicode61` 加 bigram 兜底，或接 `simple` 分词器扩展。做不到语义检索，只能做关键词——这是明确接受的取舍。注入上下文时的记忆段就靠这个检索填充，查询词由任务标题、标签与 scope 路径拼成。

### 8.5 自动整理

不设专职档案员角色（那是过度设计），改为**每个角色通过 reminder 自己整理**。默认创建角色时附带一条周 reminder：

> 回顾本周完成的任务与 notes，把散落的要点合并进 MEMORY.md，删掉已经过时的内容，把值得共享的用 `anvil lesson propose` 提出来。检查 MEMORY.md 是否超过 300 行，超了就精简。

整理由 Agent 自己在自己的上下文里做，它最清楚哪些内容是重复的。人只需要偶尔翻一眼 `agents/` 那个 Git 仓库的提交历史，看记忆的演化方向是否正常。

---

## 第九章 角色演化

### 9.1 立场：自主编辑 + 人工审批

这一章同样被砍过。最初设计了一整套：教练角色分析历史表现、生成结构化改进提案、灰度期并行对比指标、自动回滚、维护负面样本库。砍掉了。

raft 的实际做法极简：Agent 可以编辑自己的 description，加一条每周 reminder 提醒它检查。就这样。

砍掉的核心理由是：**自动修改提示词会导致缓慢漂移，而退化极难察觉。** 一个角色的表现下降 15%，你根本不会注意到，只会隐约觉得"最近它有点笨"。而当你发现时，已经经过十几轮自动迭代，无法定位是哪一次改坏的。灰度对比听起来严谨，但个人使用场景下样本量根本不够做统计判断——两个任务成功一个失败，你能推出什么？

所以 Anvil 的做法是分权：**低风险字段 Agent 自主改，中风险走审批，高风险永久禁止自动改。**

### 9.2 三档权限

| 档 | 字段 | 机制 |
|---|---|---|
| **自主** | `instruction.md`（managed block 内）、`MEMORY.md`、`notes/`、`reminders` | Agent 直接写，事后可在 Git 历史里看 |
| **审批** | `capabilities` 权重、`tools`（预设与白名单）、`responsibilities`、`claim` 规则 | 写入 `.anvil/pending/`，前端审批后生效 |
| **禁止** | `permissions`、`runtime`（含 model）、`description`、`workspace.repos`、`concurrency` | Agent 无接口可改，只能建议 |

`description` 放进禁止档是因为它是给人看的，Agent 改它没有意义还会造成人机认知不一致。`runtime` 与 `permissions` 涉及成本和安全边界，必须由人决定。

### 9.3 提案格式

```markdown
# 提案：调整工具清单
- 提出时间：2026-09-14T10:22
- 触发：周复盘 reminder
- 字段：tools.allowlist

## 现状
当前预设 backend-default 含 14 个工具，实测 WebSearch 与 BrowserNavigate
在过去 10 个任务中从未使用，占用约 600 token。

## 建议
从白名单移除 WebSearch、BrowserNavigate。预计每轮节省 600 token。

## 依据
run-01H8 至 run-01HR 的日志中无这两个工具的调用记录。

## 风险
若后续出现需要查阅外部文档的任务，需临时恢复或换用其他预设。
```

要求提案必须包含**依据**与**风险**两段。这不是形式主义——它逼 Agent 在提案前真的去看日志，也让你在三十秒内能判断该不该批。空洞的提案（"我觉得改成这样更好"）直接拒。

### 9.4 演化留痕

`agents/` 目录本身是一个本地 Git 仓库，Anvil 在每次角色配置变更、记忆写入、提案批准后自动提交，commit message 标注来源（`agent:backend-dev self-edit instruction` / `human approve proposal #12`）。

这给了你一个非常实用的能力：`git log -p agents/backend-dev/instruction.md` 就能看到这个角色的自我认知是怎么一步步长成现在这样的。如果哪天觉得它变笨了，直接 `git diff` 两周前的版本，通常一眼就能看出是哪次改动引入的问题，`git revert` 即可。

**这比任何自动回滚机制都可靠**，因为判断权在你手上，而判断"是否退化"这件事目前没有任何自动化方法能做得比人好。


# 第十章 · HTTP API

`http://127.0.0.1:7317`，仅监听回环地址，本地 token 轻校验（用于防止本机其他程序误调，不是账号体系）。FastAPI 自动生成 OpenAPI 规格，前端用 `openapi-typescript` 生成类型化客户端——这条一开始就配好，能省掉后续大量前后端对接摩擦。

```
# 运行时
GET    /api/v1/runtimes
POST   /api/v1/runtimes/discover
POST   /api/v1/runtimes/{id}/probe
PUT    /api/v1/runtimes/{id}
GET    /api/v1/runtimes/{id}/capabilities      # 前端据此决定显示哪些配置项

# 角色
GET    /api/v1/roles                    POST   /api/v1/roles
GET    /api/v1/roles/{id}               PUT    /api/v1/roles/{id}
DELETE /api/v1/roles/{id}
GET    /api/v1/roles/{id}/versions
POST   /api/v1/roles/{id}/rollback/{version}
GET    /api/v1/roles/{id}/metrics
GET    /api/v1/roles/{id}/effective-config     # 预览实际投影结果
POST   /api/v1/roles/{id}/message              # 向角色直接发指令（唤醒源之一）
POST   /api/v1/roles/{id}/clone                # 以现有角色为模板新建
GET    /api/v1/roles/{id}/catalog              # 读角色勾选的 skill/MCP（M3）
PUT    /api/v1/roles/{id}/catalog              # 保存角色勾选的 skill/MCP
GET    /api/v1/roles/{id}/profile              # 读角色 system prompt + 职责
PUT    /api/v1/roles/{id}/profile              # 更新角色 system prompt + 职责
POST   /api/v1/roles/{id}/generate             # 调 LLM 自动生成角色画像（工作模式/技能/职责/系统提示）

# 角色私有工作区
GET    /api/v1/roles/{id}/home                 # 目录树
GET    /api/v1/roles/{id}/home/file?path=      # 只读展示（见 §3.1 的理由）
GET    /api/v1/roles/{id}/home/history?path=   # Git 历史

# 工具清单与预算
GET    /api/v1/roles/{id}/tools
PUT    /api/v1/roles/{id}/tools                # 超预算返回 422 并附估算明细
POST   /api/v1/tools/estimate                  # 试算：勾选组合的 token 开销

# 技能
GET    /api/v1/skills                   POST   /api/v1/skills
PUT    /api/v1/skills/{id}              DELETE /api/v1/skills/{id}
GET    /api/v1/skills/{id}/versions
GET    /api/v1/skills/{id}/compat              # 与各运行时的兼容性

# MCP
GET    /api/v1/mcp/servers              POST   /api/v1/mcp/servers
PUT    /api/v1/mcp/servers/{id}         DELETE /api/v1/mcp/servers/{id}
POST   /api/v1/mcp/servers/{id}/test           # 握商探测，刷新工具缓存
GET    /api/v1/mcp/servers/{id}/tools          # 含每个工具的 token 估算

# 工作区
GET    /api/v1/workspaces               POST   /api/v1/workspaces
GET    /api/v1/workspaces/{id}/tree
GET    /api/v1/workspaces/{id}/diff?branch=
POST   /api/v1/workspaces/{id}/merge?branch=   # 手动合并（含越界校验结果）

# 任务板
GET    /api/v1/tasks?state=&role=&tag=&type=
POST   /api/v1/tasks                    GET    /api/v1/tasks/{id}
PATCH  /api/v1/tasks/{id}
POST   /api/v1/tasks/{id}/cancel        POST   /api/v1/tasks/{id}/reopen
POST   /api/v1/tasks/{id}/comment
POST   /api/v1/tasks/{id}/mention         # @提及角色（注入注意事项/上下文，HITL 反向通知）
POST   /api/v1/tasks/{id}/assign          # 绑定参与角色（团队协作，body {roles:[...]}）
POST   /api/v1/tasks/{id}/claim        # anvil task claim（排他，§5.4 事务）
POST   /api/v1/tasks/{id}/unclaim      # anvil task unclaim
POST   /api/v1/tasks/{id}/heartbeat    # anvil task heartbeat（续租）
POST   /api/v1/tasks/{id}/submit       # in_progress→in_review（含 diff 越界校验）
POST   /api/v1/tasks/{id}/review       # {approve|reject}：approve→done；reject→in_progress
POST   /api/v1/tasks/{id}/deps         # 建依赖边（anvil task block）
GET    /api/v1/tasks/{id}/activities    GET    /api/v1/tasks/{id}/bids
GET    /api/v1/tasks/{id}/timeline      # 任务会话时间线（audit + activities 合并，多角色交互流）
GET    /api/v1/tasks/{id}/diff
GET    /api/v1/tasks/{id}/config-snapshot      # 本任务实际使用的配置
GET    /api/v1/board/templates                # 工作流模板库（SOP 预设，如 design-dev-test / dev-test / fix-test）
POST   /api/v1/board/decompose                 # 提交目标 + 模板 → 产出带角色/依赖的 draft 任务树
POST   /api/v1/board/drafts                    # planner 提交 workflow / role_suggestion 草案
POST   /api/v1/board/drafts/approve            # 批量 draft → todo / workflow
POST   /api/v1/board/drafts/reject             # 驳回草案（可触发重规划）

# 工作流
GET    /api/v1/tasks/{id}/workflow
GET    /api/v1/workflows/{wid}
POST   /api/v1/workflows/{wid}/steps/{sid}/submit
POST   /api/v1/workflows/{wid}/steps/{sid}/plan      # 运行期切片计划
POST   /api/v1/workflows/{wid}/steps/{sid}/decide    # decision approve/reject
POST   /api/v1/workflows/{wid}/steps/{sid}/review    # 人工 gate 审核
POST   /api/v1/workflows/{wid}/steps/{sid}/retry

# 运行（run）
GET    /api/v1/runs
GET    /api/v1/runs/{id}
GET    /api/v1/runs/{id}/timeline              # 该 run 的生命周期迁移事件（session 追溯）
GET    /api/v1/runs/{id}/log                   # 该 run 的 stdout/stderr 日志
GET    /api/v1/runs/{id}/snapshot              # 该 run 冻结的 profile 快照
POST   /api/v1/runs/{id}/restart
POST   /api/v1/runs/{id}/session-reset
POST   /api/v1/runs/{id}/full-reset            # 需二次确认，自动备份 home
DELETE /api/v1/runs/{id}                       # 释放进程

# Reminders
GET    /api/v1/reminders?role=          POST   /api/v1/reminders
PUT    /api/v1/reminders/{id}           DELETE /api/v1/reminders/{id}
POST   /api/v1/reminders/{id}/fire-now         # 手动触发，便于调试

# 共享经验
GET    /api/v1/lessons?q=               PUT    /api/v1/lessons/{id}
DELETE /api/v1/lessons/{id}
GET    /api/v1/lessons/pending                 # 待评审的升格提案

# 提案审批
GET    /api/v1/proposals?status=pending
POST   /api/v1/proposals                     # anvil self propose（创建待审提案）
POST   /api/v1/proposals/{id}/approve
POST   /api/v1/proposals/{id}/reject

# 记忆与自我（Agent 写路径，走后端以同步 FTS）
GET    /api/v1/me                            # anvil self show（profile 快照 + 身份 + 并发 + 成本）
POST   /api/v1/me/memory                     # anvil memory append（追加自身 MEMORY.md）
GET    /api/v1/me/memory/search?q=           # anvil memory search（FTS5）
POST   /api/v1/me/notes                      # anvil note write
POST   /api/v1/me/instruction                # anvil self edit-instruction

# 治理
GET    /api/v1/budget                   PUT    /api/v1/budget
GET    /api/v1/costs?scope=&from=&to=
POST   /api/v1/control/freeze-all       POST   /api/v1/control/resume-all
GET    /api/v1/audit?role=&task=&action=&from=&to=

# 实时
GET    /api/v1/events                          # SSE 全局事件流
```

SSE 而非 WebSocket：事件流是单向的，SSE 更简单且自带重连。事件类型与内部事件总线一致，前端按 `topic` 字段分流到各视图。

Agent 侧 CLI 调用的是同一套 API 的子集，附加 `X-Anvil-Session` 头，服务端据此校验该会话是否有权操作目标任务——这保证 Agent 无法认领已被他人持有的任务，也无法修改别人的 home。

---

# 第十一章 · Web 前端

## 11.1 视图清单

**大厅（默认页）**。左栏角色卡片列：头像、名称、状态灯（运行中/空闲/已释放/异常）、当前任务、运行时标签、今日成本。中栏任务板看板，按状态分列，dnd-kit 拖拽改状态。右栏全局实时活动流。顶栏常驻「全场冻结」按钮与今日成本进度条。

状态灯的图例必须写清楚**灰点不代表故障**——Raft 特别强调了这一点，安静的 Agent 通常只是空闲，进程被正常释放了。这个误解会让你反复去重启健康的角色。

**任务详情（任务会话）**。一个任务即一个会话（raft 的频道/线程思路）：顶部任务卡全文与状态，主体是一条按时间排序的**多角色交互时间线**——认领/放弃、评论、提交、评审（通过/驳回）、交接（unclaim→他人 claim），以及运行生命周期事件（投影中/运行中/评审中/已完成/失败/已放弃），每条带头像色块、动作徽章与时间戳；底部可**以 human 身份评论/@提及**（输入 @ 弹出角色联想）参与交互，SSE 实时刷新。会话默认停在最新消息；用户上滚后暂停自动跟随，滚回底部恢复；吸顶栏提供「↑ 最早 / ↓ 最新」快捷跳转。**三向 @提及**：①需自然人决策（评审）时系统自动 `@human` 通知；②自然人 `@角色` 注入注意事项/上下文；③角色间通过 `anvil task mention --to <role>` 直接 `@角色` 交互（角色职责内事务不经人类）。**@ 即拉入**（raft inbox 模型）：@ 的是角色、且该角色为任务参与角色且无活跃 run 时，自动唤醒该角色起来响应。产出 diff（并排视图）、评审意见、配置快照查看、出价记录（开启 `score` 策略时）收在折叠区；diff 视图中越界文件用红色标出并注明"越界，已阻止合并"。

**工作流视图（任务详情「工作流」页签）**。图谱只表达 workflow 的逻辑与架构：静态步骤按依赖分层；运行期切片折叠成一个 `↻ 切片循环` 节点，实时显示总片数/总步数、已完成片数、当前片·阶段·本片进度。点击循环节点展开当前片的**单一闭环流程**（scope → execute → verify → fix → verify2 → decide），当前阶段高亮，可切换切片查看任意一片；列表视图按「循环 → 切片 → 步骤」三级折叠，默认不展开 200+ 个动态步骤。

**角色执行历史（`/agents/{slug}`）**。每个角色的自己执行情况可追溯：大厅角色卡点进该角色的执行历史页，列出其历次 run（session），每条含状态徽章、关联任务、起止时间、耗时。

**运行会话详情（`/runs/{id}`）**。点某次 run 进入：生命周期迁移时间线（投影中→运行中→…→已完成/失败/已放弃）、stdout/stderr 输出日志（`GET /runs/{id}/log`）、冻结的 profile 配置快照、session/worktree 元信息，SSE 实时刷新。

**角色工坊**。这是最核心的编辑页，分五个标签页：

*基本* —— 名称、头像、描述（标注"仅供你阅读，不进入提示词"）、能力标签与权重（雷达图实时预览）。

*指令* —— `instruction.md` 编辑器 + **生成画像**（输入一句描述调 LLM 自动生成工作模式/技能描述/职责/系统提示，回填后可编辑）+ **system prompt 文本域 + 职责列表**（每行一条）。

*工具/技能与 MCP* —— 「技能/MCP」tab：角色绑定运行时后自动拉取该运行时默认的 skill/MCP 目录，复选框勾选加载哪些 skill/MCP 并保存（spawn 时 symlink skill + 写 `.claude/settings.json`）。工具预算与 token 估算沿用原「工具」tab 的勾选矩阵。

*权限* —— 写入范围用目录树可视化，可写目录标绿、禁写标红。shell 白名单、审批项、成本上限。

*运行时* —— 运行时选择与降级链排序、模型与思考强度（不支持的运行时该项不显示）、会话策略、环境变量、自定义参数（附 argv 泄露警告）。

**角色主页**。展示该角色的私有工作区：`MEMORY.md`、`notes/` 目录树、`profile.yaml`。**默认只读**，编辑入口是页面底部的"给该角色发指令"输入框。这个设计是刻意的（§3.1），旁边配一行说明"直接改文件会破坏它的组织方式，请让它自己改"。

**技能库**。技能 CRUD、Markdown 编辑器、前置元数据表单、按角色的挂载矩阵、版本历史 diff、兼容性告警。

**运行时与工具**。运行时列表与健康状态、自动发现按钮、MCP 服务器 CRUD 与连接测试、工具清单浏览（含 token 开销排序，便于你发现哪些工具特别昂贵）。

**Reminders**。列表视图 + 时间轴视图，区分你创建的与 Agent 自建的，支持手动触发调试。

**共享经验**。`shared/lessons.md` 条目列表、检索、待审升格提案、手动增删。

**提案审批**。待审提案列表，每条给出 diff 视图与一键接受/拒绝。这是你与角色演化的唯一交互面（§6.3）。

**审计与成本**。日志多维筛选与全文检索、成本按角色/日期/运行时分解图表、token 消耗趋势。

## 11.2 实时事件消费

```typescript
// hooks/useEventStream.ts
export function useEventStream() {
  const qc = useQueryClient();
  useEffect(() => {
    const es = new EventSource("/api/v1/events");
    es.onmessage = (e) => {
      const ev: AnvilEvent = JSON.parse(e.data);
      switch (ev.topic) {
        case "task":     qc.invalidateQueries({queryKey: ["tasks"]}); break;
        case "session":  qc.invalidateQueries({queryKey: ["sessions"]}); break;
        case "activity": appendActivity(ev.payload); break;   // 追加，不重取
        case "cost":     updateCostBar(ev.payload); break;
        case "proposal": notifyProposal(ev.payload); break;
      }
    };
    return () => es.close();
  }, [qc]);
}
```

活动流用追加而非失效重取——高频事件下重取会打满后端。其余低频事件走 TanStack Query 失效即可。

## 11.3 启动体验

`anvil.exe` 双击后：启动 Uvicorn、注册系统托盘、默认浏览器打开 `http://127.0.0.1:7317`。托盘常驻显示活跃角色数与今日成本，右键菜单含「打开界面」「全场冻结」「退出」。

首次启动引导四步：扫描并确认运行时 → 选择或创建项目工作区 → 从模板导入 2–3 个角色 → 提交第一个目标试跑。引导流程结束时应该已经有一个任务在跑，这是建立信心最有效的方式。

---

# 第十二章 · 实施路线

工作量估算基于单人开发、每周 15–20 小时投入。

**范围注记（owner 2026-09-16 拍板）**：首版 = M1 + M2 + M5（打包，Windows 分发）；**M3 工具治理移 v2**；M4 长程能力按设计建议视实际使用再定。Windows 为第一验收要素。

## M1 · 骨架（2 周）

运行时自动发现与定义加载（先只支持你最常用的一个）、启动器与 Job Object、角色定义与私有工作区、共享仓库与 Git 操作、任务 CRUD 与手动指派、`anvil` CLI 的 `whoami` / `task show` / `task submit`、SSE 事件流、大厅与任务详情两个页面。

**目标：端到端跑通一个手动指派的任务。** 这一步跑通，整个架构的可行性就验证了。

## M2 · 协作与治理（3 周）

认领机制（`first_come`）、租约与心跳、配置快照、八态状态机、依赖解算、评审流程、分支隔离与 diff 越界校验、四层成本闸门、两级并发上限、全场冻结、审计日志、角色工坊页。

**此时系统已实用。** 我建议在这里停下来用两三周，只用不开发，把实际遇到的问题记下来再决定 M3 做什么。这个暂停很重要——下面两个里程碑的优先级应该由你的真实使用体验决定，而不是由这份文档决定。

## M3 · 工具治理（2 周）

MCP 服务器登记与握商探测、skill 与 MCP 投影、工具白名单裁剪、token 估算与预算校验、条件加载与工具组合预设、勾选矩阵 UI、运行时能力三档门控。

这一段的收益完全取决于你实际挂了多少工具。如果你只用两三个 MCP 服务器，直接沿用运行时的全局配置就够了，这一整段可以推迟。

## M4 · 长程能力（2 周）

三档重置、按需唤醒与空闲释放、reminders 调度器与 CLI、`MEMORY.md` 自整理引导模板、`shared/lessons.md` 与 FTS5 检索、升格评审流程、提案审批机制、角色主页与提案页。

## M5 · 打磨（持续）

更多运行时定义、角色模板库扩充、指标看板、目标拆解优化、性能调优、PyInstaller 打包与首启引导。

## 推迟到 v2 的事项

向量检索（`sqlite-vec`）—— 触发条件是共享经验超 80 条且你能指出具体的检索不准实例。

`score` 认领策略 —— 触发条件是你发现角色频繁抢到不适合的任务。

自动灰度验证与回滚 —— 触发条件是手工审批提案成为明显瓶颈。

WSL2 或容器强隔离 —— 触发条件是你需要让 Agent 跑不受信任的代码。

---

# 第十三章 · 风险与我的判断

## 13.1 多 Agent 常常不如单 Agent

Cognition 有篇被广泛引用的文章论证过：并行子 Agent 因上下文割裂容易产出互相冲突的结果，合并成本可能超过收益。我认为这个批评是对的，但它针对的是"把一个任务切碎给多个无记忆 Agent 并行"。

你要做的是有持久身份、有私有记忆、通过任务板显性通信、任务边界较粗的模式，更接近人类团队分工，风险小得多。Raft 与 Multica 两个真实产品的存在也说明这条路走得通。

但仍然建议：**任务粒度宁大勿小**，一个角色能连续做完的不要拆；**全局并发初期设 2**，尝到好处再调高。

## 13.2 没有实时沙箱，这是明确的架构取舍

因为放弃自实现工具循环、完全继承运行时能力，Anvil **无法在运行时内部拦截操作**。防线是三道：提示词告知、运行时配置约束、diff 越界校验。前两道可被突破，第三道保证不该改的文件不进主干。

这对单人本地开发是合适的强度。但你应当清楚：Agent 在执行过程中读到了什么，Anvil 管不住。如果你的项目里有不能被模型看到的东西（真实生产密钥、他人隐私数据），把它们放到工作区之外，并依赖 `permissions.deny` 之外的手段——最可靠的是根本不放在这台机器上。

## 13.3 成本会超出直觉

多个长会话 Agent 并发跑商业 API，烧钱速度比单 Agent 快得多，而且是超线性的——因为返工、互相驳回、重复读同一份代码都在烧钱。Raft 与 Multica 都不管这件事（成本归你的订阅），所以你没有现成参照。

四层预算闸门务必在 M2 就做。另外建议第一个月每天看一次成本分解页，你会发现钱主要花在哪些意想不到的地方。

## 13.4 我改口的两处，值得你知道原因

**知识库从四层结构改为文件优先。** 我最初设计了向量库 + 四层分类 + 置信度衰减 + 冲突消解 + 专职档案员角色。看到 Raft 的实际做法（就是普通 Markdown 文件，Agent 自己维护）之后，我认为我此前是在为一个还不存在的规模问题做设计。你的知识主体在角色自己家里、每次会话必读，绝大部分根本不需要检索。工程量差三十倍，功能差距在前几个月是零。

**角色自迭代从自动改写改为自主编辑 + 人工审批。** 我最初设计了教练角色、结构化提案、灰度期、指标对比、自动回滚、负面样本库。Raft 的做法是一句话：Agent 可以编辑自己的描述，配一个每周 reminder。我现在认为后者对你更合适——让系统自动改自己的提示词，长期会缓慢漂移到你不认可的方向，而且**退化极难察觉，因为每次只差一点点**。手动审批既是安全阀，也是你摸清系统脾气的过程。

## 13.5 范围控制

这份文档的完整实现约 9 周密集投入。如果只想尽快用上，我的建议是：**M1 + M2 做扎实，然后停下来用一段时间。**

M3 的工具治理看你实际挂多少工具决定，M4 的长程能力看你是否真的跑到了"角色记不住东西"的痛点再做。先让"多角色抢任务并各自跑完"这件事真正顺畅，再谈记忆与演化。顺序颠倒的话，你会在一个还没跑对的系统上叠加自省机制，那非常难调试——你分不清是任务分配错了、还是记忆污染了、还是角色定义漂移了。

---

# 附录 · 相对前版的变更清单

| 项目 | v2.0 | v3.0 | 依据 |
|---|---|---|---|
| 驱动方向 | Anvil 解析输出判定进度 | Agent 主动调 `anvil` CLI | Raft 外部 Agent 文档 |
| 输出流解析 | 运行必需 | 可选审计增强 | 同上 |
| 工作区 | 单一共享 Git 仓库 | 私有 home + 共享仓库并存 | Raft 私有隔离 + 你的要求 |
| 会话策略 | 默认常驻 | 默认按需唤醒 | Raft 生命周期文档 |
| 重置粒度 | terminate / resume | restart / session_reset / full_reset | Raft |
| Reminders | 无 | 一等机制，Agent 可自建 | Raft |
| 角色字段 | `mission` 一字段兼两用 | description（不注入）/ instruction（注入）分离 | Multica |
| 配置变更 | 投影哈希比对 | 认领瞬间冻结配置快照 | Multica |
| 并发上限 | 两个孤立字段 | 角色级与全局级取小 | Multica |
| 能力门控 | 静默降级 + UI 提示 | 不支持则不显示该项 | Multica |
| 工具裁剪 | 归在权限机制下 | 独立 `tools.yaml` + token 预算 + 条件加载 | 你的要求 |
| 知识库 | 四层 + 向量 + 冲突消解 + 档案员 | 文件优先 + FTS5 + 升格评审 | Raft |
| 角色演化 | 教练 + 提案 + 灰度 + 自动回滚 | 自主编辑 + 人工审批 | Raft |
| 认领 | 加权打分（默认） | 先到先得（默认），打分可选 | Raft |
| 裸 API 运行时 | 降级承载任务 | 仅内务，不承载任务 | Raft 运行时清单 |

新增数据表：`config_snapshots`、`role_tools`、`reminders`、`lessons`。
删除数据表：`knowledge`、`knowledge_vec`、`knowledge_conflicts`、演化灰度相关表。
删除代码模块：`evolution/`、`runtimes/` 的事件映射与通用适配器兜底。
新增代码模块：`agentcli/`、`reminders/`、`tools/`、`memory/`、`roles/home.py`、`sessions/reset.py`。
