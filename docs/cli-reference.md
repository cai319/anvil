# Anvil CLI 参考

面向人和 Agent 的命令参考。**不要凭直觉猜命令**：先运行 `anvil <command> -h`，
每个命令的帮助里都有动作列表、必填参数与示例。参数错误会返回 `error:` 原因和
`hint:` 下一步提示；状态冲突（409）先查状态再重试。

- 入口：`anvil`
- 后端默认：`http://127.0.0.1:7317`
- 所有命令支持 `-h` / `--help`
- 文档与实现不一致时，以 `anvil <command> -h` 为准

## 1. 身份与环境

| 环境变量 | 作用 |
|---|---|
| `ANVIL_ENDPOINT` | 后端地址，默认 `http://127.0.0.1:7317` |
| `ANVIL_TOKEN` | 会话 token；Agent 使用运行时注入的 run token |
| `ANVIL_AGENT_SLUG` | 角色 slug；缺失时 CLI 以 human 身份调用 |
| `ANVIL_TASK_ID` | 当前任务 id；多数任务/步骤命令的默认 target |
| `ANVIL_RUN_ID` | 当前 run id（由运行时注入，用于 stale-run 围栏） |

规则：

- 身份由 token 决定，不能用 `--role`/请求头冒充其他角色。
- 删除、审批、重置等人类操作需要 human token；Agent 应改用 `mention --to human`
  或 `action prepare`。
- 共享 token 只代表 human，不能冒充 Agent 角色。

### 1.1 文件参数与临时目录

需要 `--file` / `--content-file` / `--evidence-file` / `--tree-file` 的命令，
优先使用**当前工作目录的相对路径**（如 `plan.json`、`roles.json`），或绝对路径。

- macOS / Linux：可用 `$TMPDIR` 或 `/tmp`（如 `/tmp/plan.json`）。
- Windows：用 `%TEMP%`（cmd）或 `$env:TEMP`（PowerShell），例如
  `%TEMP%\plan.json`、`$env:TEMP\plan.json`；**Windows 没有 `/tmp`**。
- 不要写死 `/tmp`；跨平台脚本用 Python `tempfile.gettempdir()` 获取临时目录。
- Agent 在自己的 cwd 写文件即可，不必强制使用系统临时目录。
- Shell 语法：示例中的 `$ANVIL_TASK_ID` 在 PowerShell 写作 `$env:ANVIL_TASK_ID`，
  cmd 写作 `%ANVIL_TASK_ID%`；其余命令参数相同。

## 2. 退出码

| 码 | 含义 | 典型处理 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 通用失败 | 看 `error:`，检查后端/网络 |
| 2 | 参数错误 | 按 `hint:` 修正，运行 `-h` |
| 3 | 状态冲突 | 先 `task show` / `step show` 查状态，不要盲目重试 |
| 4 | 无权限 | 换正确角色 token，或升级给 human |

## 3. 命令总览

| 命令 | 用途 |
|---|---|
| `anvil task ...` | 任务：列表/创建/认领/评论/提交/归档/任务知识 |
| `anvil step ...` | 工作流步骤：show/plan/submit/decide/escalate/packet |
| `anvil knowledge ...` | 角色知识：检索/索引/仓库/知识卡 |
| `anvil note write` | 写入角色笔记（实践门控） |
| `anvil role ...` | 角色：list/expand |
| `anvil workspace ...` | 频道与频道知识卡 |
| `anvil lesson propose` | 提议共享经验（human 审批） |
| `anvil reminder ...` | 定时提醒 |
| `anvil self ...` / `anvil whoami` | 当前身份、会话、自我维护 |
| `anvil inbox ...` | Agent 收件箱（拉模式） |
| `anvil lease ...` | 执行租约与路径预留 |
| `anvil board ...` | 工作流草案（planner） |
| `anvil action ...` | 危险操作卡片（人类执行） |

## 4. 任务：`anvil task`

```bash
anvil task list [--type T] [--tag T] [--parent <id>] [--json]
anvil task show <id> [--json]
anvil task create --title "..." [--description "..."] [--assignee <role>]
                 [--assignees a,b] [--parent <id>] [--workspace <id>]
                 [--repo <name>] [--base-branch <branch>]
                 [--knowledge-repos a,b] [--tags a,b] [--type T]
                 [--plan-first] [--planner <role>]
anvil task assign <id> --assignees a,b
anvil task claim <id> [--paths src/a,src/b]
anvil task unclaim <id>
anvil task heartbeat <id>
anvil task submit <id> --summary "完成摘要"
anvil task comment <id> "正文"                 # 也可 --message
anvil task mention <id> --to <role|human> "正文"  # 也可 --message
anvil task block <id> --on <task-id>
anvil task cancel <id>
anvil task reopen <id>
anvil task delete <id> [--force]
anvil task archive <id>
anvil task unarchive <id>
anvil task knowledge-list <id> [--include-subtasks]
anvil task knowledge-write <id> --title "..." --tldr "..." --kind task_finding \
  [--fields '{"finding":"..."}'] [--tags a,b]
anvil task knowledge-delete <id> --id <knowledge-id>   # human
```

