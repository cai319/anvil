# 运维、打包与数据管理

> 本文承接 README 的运维细节：打包、备份/迁移/重置、卸载与清理。

## 打包为可执行程序

打包使用 PyInstaller **单目录**（onedir）模式，产物包含 `anvil` 与 `anvil-server` 两个可执行文件，前端静态资源与内置运行时 YAML 一并打入。

### macOS（以及脚本标称的 Linux）

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

### Windows

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

### 运行打包产物

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

## 数据备份、迁移与重置

### 备份

1. 停止后端（`Ctrl+C`），确保没有 Agent 子进程；
2. 复制整个数据目录（默认 `<项目根>/data/` 或打包版的 `dist/data/`）。

需要保留的核心内容：

- `agents/`：角色身份、记忆、日志、产出；
- `repos/`：共享 Git 仓库；
- `anvil.db` / `anvil-graph.db`：状态与检查点；
- `backups/`：历史重置产生的备份。

> **自动备份**：调度器每天做一次 SQLite 在线备份到
> `data/backups/anvil-YYYYMMDD.db`，默认保留 7 天（`ANVIL_BACKUP` /
> `ANVIL_BACKUP_KEEP_DAYS`）。`data/` 已被 `.gitignore` 忽略，**不要提交到 Git**；
> 若怀疑 token 泄漏，执行 `python dev/scripts/revoke_sessions.py --db data/anvil.db --yes`。

### 迁移到另一台机器

1. 复制源码（或打包产物）与数据目录；
2. 在新机器安装 Python/Node/CLI Agent；
3. 用 `ANVIL_DATA_DIR` 指向复制过来的数据目录（或放到默认位置）；
4. 在新机器上重新执行运行时发现（界面「扫描本地运行时」，或再次运行 `anvil init`），因为可执行文件路径会变化。

> 角色 `home_path` 会在后端启动时自动重算为当前数据目录下的 `agents/<slug>`，不需要手动改数据库；
> 请连同 `agents/` 目录一起复制。若只复制 `anvil.db`，会创建空 home（历史指令/记忆会丢失）。

### 知识库索引与检索

完整命令、参数与错误处理见 [cli-reference.md](cli-reference.md)；每个命令也可用
`anvil <command> -h` 查看动作列表、必填参数与示例。

- 文件是真相，`notes` 表与 `notes_fts` 只是索引；`anvil note write` 会同步写文件与索引。
- 同一 path 只保留一行索引：append 累积文件正文后 upsert，`--replace` 覆盖；路径相对
  `notes/`（写成 `notes/foo.md` 也会规范化为 `foo.md`）。
- `anvil knowledge search "<关键词>" [--task ID] [--kind ...] [--tags ...]` 按角色/任务
  scope 检索；不传 `--task` 时只返回 role-scoped 知识。
- `anvil knowledge index|list [--task ID]` 查看各 kind 计数与最近笔记（`list` 为别名）。
- `anvil knowledge reindex [--dry-run] [--prune]` 扫描角色 `notes/**/*.md` 重建索引；
  直接编辑文件后使用。`--prune` 会删除磁盘上已不存在的文件的索引行。
- 任务快照（`notes/knowledge/<task>/`）只对同一任务可见；普通 `notes/` 是 role scope，
  可跨任务复用。其它任务的快照不会进入本任务的 CIC/检索/投影。

### 重置

- **不要直接删除 `anvil.db` 而保留 `agents/`**：状态与文件会不一致；
- 想要“干净重来”时，停止后端后整体移走/删除数据目录，再重新 `anvil init`；
- 会话上下文重置（每个角色在每个任务上有一个独立上下文槽）：
  - Run 详情 → **重启进程（保留上下文）**：保留所有任务槽的 `session_id`，下次按任务 `--resume`；
  - Run 详情 → **重置会话（新上下文）**：只清该 run 所属任务的槽，其它任务上下文保留；
  - Run 详情 → **全量重置（清记忆）**：所有任务槽与 `MEMORY.md` / `notes` 都清空（先备份 home）；
  - 角色详情 →「运行时」→ **重置 Agent 会话**：无需存在 run，清空该角色所有任务槽（适合 Cindy）。
- 角色忙在任务 A 时，任务 B 的 @/步骤会排队；任务详情「角色运行时」页签可看到
  当前忙碌任务与本任务排队数。

---

## 卸载与清理资源

> **警告**：删除数据目录不可恢复。执行前请先按第 12 节备份需要保留的角色 home、共享仓库
> 与数据库。Anvil 不会自动删除你机器上的 Claude Code / Codex / Gemini 等运行时。

### 停止服务与 Agent 进程

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

### 删除开发环境（源码模式）

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

### 删除运行时数据

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

### 删除用户运行时定义

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

### 删除打包产物

```bash
rm -rf <项目根>/dev/dist <项目根>/dev/build
```

```powershell
Remove-Item -Recurse -Force C:\path\to\aliceAgent\dev\dist, C:\path\to\aliceAgent\dev\build
```

- 如果曾把 `anvil-dist` 加入 PATH，请从用户环境变量中移除该目录；
- 如果创建过桌面快捷方式、计划任务或系统服务，也一并删除。

### 清理环境变量与临时文件

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
rm -f "${TMPDIR:-/tmp}"/anvil-*.log      # macOS / Linux
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

### 彻底删除源码目录

```bash
rm -rf <项目根>
```

```powershell
Remove-Item -Recurse -Force C:\path\to\aliceAgent
```

> 这会同时删除源码、数据目录和所有未备份内容；执行前请确认备份已完成。

### 关于 Claude Code 等运行时

Anvil 只是调用本机已安装的 CLI Agent，卸载 Anvil **不需要**卸载它们。如果确实要移除，
请按各工具自己的文档操作（例如 Claude Code 的 npm 安装可用
`npm uninstall -g @anthropic-ai/claude-code`；原生安装按其官方卸载说明）。

---
