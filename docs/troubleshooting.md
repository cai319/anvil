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

灰色 = 空闲，不是故障。常驻会话在空闲超过 `ANVIL_IDLE_RELEASE_MINUTES`（默认 15 分钟）后会退出；每个 `(角色, 任务)` 的 `session_id` 保存在 `role_task_sessions`，下次唤醒按任务用 `--resume` 恢复对应上下文。角色在别的任务上忙碌时，本任务的 @/步骤会排队（任务详情「角色运行时」页签可见）。

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

### Q17. 角色报 `Prompt is too long` / 上下文超限

**现象**

- `data/agents/<slug>/logs/role-session.log` 出现 `Prompt is too long`、
  `context overflow (...) → 将用 CIC 新会话重投当前 run`；
- run 可能短暂停在 `running`，或工作流步骤停住不动。

**原因**

- 一次唤醒是一个很长的工具循环：角色从一条消息开始连续调用工具，实测 1 小时可达 700+
  次 API 请求；CLI 的自动压缩只在轮次边界生效，单轮内上下文会一路涨到模型窗口上限。
- API 的上下文上限按 **input + max_tokens** 计算（Claude Code 默认 `max_tokens=32000`），
  所以“input 还没到 1M”也可能被拒。
- `result` 事件的 `usage` 是**会话累计消耗**（含历史所有 cache read），不是当前上下文大小；
  Anvil 只认 assistant 事件的单次请求 usage。旧版本会用累计值覆盖，导致
  `whoami`/阈值失真（实测被放大到 1.5M–3M）。

**自动处理（当前版本）**

1. assistant 实测上下文达到 `ANVIL_CONTEXT_HARD_RESET_RATIO`（默认 0.9）× 窗口时，
   Anvil 主动终止该会话，写入知识快照，并用 CIC + 当前 run 的提示自动重投；
   **主动水位轮换不计入失败预算，不会 @human**。
2. 轮次结束时上下文已超过 CIC 阈值（默认窗口 80%）：释放进程，下次唤醒新建会话，
   不再 `--resume` 大上下文。
3. 真正的 API 溢出（`api_error`）会按“最后一次成功上下文 × 95%”自适应收紧窗口
   （只调小不调大），然后换新会话重试；同一 run 超过
   `ANVIL_CONTEXT_RESET_MAX_RETRIES`（默认 3）次后，交给步骤自动恢复队列。
4. 三种 API 错误形态（裸文本 / synthetic assistant / result）都会识别；回调失败时，
   调度器在 `ANVIL_RUN_DEAD_GRACE_SECONDS`（默认 60 秒）后把卡在 `running` 的 run
   转为 `suspended`，并标记下一次唤醒使用全新 CIC 会话。

**自动恢复策略（不依赖人类在场）**

参考 Kubernetes restart backoff / Temporal activity retry / CI rerun 的实践：

1. 技术性失败（`platform_timeout`、`spawn_error`、`agent_exit_*`、
   `context_overflow`、步骤停滞）由 `fail_step` 自动重排队；
2. 退避：`ANVIL_AUTO_RECOVER_BACKOFF_SECONDS` 起，逐次 ×2，封顶
   `ANVIL_AUTO_RECOVER_MAX_BACKOFF_SECONDS`；
3. 默认最多 `ANVIL_AUTO_RECOVER_MAX_ATTEMPTS`（10）次；耗尽后步骤标记失败，
   只写一条**非阻塞 comment**，不产生待回复的 `@human`；
4. 重跑时步骤提示会带“上次失败原因”，帮助 agent 避免重复踩坑；
5. 人工确认点（评审、gate、审批、决策僵局）不受影响，仍走 HITL；
   角色最后一条 @ 是 @human 且人类尚未回复时，看门狗不会自动重试/重排队，
   human 回复后由 mention 端点唤醒。

调度器每轮（默认 5 秒）还会自愈：`created` 重投、`projecting` 卡住重投、死进程回收、
挂起步骤续跑、失败 run 重排队。可用 `ANVIL_AUTO_RECOVER=0` 关闭自动重试。

**等待状态与 5 秒调度**

| 状态 | 5 秒 tick 的行为 |
|---|---|
| 等其它角色（run=`suspended`，对方有活跃 run/正忙） | 不唤醒、不回收；只续租与健康检查 |
| 等其它角色，对方空闲/失联 | 超过 `ANVIL_STEP_NUDGE_SECONDS`（默认 600s）才 `resume` 等待方检查 inbox/重新询问；3 次后进入自动恢复 |
| 等 human（最后一条 @ 是 human） | `_waiting_for_human` 命中，不自动重试；human 回复后由 mention 端点唤醒 |
| 等模型/工具返回（run=`running`，进程 busy） | 只做 liveness 检查；超过 run 级 timeout（默认 3600s）才重置会话并自动恢复 |
| 进程已死、run 仍 `running` | 60s 宽限期后转 `suspended` + 下次用全新 CIC 会话 |
| 租约到期（任务/角色 claim） | 有非终态 run 且仍活着 → 自动续租；确认失联才回收 |
| 步骤 lease 到期（长步骤超过 TTL） | run 仍 `running/suspended` → scheduler 每轮续租；`anvil task heartbeat` 也续租当前步骤；run 结束/失联才回收 |

