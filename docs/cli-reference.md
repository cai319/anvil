# Anvil CLI 参考

面向人和 Agent 的命令参考。**不要凭直觉猜命令**：先运行 `anvil <command> -h`，
每个命令的帮助里都有动作列表、必填参数与示例。参数错误会返回 `error:` 原因和
`hint:` 下一步提示；状态冲突（409）先查状态再重试。

- 入口：`anvil`
- 后端默认：`http://127.0.0.1:7317`
- 版本：`anvil --version`（不连接后端）
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
| `anvil --version` | 查看 CLI 版本 |
| `anvil task ...` | 任务：列表/创建/认领/评论/提交/归档/任务知识 |
| `anvil step ...` | 工作流步骤：show/plan/submit/decide/escalate/packet |
| `anvil finding ...` | 问题板：安全 finding 的填写/提交/完成（reviewer） |
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
                 [--plan-first] [--planner <role>] [--issue-board]
anvil task assign <id> --assignees a,b
anvil task reviewers <id> --assignees auditor   # planner/onboarding/human
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
- `--issue-board` 只能由 human 在顶层任务启用；任务会强制走 Cindy 计划先行。
  Cindy 用 `task reviewers <id> --assignees <role>` 指定审核者（必须是已存在且被
  workflow 覆盖的角色），之后 reviewer 用 `anvil finding ...` 填写问题板。
- **已有 Roster 时 `assign` 只追加新角色**：首次调用会创建/更新角色追加提案并
  @human（返回 202 / `pending_review`）；human 反馈或批准后重新执行同一命令即可。
  批准只授权 planner，角色仍由 planner 创建/绑定，不会替换已有 assignees。
- `archive` 只接受 `done/closed` 任务；归档任务仍在频道/`#无主题` 中可见。
- `cancel` → `reopen` 会接回最新执行：workflow 仍 `running` → 任务回到 `in_progress`
  并立即唤醒挂起/空闲的步骤 run，**不重新规划**；workflow 已 `done` → `in_review`；
  无 workflow 或 workflow `terminated/failed` → `todo`，再决定是否重新派发/规划。
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
- `anvil step show <wf>` 会在每个 `pending` 步骤下打印未激活原因：
  `dependency`、`retry_backoff_until`、`role_busy_step`、`active_run`、
  `lease_held`、`governance`（冻结/预算/并发）；`ready` 表示下一轮调度即可派发。
- `anvil step show <wf>` 还会显示待审批的增量修订（`pending_revision`）及影响摘要。
- `plan_slices` 用 `step plan`，`decision` 用 `step decide`，普通步骤用 `step submit`。
- 切片只描述范围/粒度/轮次，**不得指定角色或 loop 阶段**；角色由 Cindy 的 loop 模板定义。
- 不是工作流任务时才用 `anvil task submit`。

## 5.1 工作流增量修订：`anvil workflow revise`

```bash
anvil workflow revise <wf> --file revision.json --preview   # 仅分析影响
anvil workflow revise <wf> --file revision.json             # 创建修订草案，@human
```

`revision.json` 顶层可写 `rationale` 与 `intent`：

```json
{
  "rationale": "切片角色反馈：认证模块还需补一轮",
  "intent": {
    "add_slices": [
      {"id": "auth2", "title": "认证补充", "scope_paths": ["src/auth/**"],
       "execute_steps": [{"id": "static", "title": "补充审计"}]}
    ],
    "remove_slices": ["pay2"],
    "update_slices": [{"id": "pay", "patch": {"title": "...", "acceptance": ["..."]}}],
    "patch_steps": [{"step_id": "F1", "fields": {"instructions": "..."}}]
  }
}
```

规则：

- 只有 task planner / Cindy 与 human 能调用；执行角色只能 `task mention --to cindy` 反馈。
- 后端重新计算影响，**不要自行判断 safe**：
  `safe_loop`/`safe_future` → 提交修订草案，human 批准后原地应用、不重启；
  `requires_restart` → 不要提交修订草案，改为完整 workflow 草案 + @human 说明受影响步骤；
  `stale`/`invalid` → 重新 `step show` 后再判断。
- v1 只能改未来（pending）切片和 pending 静态步骤；改 loop 阶段/角色、增删静态步骤、
  重排切片、修改 running/submitted/approved 步骤都会 `requires_restart`。
- 同一 workflow 同时只允许一个待审修订；有待审完整 workflow 草案时也会 409。
- 详细设计见 `docs/workflow-revision.md`。

## 5.2 问题板：`anvil finding`

任务启用问题板（`anvil task create --issue-board`）后，由任务 reviewer 在 agent 会话中
用以下命令填写安全 finding；human 在 Web 任务详情的「问题板」页签逐条确认真实/误报。

```bash
anvil finding template                                  # 输出 finding JSON 模板 + 枚举
anvil finding add --file finding.json [--task <id>] [--submit]
anvil finding update <fid> --file finding.json [--task <id>]
anvil finding submit <fid> [--task <id>]
anvil finding list [--task <id>] [--status ...] [--severity ...] [--json]
anvil finding show <fid> [--task <id>] [--json]
anvil finding complete --summary "..." [--task <id>]
anvil finding delete <fid> [--task <id>]
```

规则：

- `--task` 默认取 `ANVIL_TASK_ID`；只有 `task_reviewers` 能写入，human 也不能代填。
- `add` 默认保存草稿；`submit` 会校验全部必填字段（风险类型/严重等级/模块/函数/文件与行号/
  前置条件/推理链/风险描述/攻击路径/触发点/PoC 状态/技术证明/修复优先级/修复建议/置信度）。
- `poc_status=provided` 时 `poc` 填可复现证明；`not_verified` / `not_applicable` 时
  `poc` 必须写明豁免原因。其余 finding 字段一律必填。
- 所有条目提交后执行 `finding complete --summary`（即使没有问题也要写明范围与结论），
  任务才会进入 human 最终评审；human 逐条确认前任务 `review` 会被 409 拒绝。
- human 在页面「要求重新评估」后，reviewer 会收到 `[anvil:findings]` run：
  用 `finding list/show` 读取意见，`finding update` 补证后 `finding submit`，再 `finding complete`。
- 设计细节见 `docs/issue-board.md`。

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
- Agent 调用 `knowledge reindex --self` 同样受实践门控：直接写入 `notes/` 的无证据内容
  只会落 draft；human 通过 `--all`（`/knowledge/reindex`）复核后才会激活为可检索知识。
- 已 active 的笔记不能被无证据写入覆盖（返回 422）：补充 `--practice-task`/`--outcome`
  等证据，或把未验证内容写到新的草稿路径。
- 参考其他角色知识可以，写入自己的知识库必须经过自己的实践。

## 8. 角色：`anvil role`

```bash
anvil role list [--json]        # 查找系统已有角色（slug/名称/状态/画像）
anvil role propose --task <id> --file roles.json [--goal "..."] [--json]
anvil role expand --brief "需要一个安全评审角色" --slug <已批准 slug> [--task <id>] [--json]
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
- 批准后再 `role expand --slug <已批准 slug>` 创建角色（继承创建者 runtime/permission_mode；
  新角色为普通 member，每日配额有限），然后用 `task assign` 加入任务。只能创建提案中
  已批准的 slug；多角色提案必须用 `--slug` 指定。
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
`approve` 仅 human；harvest 只接受 `done/closed` 任务。Agent 读取频道知识/成员/
资产时，必须在该频道有参与任务，否则 403；human 不受限。

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