规则：

- 位置参数 `action` 默认 `list`；`target` 通常省略，默认取 `ANVIL_TASK_ID`。
- `comment` 只写时间线，不唤醒；`mention` 才唤醒目标角色。
- **工作流步骤不要调用 `task claim` / `task submit`**；用 `anvil step`。
- 子任务用 `--parent` + `--assignee`/`--assignees` 显式指派；创建者不要先 claim。
- **已有 Roster 时 `assign` 只追加新角色**：首次调用会创建/更新角色追加提案并
  @human（返回 202 / `pending_review`）；human 反馈或批准后重新执行同一命令即可。
  批准只授权 planner，角色仍由 planner 创建/绑定，不会替换已有 assignees。
- `archive` 只接受 `done/closed` 任务；归档任务仍在频道/`#无主题` 中可见。
- `knowledge-write` 的 `--kind` 例如 `task_finding`；`--fields` 必须是 JSON 对象。

## 5. 工作流步骤：`anvil step`

```bash
anvil step show <workflow-id> [--json]
anvil step plan <wf> <plan-step> --file plan.json
anvil step submit <wf> <step> --summary "..." \
  [--evidence-file evidence.json] [--changed-paths src/a,src/b]
anvil step decide <wf> <decision-step> --approve --feedback "通过"
anvil step decide <wf> <decision-step> --reject --feedback "..." \
  [--rework-targets S3,S4] [--reason ...]
anvil step escalate <wf> <decision-step> --reason "..." \
  [--triggers risk,compliance] [--proposed-decision approve|reject]
anvil step packet <wf> <step> [--json]
```

推荐顺序：`step show` → `step plan`（仅 plan_slices）→ `step submit` →
`step decide`（仅 decision）→ `step escalate`（证据/风险需要人工）。

`plan.json` 最小结构：

```json
{
  "rationale": "按模块切片",
  "slices": [
    {
      "id": "auth",
      "title": "认证模块",
      "scope_paths": ["src/auth/**"],
      "objective": "梳理认证链路并找出可复现问题",
      "instructions": "...",
      "acceptance": ["..."],
      "execute_steps": [
        {"id": "static", "title": "静态审计", "instructions": "..."}
      ]
    }
  ]
}
```

规则：

- 只对 `running` 步骤操作；返回 409 时先 `anvil step show <wf>`，不要反复重试。
- 步骤 lease 在 run 存活期间由 scheduler 自动续租；长步骤仍应定期
  `anvil task heartbeat`（它同时续租任务与当前步骤 lease）。
- `plan_slices` 用 `step plan`，`decision` 用 `step decide`，普通步骤用 `step submit`。
- 切片只描述范围/粒度/轮次，**不得指定角色或 loop 阶段**；角色由 Cindy 的 loop 模板定义。
- 不是工作流任务时才用 `anvil task submit`。

## 6. 知识：`anvil knowledge`

```bash
anvil knowledge search "<关键词>" [--task <id>] [--scope auto|role|task]
                       [--kind ...] [--tags ...] [--repo ...] [--limit 8]
anvil knowledge show --id <note-id>
anvil knowledge index [--task <id>]          # 别名 list
anvil knowledge list
anvil knowledge reindex [--dry-run] [--prune] [--self | --all]
anvil knowledge repo-list
anvil knowledge repo-show --repo <name>
anvil knowledge repo-suggest "任务标题"
anvil knowledge card-template --kind <kind>
anvil knowledge card-validate --file card.json [--for-approval]
```

规则：

- 页面浏览/搜索不计命中；只有进入 CIC / one-shot 上下文才记 `hit_count`。
- 写入角色知识用 `anvil note write`；不要直接导入其他角色的 notes。
- 知识卡模板：`convention|decision|workflow|role_requirement|insight` 等，
  以 `anvil knowledge card-template -h` 为准。

## 7. 角色笔记：`anvil note write`

```bash
anvil note write notes/<path>.md --content-file <file> \
  --kind module_knowledge|vulnerability_pattern|failure_scenario|task_snapshot|decision|note \
  --scope role|task [--task <id>] [--tags a,b] [--repo <name>] [--module <name>] \
  [--practice-task <id>] [--practice-run <id>] [--outcome "..."] \
  [--source-knowledge id1,id2] [--replace] [--confidence 0.8]
```

