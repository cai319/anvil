# 故障排查

> 本文承接 README 的详细排查步骤；常见问题速查见 README。

## 常见问题

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

### Q13. 「AI 生成并创建 / 生成画像」失败或只返回部分字段

该功能通过 **CLI 运行时**执行，不再直连模型 API。错误信息会显示后端返回的 `detail`，常见原因：

1. **没有可用运行时**：到「运行时」页扫描，确认至少一个 `ready`，并在「默认」列点击 **设为默认**。
2. **运行时未登录/不可用**：先在终端执行 `claude --version` 并做一次最小调用；Codex / Gemini 同理。运行时自身的登录态或 `ANTHROPIC_*` 配置由它自己读取。
3. **角色自己的运行时不可用**：角色详情「生成画像」优先使用角色绑定的运行时；可在角色配置里改回默认运行时。
4. **超时**：默认 120 秒，可用 `ANVIL_UTILITY_TIMEOUT` 调整。
5. **退出码/无输出**：错误信息会带上运行时的 stderr/退出码；据此检查运行时安装或登录。
6. **Windows 下中文参数乱码 / 模型拒绝输出 JSON**：命中了 npm 的 `claude.cmd` wrapper。
   `.cmd` 经 `cmd.exe` 展开 `%*` 时会破坏 UTF-8 中文参数。安装原生
   `claude.exe`（`%USERPROFILE%\.local\bin\claude.exe`）并重新扫描；Anvil 会优先使用 `.exe`。

**只返回工作模式/技能描述，系统提示和职责为空：**

后端会校验四个字段（`working_mode`、`skill_description`、`responsibilities`、`system_prompt`）；
只要有一个缺失或为空就返回 502，`detail` 形如：

```text
运行时返回的画像不完整（12.3s；缺少/为空：system_prompt, responsibilities）。
原始输出片段: {...}
```

排查步骤：

1. 浏览器 DevTools → Network → 点击 generate 请求 → **Response**，查看 `detail` 与耗时（Timing）；
2. 查看后端控制台/日志中的 `role profile generation incomplete ... output=...` 行：
   - 源码模式：`python run_server.py` 的终端输出；
   - 打包模式：从 PowerShell 启动并重定向：`.\anvil-server.exe *> anvil-server.log`；
3. 确认角色绑定的运行时（角色详情 →「运行时」tab），以及该运行时 `claude --version` 是否正常；
4. 如果是自定义运行时（如 `codeagent`），检查 `invocation.args` 是否把 `{{ prompt }}` 正确传给 CLI，
   以及它输出的 JSON 是否包含上述四个字段；
5. 如果耗时接近 120 秒，调大 `ANVIL_UTILITY_TIMEOUT` 或换更快的运行时/模型；
6. 如果原始输出是乱码，确认已使用最新代码（运行时输出按 UTF-8 解码）。

> 生成结果不完整时，前端会**保留原有的系统提示与职责**，不会再用空值覆盖；确认无误后再点「保存」。

### Q14. 从 macOS 迁移到 Windows 后角色执行失败（如 Cindy）

旧数据库里的 `roles.home_path` 是 macOS 绝对路径。后端启动时会自动：

- 把 `home_path` 重算为 `<数据目录>\agents\<slug>`；
- 补齐缺失的 home 目录与基础文件（`instruction.md`、`MEMORY.md`、`notes/`）。

如果仍失败：

1. 确认复制了 `data\agents\<slug>`（只复制 `anvil.db` 会得到空 home）；
2. 查看 run 日志的 `failure_class`（`spawn_error` / `agent_exit_N`）；
3. 确认该角色绑定的运行时在 Windows 上可用（优先原生 `.exe`，见 Q13）。

### Q15. 角色失败后重启仍然失败 / Cindy 反复失败

常驻会话默认会 `--resume` 同一 session；如果失败由上下文污染导致，简单重启进程会复用坏上下文。