> 调度器不会每 5 秒就动一次等待中的角色；它只处理明确异常（死进程、僵尸 run、
> 卡住的 projecting、到期的退避重试）。等待中的正常会话由事件（mention 回复）唤醒。

**`request too large (max 32mb)`**

- 现象：`Request too large (max 32mb): accumulated images and attachments in the
  conversation pushed the request over the limit. Remove older images or compact the
  conversation.`
- 原因：图片/附件不计 token，但会计入请求体；token 阈值不会触发。长会话反复读截图、
  大文件或图片时会先撞 32MB 请求体上限。
- 角色自律（第一道防线）：角色协议明确要求不把大文件/整页截图/长日志直接塞进对话，
  优先用 `grep/head/tail` 或 `Read offset+limit` 读片段、大产物落盘用路径引用；
  同一图片不要反复读取。累计附件达到 `ANVIL_PAYLOAD_HINT_CHARS`（默认 8MB）时，
  唤醒提示会要求先固化结论并停止携带大图。
- 自动处理（兜底）：Anvil 累计每个会话的图片/附件 base64 字符数，达到
  `ANVIL_PAYLOAD_HARD_RESET_CHARS`（默认 24MB）时保存知识快照并换新 CIC 会话；
  API 返回该错误时也会识别（`reason=payload`），**强制新会话**（不 `--resume`，
  避免旧附件被原样带回）并重投当前 run。
- 调参：降低 `ANVIL_PAYLOAD_HARD_RESET_CHARS` 会更早换会话；`0` 关闭主动保护
  （仍会识别 API 错误）。
- 注意：新会话从 CIC 恢复，旧图片不会保留；需要保留下来的结论应先写入
  `MEMORY.md` 或 notes。

**手动处理**

- Run 详情 → **重置会话（新上下文）**，或 `POST /api/v1/runs/<run-id>/session-reset`
  （只清该 run 所属任务的上下文槽）；
- 角色详情 →「运行时」→ **重置 Agent 会话**（清空该角色所有任务槽）；
- 检查是否还有僵尸 run（`status='running'` 但角色进程已不在）：重启后端会做启动自愈，
  调度器也会按宽限期回收。

**调参**

| 变量 | 作用 |
|---|---|
| `ANVIL_MODEL_CONTEXT_WINDOWS` | 模型 id → 真实窗口；未识别模型务必显式配置 |
| `ANVIL_CONTEXT_HARD_RESET_RATIO` | 单轮硬阈值比例；越小越早换会话 |
| `ANVIL_CONTEXT_OUTPUT_RESERVE` | 预留的 max_tokens；按运行时实际值调整 |
| `ANVIL_CIC_RESET_RATIO` | 轮次结束时换会话的阈值比例 |
| `ANVIL_CONTEXT_RESET_MAX_RETRIES` | 单 run 上下文溢出的自动重投次数上限 |
| `ANVIL_AUTO_RECOVER` | `1` 时技术性失败自动退避重试；`0` 关闭 |
| `ANVIL_AUTO_RECOVER_MAX_ATTEMPTS` | 单步骤自动恢复次数上限（默认 10） |
| `ANVIL_AUTO_RECOVER_BACKOFF_SECONDS` | 首次退避秒数（默认 120，之后指数增长） |
| `ANVIL_AUTO_RECOVER_MAX_BACKOFF_SECONDS` | 退避上限（默认 1800 秒） |
| `ANVIL_RUN_PHASE_STALL_SECONDS` | run 卡在 `projecting` 的重投宽限期 |

**长期知识不受影响**

- 换会话前 Anvil 会自动把任务的工作流步骤摘要、最近结论与交接写入
  `data/agents/<slug>/notes/knowledge/`，并建立 `notes_fts` 索引；
- 新会话的 CIC 注入本角色 `MEMORY.md` 尾部、知识库索引、按任务检索的可复用知识、
  当前任务快照、工作流决策与最近消息；
- 私有记忆按角色隔离：`data/agents/<slug>/MEMORY.md`、`notes/`、`notes/knowledge/` 与
  FTS 检索都只属于该角色；跨角色共享只通过人工批准的 lessons 通道；
- 知识按 scope 隔离：`notes/knowledge/<task>/` 是 task scope，只进入**同一任务**的
  CIC/检索；普通 `notes/` 是 role scope，可跨任务复用。其它任务的快照不会串进来，
  投影到 `CLAUDE.md` 时也不注入任务快照；
- 角色在运行中用 `anvil memory append "关键结论..."` 主动沉淀；每日复盘 reminder 会
  把散落 notes 合并进 `MEMORY.md`，并把可复用知识提升为 role-scoped note。