规则：

- 文件是真相层；写入会同步文件与索引。
- 角色知识必须有成功实践 + 提炼证据；无证据只能写 draft，不进入检索/CIC。
- 参考其他角色知识可以，写入自己的知识库必须经过自己的实践。

## 8. 角色：`anvil role`

```bash
anvil role list [--json]        # 查找系统已有角色（slug/名称/状态/画像）
anvil role propose --task <id> --file roles.json [--goal "..."] [--json]
anvil role expand --brief "需要一个安全评审角色" [--task <id>] [--json]
```

`roles.json`：

```json
{
  "rationale": "缺少安全审计能力",
  "suggested_roles": [
    {
      "slug": "security-auditor",
      "display_name": "安全审计员",
      "description": "负责代码安全审计",
      "responsibilities": ["审计代码"],
      "capabilities": {"security-audit": 0.9}
    }
  ]
}
```

规则：

- 先 `role list` 确认已有角色，避免重复创建。
- **新角色先提案**：`role propose` 创建 `role_suggestion` 草案并 @human；
  human 可以反馈/驳回，直到一致后批准。
- 批准后再 `role expand` 创建角色（继承创建者 runtime/permission_mode；
  新角色为普通 member，每日配额有限），然后用 `task assign` 加入任务。
- 任务已有 Roster 时，`task assign` 只追加新角色；未获批准会自动生成追加提案，
  human 反馈/批准后重试，不会覆盖已有 assignees。

## 9. 频道与频道知识：`anvil workspace`

```bash
anvil workspace list
anvil workspace create <name> [--repo <name>] [--knowledge-repo <name>]
anvil workspace knowledge list <ws-id> [--status ...] [--kind ...]
anvil workspace knowledge propose <ws-id> --title "..." --tldr "..." \
  --kind convention|decision|workflow|role_requirement|insight \
  [--authority normative|reference] [--fields '{"...":"..."}']
anvil workspace knowledge approve <ws-id> <kid>    # human
anvil workspace knowledge harvest <ws-id> <task-id>
anvil workspace knowledge review <ws-id>
```

规则：频道是任务归属 + 知识作用域，不是聊天室。Agent 写入为 `proposed`；
`approve` 仅 human；harvest 只接受 `done/closed` 任务。

## 10. 其他命令

```bash
anvil self show | anvil whoami
anvil self reset-context --task <id> [--reason "..."]
anvil self edit-instruction --file instruction.md
anvil self propose --field <field> --file proposal.json

anvil lesson propose "..." [--tags a,b] [--repo shared]

anvil reminder add --in 30m --prompt "检查构建结果"   # 或 --cron / --when
anvil reminder list
anvil reminder remove <id>
anvil reminder fire-now <id>

anvil inbox check [--task <id>] [--limit 30]
anvil inbox commit --up-to <max-message-id> [--task <id>]

anvil lease list <task-id>
anvil lease reserve <path>... [--task <id>] [--ttl 600]
anvil lease release <path>... [--task <id>]

anvil board draft --task <id> --goal "..." --tree-file wf.json --kind workflow
anvil board drafts [--task <id>]

anvil action prepare --kind task.cancel --task <id> --reason "..."
anvil action list
```

## 11. 常见错误与处理

| 错误 | 含义 | 下一步 |
|---|---|---|
| `No such command` / `No such option` | 命令或参数猜错 | 按 `hint:` 运行 `anvil <command> -h` |
| `task <action> 需要任务 id` | 缺 target | 传 `<id>` 或设置 `ANVIL_TASK_ID` |
| `step ... is pending, expected running` | 步骤未在运行态 | `anvil step show <wf>`；等待调度重派，不要 task claim |
| `stale_run` | 旧 run 的 token 已失效 | 等新 run 唤醒；不要用旧上下文重试 |
| `lease_expired` / `lease_fenced` | 步骤租约失效 | 等待自动恢复；先 `step show` 确认 |
| `step ... already expanded` | 切片计划已应用 | 查看现有切片，不要重复 plan |
| `409` 其他 | 状态冲突 | 先 show/查询当前状态，再决定是否重试 |
| `403 this operation requires a human` | 人类专属操作 | 用 `mention --to human` 或 `action prepare` |

## 12. 最小自检

```bash
anvil -h                    # 命令总览与规则
anvil task -h               # 任务动作与示例
anvil step -h               # 工作流步骤顺序
anvil knowledge -h          # 知识动作
anvil role list             # 系统现有角色
anvil whoami                # 当前身份/任务/会话
```
