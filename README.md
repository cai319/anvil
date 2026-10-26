# Anvil · 本地多 Agent 编排系统

Anvil 是一个**单机、单用户**的多 Agent 编排服务：把你本机已安装的 CLI Agent（Claude Code、Codex CLI、Gemini CLI 等）抽象成长期存在的**角色**，由 Cindy 规划工作流，角色执行步骤，人类在关键节点审批。数据、记忆和日志都留在本机。

- **后端**：FastAPI + SQLite（WAL）+ 文件系统
- **前端**：React + TypeScript + Vite
- **Agent CLI**：`anvil`，Agent 在自己的工具循环里调用它上报状态
- **地址**：`http://127.0.0.1:7317`（仅回环）

> 架构、数据模型、工作流引擎与实现细节见 [design.md](design.md)。

## 目录

1. [核心特性](#1-核心特性)
2. [快速开始](#2-快速开始)
3. [使用指南](#3-使用指南)
4. [配置](#4-配置)
5. [测试与打包](#5-测试与打包)
6. [卸载与清理](#6-卸载与清理)
7. [故障排查](#7-故障排查)
8. [数据、备份与安全](#8-数据备份与安全)
9. [文档](#9-文档)

## 1. 核心特性

- **运行时发现**：内置 Claude Code / Codex CLI / Gemini CLI 定义，自动发现，也可用 YAML 扩展。
- **角色**：独立私有 home（`instruction.md`、`MEMORY.md`、`notes/`、日志），绑定自己的运行时和技能/MCP。
- **常驻会话**：每个角色一个长期存活的 CLI 进程；唤醒 = 投递消息；空闲 15 分钟退出，下次 `--resume` 恢复。
- **上下文与模型窗口**：自动识别实际模型与窗口；支持 `model_pool` / 窗口下限；角色可自主重置上下文。
- **任务与工作流**：任务板、认领/租约、多角色协作；Cindy 先规划 workflow，人类批准后按依赖执行。
- **切片闭环**：`plan_slices + loop`。Cindy 定义 loop 的角色/阶段；运行期切片只决定粒度与轮数，不能覆盖角色。
- **记忆与知识**：`MEMORY.md` + FTS5 + 四层知识作用域（global / channel / task / role）+
  知识卡模板、审批、版本、过期/复核/冲突检测；角色知识必须经过“成功实践 + 提炼”门控。
- **频道与任务归档**：频道承载任务归属、领域 repo、knowledge repo 与参与角色；频道约定对绑定
  任务是“约束”、对只引用 knowledge repo 的任务是“参考”；未绑定频道的任务进入内置「#无主题」。
  任务可按终态归档，归档任务在所属频道任务页签或「#无主题」中查看。
- **知识库与效果评估**：`/knowledge` 统一浏览频道知识、任务知识、角色知识、共享经验、演化提案
  和总体效果评估；具体知识条目显示单条命中次数/最近命中，评估页展示命中覆盖率、零命中、
  治理待处理与任务知识覆盖率。
- **HITL**：评审、审批、删除、重置等只接受自然人身份，Agent 令牌返回 403。
- **可观测**：SSE 事件、审计、运行日志、Token 统计、全场冻结。

## 2. 快速开始

### 2.1 环境要求

- Python 3.11+
- Node.js 18+（构建前端）
- 至少一个 CLI Agent：`claude` / `codex` / `gemini`

### 2.2 macOS / Linux

```bash
cd <项目根>/dev
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
pip install -e agentcli          # Agent 子进程必须能找到 anvil
anvil --help

cd frontend
npm install
npm run build
cd ..

cd backend
python run_server.py             # 打开 http://127.0.0.1:7317
```

首次启动后，另开一个终端初始化：

```bash
cd <项目根>/dev
source .venv/bin/activate
anvil init --with-roles          # 发现运行时 → 建默认频道 → 导入 developer/tester/reviewer
```

### 2.3 Windows

```powershell
cd C:\path\to\aliceAgent\dev
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
pip install pywin32             # Windows 进程管理必需
pip install -e agentcli

cd frontend
npm install
npm run build
cd ..

cd backend
python run_server.py
```

初始化：

```powershell
cd C:\path\to\aliceAgent\dev
.\.venv\Scripts\Activate.ps1
anvil init --with-roles
```

> Windows 优先使用原生 `claude.exe`，不要用 `claude.cmd`（`cmd.exe` 可能破坏 UTF-8 中文参数）。
> 若运行时显示 `missing`，把运行时目录加入 PATH，重新打开终端后再执行 `anvil init`。

### 2.4 停止

后端终端按 `Ctrl+C`。Anvil 会终止活跃 Agent 子进程，把未完成运行标记为 `suspended`，并保留角色 `session_id`。

## 3. 使用指南

### 3.1 创建角色

在「角色工坊」新建角色：

- **slug**：小写 ASCII 字母/数字/`-`/`_`，2–64 位；会进入目录与分支名。
  `workflow` / `system` / `human` 是系统保留字，不能用作角色 slug。
- **运行时**：选择该角色绑定的运行时；角色执行任务时始终使用自己的运行时。
- **指令**：`instruction.md`；**记忆**：`MEMORY.md` + `notes/`。
- **技能/MCP**：在角色详情中按角色独立勾选，默认全部不选。

### 3.2 创建并派发任务

1. 任务大厅 → **新建任务**；
2. 可选「所属频道」：频道提供任务归属、默认 repo / knowledge repo 与参与角色偏好；留空则进入
   内置「#无主题」。顶层任务可显式覆盖 repo，子任务继承父任务结构标记且不可覆盖；
3. 选择参与成员（可多选；也可以不选，让 Cindy 先建议角色）；
4. 点击 **派发**：
   - **0 角色**：Cindy 评估缺口，必要时提交角色建议；
   - **1 角色**：直接唤醒执行；
   - **多角色**：Cindy 设计 workflow，草案进入收件箱；
5. 人类在收件箱批准草案后，步骤按依赖自动执行；最终由人类评审。

任务大厅默认只显示未归档任务；完成/终止的任务可在任务详情归档，归档后在所属频道任务页签
或「#无主题」查看，也可在详情页取消归档。

### 3.3 工作流与切片

- Cindy 负责 workflow/loop 的**阶段、角色、顺序、决策与返工规则**；
- 大型任务用 `plan_slices + loop`：切片计划步骤只产出“这一片做什么、多少轮”，不写角色；
- 任务绑定的**每个角色**都必须出现在静态步骤或 loop 阶段中；
- 执行角色需要调整 loop 结构时，执行：

  ```bash
  anvil task mention $ANVIL_TASK_ID --to cindy "需要增加密码学证明阶段"
  ```

  Cindy 重新提交草案；新草案待审期间，旧 loop 下的 `step plan` 会被拒绝。

### 3.4 收件箱与人工确认

收件箱聚合：`in_review` 任务、`@human` 提及、待审 workflow/角色建议、gate 步骤、lesson/提案、操作卡片。

所有审批必须由自然人完成；Agent 调用审批端点会被拒绝。

需要人类回复的系统通知（最终评审、决策重做耗尽、角色建议被拒）由 **Cindy、任务
`planner` 字段所指的真实角色或任务 assignee** 发出，而不是 `workflow` 伪角色；任务会话
的 @ 候选始终包含 Cindy（以及 `planner` 字段指向的真实角色），human 可以直接 @ 回去答复。

### 3.5 上下文与模型窗口

- 常驻会话默认复用同一上下文；空闲退出后 `--resume` 恢复。
- 模型窗口会根据运行时实际使用的模型自动识别；如果角色可能从 pro 切到 flash，
  可在 `profile.yaml` 配置模型池/保守下限：

  ```yaml
  runtime:
    model_pool: ["deepseek-flash[1m]", "flash-128k"]
    context_window_floor: 128000
  ```

- 任务切换或上下文较高时，唤醒提示会给出占用与重置建议；角色可自行换新会话：

  ```bash
  anvil self reset-context --task $ANVIL_TASK_ID --reason "任务切换/上下文过高"
  ```

- **单轮硬保护**：一次唤醒可能连续跑几百次工具调用（实测 1 小时 714 次），CLI 的自动压缩
  只在轮次边界生效。Anvil 在 assistant 实测上下文达到窗口的 `ANVIL_CONTEXT_HARD_RESET_RATIO`
  （默认 0.9）时主动终止会话，并自动用 CIC 新会话重投当前 run。主动水位轮换不计入失败
  预算；真正的 API 溢出会按最后成功上下文自适应收紧窗口后重试，超过
  `ANVIL_CONTEXT_RESET_MAX_RETRIES` 次则交给步骤自动恢复队列（不 @human）。因此不会再以
  `prompt is too long` 的形式失败。
- **长期知识（按作用域隔离）**：换会话/重置前，Anvil 会把当前任务的工作流步骤摘要、最近结论与
  交接自动写成 `notes/knowledge/<task-id>/<时间>-<触发>.md`，并写入 FTS 索引。新会话的 CIC 会
  注入本角色 `MEMORY.md` 尾部、按任务检索到的相关记忆、工作流决策、任务共享知识、频道知识
  和最近知识快照；角色也可用 `anvil memory append "关键结论..."` 主动沉淀长期知识。
  `MEMORY.md`、`notes/` 和个人知识按 `role_slug` 隔离；任务知识在同一任务的参与角色间共享；
  频道知识对绑定该频道的任务是约束、对其他引用其 knowledge repo 的任务是参考；跨角色共享
  还可通过人工批准的 lessons（`anvil lesson propose`）。角色不能直接导入他人的 notes。
- 常驻进程已死但 run 仍卡在 `running` 时，调度器在 `ANVIL_RUN_DEAD_GRACE_SECONDS`
  （默认 60 秒）后强制转为 `suspended`，并标记下一次唤醒使用全新 CIC 会话；
  步骤看门狗随后续跑。
- **自动恢复（不需要人类在场）**：进程崩溃、超时、上下文溢出、步骤停滞等技术性失败
  会进入自动恢复队列：重置会话 → 指数退避（默认 2 分钟起，上限 30 分钟）→ 重新拉起
  步骤，默认最多 10 次；耗尽后步骤标记失败并留一条非阻塞评论，不产生需要人类回复的
  `@human`。评审、gate、审批等 HITL 确认点仍必须自然人处理。
- 调度器每 5 秒做一轮健康检查：`created` 重投、`projecting` 卡住重投、死进程回收、
  挂起步骤续跑、失败 run 自动重排队；这些都不依赖人工。
- **等待状态不会被打断**：
  - 等其它角色（run=`suspended`）：5 秒 tick 只续租和做健康检查；对方仍有活跃 run
    或正忙时不唤醒等待方；对方空闲/失联超过 `ANVIL_STEP_NUDGE_SECONDS` 才唤醒等待方
    检查 inbox/重新询问。
  - 等 human（run=`suspended`）：`_waiting_for_human` 命中后不自动重试，human 回复后
    由 mention 端点唤醒。
  - 等模型/工具返回（run=`running`、进程 busy）：5 秒 tick 只做 liveness 检查；
    超过 run 级 timeout（默认 3600 秒）才重置会话并进入自动恢复。
  - 等待期间任务/角色租约会自动续租，不会因为 30 分钟没 heartbeat 被判失联。
- **请求体大小保护**：图片/附件不计 token，但会计入请求体。**角色协议首先要求自律**：
  不把大文件/整页截图/长日志直接塞进对话，优先片段读取与路径引用；会话累计附件达到
  `ANVIL_PAYLOAD_HINT_CHARS`（默认 8MB）时，唤醒提示会要求角色先固化结论、别再带大图。
  Anvil 的硬保护是最后防线：达到 `ANVIL_PAYLOAD_HARD_RESET_CHARS`（默认 24MB）时主动
  换新会话；API 返回 `request too large (max 32mb)` 时也会识别并强制新会话重投。
- 用 `anvil whoami --json` 查看 `context_tokens`、窗口、`model_pool_windows`、
  `context_fit`、`hard_limit_tokens` 等指标。
- **删除 vs 取消**：删除任务会回收该任务的常驻进程/会话；取消（`closed`）保留进程/会话，`reopen` 可继续。

> 窗口解析优先级、阈值公式、模型切换与同族后缀处理等实现细节见
> [design.md §2.7](design.md#27-上下文窗口与模型路由)。

### 3.6 常用 CLI

```bash
# 人类/运维
anvil init --with-roles
anvil task list
anvil task show <task-id>
anvil task create --title "..." --description "..."
anvil task comment <task-id> "..."
anvil task mention <task-id> --to human "请确认"
anvil whoami --json
anvil ws status <repo-name>

# Agent 工作流
anvil task claim $ANVIL_TASK_ID
anvil task heartbeat
anvil inbox check $ANVIL_TASK_ID
anvil inbox commit $ANVIL_TASK_ID --up-to <id>
anvil task comment $ANVIL_TASK_ID "进展"          # 也可用 --message "进展"
anvil task mention $ANVIL_TASK_ID --to <role> "请确认接口"  # 也可用 --message
anvil task submit $ANVIL_TASK_ID "完成摘要"
anvil step submit <workflow-id> <step-id> --summary "步骤完成"
anvil step plan <workflow-id> <plan-step-id> --file slices.json
anvil step decide <workflow-id> <decision-step-id> --approve|--reject --feedback "结论"
anvil board draft --task $ANVIL_TASK_ID --goal "..." --tree-file wf.json --kind workflow
anvil self reset-context --task $ANVIL_TASK_ID --reason "..."
anvil role expand --brief "需要一个安全评审角色"
anvil action prepare --kind task.cancel --task $ANVIL_TASK_ID --reason "..."

# 频道 / 任务 / 知识卡
anvil workspace knowledge list <workspace-id>
anvil workspace knowledge propose <workspace-id> --title "..." --tldr "..." --kind convention --fields '{"rule":"..."}'
anvil workspace knowledge approve <workspace-id> <knowledge-id>
anvil workspace knowledge harvest <workspace-id> <task-id>
anvil task knowledge-list $ANVIL_TASK_ID --include-subtasks
anvil task knowledge-write $ANVIL_TASK_ID --title "..." --tldr "..." --kind task_finding --fields '{"finding":"..."}'
anvil task archive <task-id>
anvil knowledge card-template --kind convention
anvil knowledge card-validate --file card.json --for-approval
```

退出码：`0` 成功；`1` 通用失败；`2` 参数错误；`3` 状态冲突；`4` 无权限。

### 3.7 角色写文件的位置

| 场景 | 工作目录 |
|---|---|
| 任务没有 `repo` | 角色私有 home：`data/agents/<slug>/` |
| 任务配置了 `repo` | 独立 worktree：`data/worktrees/<run_id>/`，分支 `agent/<slug>/<task>` |

运行日志：`data/agents/<slug>/logs/run-<run_id>.log`。

### 3.8 知识库与效果评估

「知识库」页面按作用域提供六个页签：

- **频道知识**：跨频道浏览/筛选，待批准卡可直接批准/拒绝；
- **任务知识**：任务参与角色共享的知识，只读审计；
- **角色知识**：按角色查看最近笔记或搜索 notes，仅作人工审计，不导入他人知识；
- **共享经验**：lessons 的提案、审批与删除；
- **演化提案**：角色自我演化建议的审批；
- **效果评估**：只展示总体指标，不展示单条明细。

单条知识的命中信息直接显示在对应条目行内：

```text
命中 N 次 · 最近命中 YYYY-MM-DD HH:mm
```

- `hit_count` 表示该条知识被检索命中并进入 CIC/one-shot 上下文的次数；
- 列表浏览、页面搜索本身不计命中；
- 它只代表“暴露/使用机会”，不等于任务收益；收益仍需结合返工、评审驳回、周期和 token 成本评估。

效果评估页的总体指标包括：知识利用率（命中覆盖）、累计命中、零命中、过期/复核/冲突、
任务知识覆盖率。任务归档后，终态任务的共享知识仍在任务知识页签中可见（标记为归档）。

## 4. 配置

所有配置通过环境变量读取（完整列表见 `dev/backend/anvil/config.py`）。常用项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANVIL_HOST` / `ANVIL_PORT` | `127.0.0.1` / `7317` | 监听地址/端口 |
| `ANVIL_DATA_DIR` | `<项目根>/data` | 数据根目录 |
| `ANVIL_AUTH_TOKEN` | 空 | 设置后 `X-Anvil-Session` 必须匹配 |
| `ANVIL_DEFAULT_RUNTIME` | `claude-code` | 默认运行时 |
| `ANVIL_DEFAULT_PERMISSION_MODE` | `bypassPermissions` | 角色默认权限模式 |
| `ANVIL_PERSISTENT_RUNTIME` | `auto` | `auto`/`1`/`0`：常驻会话开关 |
| `ANVIL_IDLE_RELEASE_MINUTES` | `15` | 空闲释放时间 |
| `ANVIL_RESET_SESSION_ON_FAILURE` | `1` | 非零退出后丢弃旧 session |
| `ANVIL_CIC_THRESHOLD_TOKENS` | `0` | 冷唤醒阈值；`0`=按模型窗口自动，`>0`=绝对覆盖 |
| `ANVIL_CIC_RESET_RATIO` | `0.8` | 自动 CIC 阈值占模型窗口比例 |
| `ANVIL_CONTEXT_HINT_RATIO` | `0.5` | 上下文提示阈值占模型窗口比例 |
| `ANVIL_CONTEXT_HARD_RESET_RATIO` | `0.9` | 单轮内上下文达到窗口比例时主动换会话；`0`=关闭 |
| `ANVIL_CONTEXT_OUTPUT_RESERVE` | `32000` | 硬阈值预留的响应额度（API 上限为 input+max_tokens） |
| `ANVIL_PAYLOAD_HARD_RESET_CHARS` | `24000000` | 累计图片/附件字符数硬阈值；超过换新会话（0=关闭） |
| `ANVIL_PAYLOAD_HINT_CHARS` | `8000000` | 累计附件达到该值时在唤醒提示里要求角色自律（0=关闭） |
| `ANVIL_CONTEXT_RESET_MAX_RETRIES` | `3` | 同一 run 因上下文溢出自动换会话的最大次数 |
| `ANVIL_RUN_DEAD_GRACE_SECONDS` | `60` | 常驻进程死亡后，卡在 running 的 run 回收宽限期 |
| `ANVIL_RUN_PHASE_STALL_SECONDS` | `60` | run 卡在 `projecting` 时重新投递的宽限期 |
| `ANVIL_AUTO_RECOVER` | `1` | 技术性失败是否自动退避重试（不 @human）；`0`=关闭 |
| `ANVIL_AUTO_RECOVER_MAX_ATTEMPTS` | `10` | 单步骤自动恢复次数上限 |
| `ANVIL_AUTO_RECOVER_BACKOFF_SECONDS` | `120` | 自动恢复首次退避秒数（之后指数增长） |
| `ANVIL_AUTO_RECOVER_MAX_BACKOFF_SECONDS` | `1800` | 自动恢复退避上限（秒） |
| `ANVIL_DEFAULT_CONTEXT_WINDOW` | `200000` | 无法识别模型时的默认窗口 |
| `ANVIL_MODEL_CONTEXT_WINDOWS` | `{}` | 模型 id → 窗口 JSON，如 `{"deepseek-flash[1m]":1000000}` |
| `ANVIL_CONTEXT_WINDOW_FLOOR` | `0` | 全局保守窗口下限 |
| `ANVIL_ONBOARDING` | `1` | 多角色任务是否启用 Cindy 规划 |
| `ANVIL_AUTO_DISPATCH` | `0` | 是否自动派发 todo 任务 |
| `ANVIL_MAX_ROLES` | `20` | 角色总数上限 |
| `ANVIL_RUN_TIMEOUT` | `600` | 运行超时兜底（秒） |
| `ANVIL_BACKUP` | `1` | 是否每天自动备份 SQLite |
| `ANVIL_BACKUP_KEEP_DAYS` | `7` | 数据库备份保留天数 |

## 5. 测试与打包

### 5.1 后端测试

```bash
cd dev
pytest tests/ -q
```

CLI 契约测试需要安装 `anvil` 并指定可执行文件：

```bash
ANVIL_BIN=$(which anvil) pytest tests/cli/test_cli_contract.py -q
```

### 5.2 打包

```bash
# macOS / Linux
cd dev
bash packaging/build.sh

# Windows
cd dev
packaging\build.bat
```

产物在 `dev/dist/anvil-dist/`。完整打包说明见 [docs/operations.md](docs/operations.md)。

## 6. 卸载与清理

### 6.1 停止服务

macOS：

```bash
lsof -nP -iTCP:7317 -sTCP:LISTEN
kill <PID>          # 优先优雅停止；15 秒后仍未退出再 kill -9
```

Windows PowerShell：

```powershell
netstat -ano | findstr :7317
taskkill /PID <PID> /F
```

优雅停止会终止角色常驻进程，并把活跃 run 转为 `suspended`。强制结束后建议检查是否还有
cwd 在 `data/agents/` 下的 `claude/codex/gemini` 进程。

### 6.2 删除源码环境

macOS / Linux：

```bash
cd <项目根>/dev
.venv/bin/pip uninstall -y anvil-cli
rm -rf .venv frontend/node_modules frontend/dist
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

### 6.3 删除数据与运行时定义

> 删除数据目录不可恢复；先备份需要保留的角色 home、repos 和数据库。

```bash
# 源码模式默认数据目录
rm -rf <项目根>/data

# 自定义/覆盖的运行时定义（macOS）
rm -rf ~/.config/Anvil/runtimes
```

```powershell
Remove-Item -Recurse -Force C:\path\to\aliceAgent\data
Remove-Item -Recurse -Force "$env:APPDATA\Anvil\runtimes"
```

如果设置过 `ANVIL_DATA_DIR` / `ANVIL_USER_RUNTIMES_DIR`，删除对应目录。

### 6.4 删除打包产物

```bash
rm -rf <项目根>/dev/dist <项目根>/dev/build
```

```powershell
Remove-Item -Recurse -Force dev\dist, dev\build -ErrorAction SilentlyContinue
```

## 7. 故障排查

| 现象 | 处理 |
|---|---|
| 打开 7317 是 404/空白 | `cd dev/frontend && npm install && npm run build`，重启后端 |
| `anvil init` 提示后端未启动 | 先启动 `python run_server.py` |
| 运行时 `missing` | 检查 CLI 是否在 PATH；Windows 优先原生 `.exe`；重新扫描运行时 |
| 角色一直“等待授权” | 检查 `ANVIL_DEFAULT_PERMISSION_MODE` 与运行日志 |
| 401 | 检查 `ANVIL_AUTH_TOKEN` / `X-Anvil-Session` |
| 403 | 该操作只接受自然人身份；Agent 改用 `mention --to human` 或 `action prepare` |
| Agent 调不到 `anvil` | 后端与 CLI 装在同一个 venv；运行时允许 `Bash(anvil:*)` |
| 上下文/模型窗口不对 | `anvil whoami --json` 查看 `model_id`/窗口；配置 `ANVIL_MODEL_CONTEXT_WINDOWS` 或角色 `model_pool` |
| 想清空角色上下文 | `anvil self reset-context --task <id> --reason "..."`；人类可在运行详情重置 |
| 删除任务后进程还在 | 确认后端已重启到最新版本；删除会回收该任务的常驻进程，取消不会 |

详细排查步骤见 [docs/troubleshooting.md](docs/troubleshooting.md)。

## 8. 数据、备份与安全

数据目录默认 `<项目根>/data/`：SQLite、角色 home、repos、worktrees、日志、备份。

### 8.1 备份

先停止后端，再打包：

```bash
tar -czf anvil-data-$(date +%Y%m%d).tar.gz data/
```

```powershell
Compress-Archive -Path data -DestinationPath anvil-data.zip
```

### 8.2 迁移到另一台机器

1. 停止后端；
2. 复制 `data/` 到新机器（或设置 `ANVIL_DATA_DIR` 指向它）；
3. 在新机器安装相同运行时，启动后端；
4. 在「运行时」页重新扫描，或执行 `anvil init` 重新发现运行时（可执行文件路径会变化）。

### 8.3 重置

```bash
# 停止后端后
mv data data.backup-$(date +%Y%m%d)
anvil init --with-roles
```

Windows PowerShell：

```powershell
Move-Item data data.backup
anvil init --with-roles
```

### 8.4 安全

- Anvil 面向本机单用户：默认只监听 `127.0.0.1`；不要暴露到公网。
- **不要把 `data/` 提交到 Git**：其中包含数据库、token、角色记忆和日志；仓库已通过 `.gitignore` 忽略。
- 非回环访问必须设置 `ANVIL_AUTH_TOKEN`；未设置时仅接受本机默认共享 token。
  设置后，Web 构建需提供 `VITE_ANVIL_TOKEN`，或在浏览器 localStorage 写入
  `anvil-session`；CLI 设置 `ANVIL_TOKEN`。
- 运行时/MCP 的注册、探测与 health，以及角色创建、workspace 合并等操作只接受自然人身份。
- 数据库每天自动备份到 `data/backups/anvil-YYYYMMDD.db`，默认保留 7 天。
- 怀疑 token 泄漏时执行：
  ```bash
  python dev/scripts/revoke_sessions.py --db data/anvil.db --yes
  ```
  然后重启后端。
- 角色审批、删除、重置等 HITL 操作只接受自然人身份。

## 9. 文档

- [design.md](design.md)：架构、数据模型、工作流引擎、上下文与模型策略
- [docs/v1.30-channel-knowledge.md](docs/v1.30-channel-knowledge.md)：频道、知识作用域、知识卡、归档与效果评估方案
- [docs/operations.md](docs/operations.md)：打包、备份/迁移/重置、卸载
- [docs/troubleshooting.md](docs/troubleshooting.md)：详细排查