- 默认 `ANVIL_RESET_SESSION_ON_FAILURE=1`：进程非零退出后自动清空 `session_id`，下一次唤醒新建上下文；
  - 常见非零退出：CLI 自身 fatal（认证 / 模型 / 网络 / 参数错误、未处理异常、崩溃）、
    进程被 OS 或信号终止（OOM、任务管理器强杀等）；
  - 常驻会话忙时超时（`platform_timeout`）也会重置会话，而不是继续 `--resume`；
  - 正常 `exit 0`（含空闲释放）、后端优雅停机、主动 restart/reset/kill 不会触发自动清理；
  - `spawn_error`（进程未启动）没有退出码，不会自动清理；
- 手动：Run 详情 → **重置会话（新上下文）**；或角色详情 →「运行时」→ **重置 Agent 会话**；
- 只想重启进程但保留上下文：Run 详情 → **重启进程（保留上下文）**；
- 要连记忆一起清：Run 详情 → **全量重置（清记忆）**（先备份 home）；
- 设为 `ANVIL_RESET_SESSION_ON_FAILURE=0` 可关闭失败后的自动清理。

> 重置会话不会删除角色的 `instruction.md`、profile、`MEMORY.md` 或 `notes/`。

### Q16. Windows 上角色 init 后一直 0 输出

常驻会话通过 stdin 投递 stream-json 消息。如果中文 prompt 被系统代码页/CRLF 破坏，CLI 可能
只输出 `init` 事件，然后一直等不到有效输入，表现为“0 输出”。

已修复：常驻进程的 stdin/stdout 显式使用 **UTF-8**，stdin 关闭 CRLF 转换（`newline="\n"`）。
reader 遇到解码/管道异常时会记录完整 traceback 到 `role-session.log` 并终止子进程，
不会静默卡死。所有 `text=True` 子进程（git / npm / health / MCP）也统一 UTF-8。

> **历史乱码不会自动还原**：修复前产生并已写入文件的日志/消息，内容是“先用 GBK 解码
> UTF-8 再按 UTF-8 保存”后的乱码字符串，无法可靠逆转换。请重启后端并开始一个**新 run**
> 验证；旧日志仍会是乱码。

如果仍出现：

1. 确认已更新到最新代码并重启后端；
2. 查看角色日志：
   ```text
   data\agents\<slug>\logs\role-session.log
   ```
   - 应能看到 `[anvil] spawn <claude.exe> ...`；
   - 非 JSON 行是 CLI 的警告/错误（如模型、认证、网络）；
   - 用 UTF-8 查看，避免编辑器按 GBK 打开造成“二次乱码”：
     ```powershell
     Get-Content -Raw -Encoding UTF8 data\agents\<slug>\logs\role-session.log
     ```
3. 查看 run 日志 `data\agents\<slug>\logs\run-<id>.log`：
   - 有 `USER（上下文/提示词）` 和 `[system] init`，但没有 `[assistant]` / `[result]`，
     说明 CLI 收到了启动参数但没有完成模型调用；
4. 在同一个 PowerShell 环境做最小验证：
   ```powershell
   claude --print --permission-mode bypassPermissions "只回复 OK"
   ```
   - 如果这里也失败/为空，检查 Claude Code 登录、模型名、`ANTHROPIC_*` 网关变量；
   - 如果这里正常，重点检查 `role-session.log` 的 spawn 行与非 JSON 警告；
5. 可临时设置 `ANVIL_PERSISTENT_RUNTIME=0` 回退 one-shot 做对比：
   - one-shot 正常、常驻失败 → 问题在 stream-json 管道/CLI 的常驻模式；
   - 两者都失败 → 问题在运行时登录/模型/网络。

> 注意：后端必须从带有 `ANTHROPIC_*` 环境变量的同一个 shell 启动；双击 exe 不会继承你在
> 某个终端里临时设置的环境变量。优先使用原生 `claude.exe`（见 Q13）。

---