> 只有通过 `anvil memory append` / `anvil note write` 写入的内容会进入 FTS 索引；
> 直接编辑 `MEMORY.md` 仍会被 CIC 直接读取，但直接编辑 `notes/` 不会自动重建索引。
> 需要检索时用 `anvil knowledge search "<关键词>" --task $ANVIL_TASK_ID`；直接改文件后
> 用 `anvil knowledge reindex` 同步（`--dry-run` 预览，`--prune` 删除已不存在的文件行）。

### Q18. 角色被 @ 了但没被唤醒 / @ 没标黄

**1. 评论里的 @ 不是 mention**

- 只有 `anvil task mention ... --to <role>` 会产生 `kind=mention` 事件并唤醒角色；
  `anvil task comment "... @role ..."` 里的 @ 只是普通文本，不会唤醒。
- 当前版本：
  - `anvil task comment` 检测到 `@` 会打印 warning，提示改用 mention；
  - 前端会把评论正文里命中角色的 `@slug` / `@显示名` 标黄（只是视觉提示）；
  - 需要对方行动时，请用 `anvil task mention`。

**2. @ 的对象没有被识别为任务参与者**

旧版本只检查 `task_assignees`，因此下面这些合法场景会漏唤醒：

- 子任务只有单负责人 `assignee`，`assignees=[]`；
- 角色只出现在 workflow 步骤里，但没有写进 `task_assignees`；
- 想通过 `@角色` 把任务转交给尚未绑定的执行者（协议里的“先 unclaim 再 @ 对方认领”）。

当前唤醒范围已扩大为：`task_assignees` + 单负责人 `assignee` + `task.planner` +
workflow 步骤角色 + **被明确 @ 的现有角色**。

- 普通任务/子任务：起普通 run（任务协议会引导对方 `claim`）；
- 工作流任务：起 `[anvil:reply]` run（只讨论，不覆盖 workflow 角色分配）；
- 回复 run 的协议允许按消息明确指示 `claim` **另一个任务/子任务**，当前任务的
  claim/submit 仍禁止。

**3. 子任务认领仍受“单负责人排他”限制**

如果子任务是 `assignee=picky-pm`、`assignees=[]`，被 @ 的 report-collector 虽然会被
唤醒，但仍无法 claim：单负责人任务已被 picky-pm 独占。解决方式：

- 创建者先 `anvil task unclaim <子任务ID>`，再 @ 目标角色 claim；
- 或者创建时直接用 `anvil task create ... --assignee report-collector`（单人）
  或 `--assignees report-collector,...`（团队）。

**4. 检查是否真的产生了 mention 事件**

```sql
SELECT id, role_slug, kind, payload_json, created_at
FROM activities
WHERE task_id='<task-id>' AND kind='mention'
ORDER BY id DESC LIMIT 10;
```

如果没有 `kind=mention`，说明发送方用的是 comment；如果有 mention 但角色仍未唤醒，
检查目标角色是否存在/启用，以及日志中是否有 `spawn` / `wake`。

### Q19. 出现了 slug 为 `workflow` / `system` 的“角色”

它们**不是 Agent 角色**，是任务时间线与收件箱的系统伪发送者：

| slug | 显示名 | 用途 |
|---|---|---|
| `workflow` | 工作流引擎 | 工作流最终评审、角色建议被拒、旧版步骤失败通知 |
| `system` | 系统 | 上游任务完成等系统通知 |

它们没有 `roles` 行、没有 `data/agents/<slug>/` home、没有 session/run，不会消耗 token。
前端时间线显示“工作流引擎”/“系统”，收件箱 detail 为“系统通知，请处理”。

- 2026-10-23 起，`workflow` / `system` / `human` 是**保留 slug**：不能创建为角色；
  即使历史数据里有同名 role 行，`@workflow` / `@system` 也不会唤醒。
- **需要人类回复的 @human 通知不再由 `workflow` 发出**：最终评审、决策重做耗尽、
  角色建议被拒等通知，发送者取 workflow 创建者（通常 Cindy）、任务 `planner` 字段
  所指的真实角色或任务 assignee，回退 Cindy。这样 human 可以直接 @ 该角色答复。
- 任务会话的 @ 候选始终包含 **Cindy** 和任务 `planner` 字段指向的真实角色
  （即使它们不是任务执行成员）；
  人类 @Cindy 会触发一次规划消费/回复 run，由 Cindy 处理。
- 旧版 `fail_step` 会以 `workflow` sender @human；新版已改为非阻塞 comment。
  历史时间线里的旧消息仍会显示，但不影响运行。
- 如果确实在 `roles` 表里看到了同名真实角色（通常由旧版 agent 扩员/角色建议创建）：

```sql
SELECT slug, display_name, home_path, created_at
FROM roles WHERE slug IN ('workflow','system','human');
```

确认它是误创建后，在角色工坊删除或调用人类身份的角色删除端点；不要直接删 home，
先确认其中是否有需要保留的 notes。

---
