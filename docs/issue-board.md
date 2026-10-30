# 问题板（Issue Board）

问题板是任务级可选能力：新建任务时勾选「启用问题板」，该任务详情增加一个「问题板」页签，
集中收集本任务排查出的**安全问题**。审核者通过 CLI 按固定字段填写 finding；自然人在页面上
逐条确认「真实问题 / 误报」，全部确认且问题板完成后任务才能最终通过。

未启用问题板的任务行为完全不变。

## 1. 角色与权限

| 动作 | 谁可以 | 入口 |
|---|---|---|
| 启用问题板 | 人类（创建任务 / PATCH 任务） | Web 新建任务、`POST/PATCH /tasks` |
| 指定 reviewer | planner/Cindy、onboarding 助理、human | `POST /tasks/{id}/reviewers`、`anvil task reviewers` |
| 创建/编辑/提交 finding | `task_reviewers`（Agent，含其 run token） | `anvil finding add/update/submit` |
| 标记问题板完成 | `task_reviewers` | `anvil finding complete --summary` |
| 确认/误报/要求重评/重新打开 | 仅人类（`require_human`） | Web 问题板抽屉 |
| 最终任务通过 | 人类，且问题板 completed + 所有 finding 已决策 | Web 评审横幅、`POST /tasks/{id}/review` |

- Web 页面**不代写** finding：填写是审核者的职责，页面只做展示与人工决策。
- Agent 创建子任务时不能启用问题板；启用后任务强制 `plan_first=True`，由 Cindy 规划。
- reviewer 必须是已存在、启用、非 onboarding 角色，且**每个** reviewer 都出现在当前
  workflow 的角色集合中；否则 workflow 草案创建/批准会被 409 拒绝。新增 reviewer 时若已有
  running workflow 且该角色未被覆盖，同样 409。

## 2. Finding 字段

提交时全部必填；草稿可以只填部分。

| key | 中文 | 约束 |
|---|---|---|
| `title` | 问题标题 | ≤200 |
| `risk_type` | 风险类型 | injection / authn_authz / info_disclosure / ssrf / path_traversal / deserialization / race_condition / memory_safety / logic_flaw / crypto / supply_chain / dos / config / other |
| `severity` | 严重等级 | critical / high / medium / low / info |
| `module` | 问题所在模块 | ≤500 |
| `function_name` | 问题函数 | ≤500 |
| `file_path` | 文件 | ≤1000 |
| `line_numbers` | 行号 | 字符串，如 `123` / `123-145` |
| `preconditions` | 触发前置条件 | 长文本 |
| `reasoning_chain` | 推理链（CoT） | 长文本 |
| `risk_description` | 问题风险描述 | 长文本 |
| `attack_path` | 攻击路径（数据流分析） | 入口 / source / 传播 / sink 点 |
| `trigger_point` | 触发点 | 长文本 |
| `poc_status` | PoC 状态 | provided / not_verified / not_applicable |
| `poc` | 技术证明 / 豁免原因 | provided 时填 PoC；否则填豁免原因；均必填 |
| `fix_priority` | 修复优先级 | P0 / P1 / P2 / P3 |
| `fix_suggestion` | 修复建议 | 长文本 |
| `confidence` | 问题最终置信度 | 0–100 整数 |

后端字段/枚举的单一来源是 `services/findings.py` 的 `FIELD_SPECS`，API 与前端通过
`GET /tasks/{id}/findings/schema` 读取；CLI `anvil finding template` 提供同一份字段清单
和枚举，提交时仍由后端权威校验。

## 3. 状态机

Finding：

```
draft ──submit──▶ submitted ──┬─ confirm ────────▶ confirmed（锁定）
  ▲                           ├─ false-positive ─▶ false_positive（锁定）
  │                           └─ request-changes ─▶ changes_requested
  └────────── update ◀──────────────┘（补证后重新 submit）
```

- `draft` / `changes_requested` 可编辑、可删除；`submitted` 等待 human；已决策状态锁定。
- human 可对 confirmed / false_positive 执行 reopen，回到 `changes_requested`，留审计。

问题板：

```
in_progress ──reviewer complete（无 draft/changes_requested + summary）──▶ completed
      ▲                                                                       │
      └────────── human request-changes / reopen board ◀──────────────────────┘
```

任务闸门：

- 问题板 `completed` 且所有 finding 为 `confirmed` / `false_positive` 时，
  `POST /tasks/{id}/review {approve:true}` 才允许通过。
- workflow 与问题板完成顺序无关：
  - workflow 先完成 → 任务保持 `in_progress`，系统 @reviewer 并创建 `[anvil:findings]` run；
    问题板完成后 `finalize_task_review_if_ready` 把任务推进到 `in_review`。
  - 问题板先完成 → workflow 完成时 `_finalize_if_done` 直接推进到 `in_review`。
- 空问题板也必须由 reviewer 执行 `finding complete --summary "未发现安全问题…"`，
  避免「没填 = 没问题」。

## 4. Cindy 规划

启用问题板后，Cindy 在 `PLANNING_PROTOCOL` 中收到额外要求：

1. 优先从用户勾选的角色中寻找审核/审计/安全/测试类角色作为 reviewer；
2. 没有合适角色时，绑定现有合适角色（写审计），或提交 `role_suggestion` 让 human 批准新建；
3. 用 `anvil task reviewers <task-id> --assignees <role>` 指定；
4. workflow 必须覆盖 reviewer：flat steps 优先给 reviewer 的步骤 `gate=1`；
   plan_slices/loop 内禁止 gate，由任务最终评审承担 human 审核；
5. 不允许静默启用一个无人填写的问题板。

## 5. API

前缀 `/api/v1`：

```
GET    /tasks/{tid}/findings/schema
GET    /tasks/{tid}/findings/summary
GET    /tasks/{tid}/findings?status=&severity=&risk_type=&q=
POST   /tasks/{tid}/findings                       reviewer，创建草稿
GET    /tasks/{tid}/findings/{fid}
PATCH  /tasks/{tid}/findings/{fid}                 reviewer，draft/changes_requested
POST   /tasks/{tid}/findings/{fid}/submit          reviewer，完整校验
POST   /tasks/{tid}/findings/{fid}/confirm         human
POST   /tasks/{tid}/findings/{fid}/false-positive  human
POST   /tasks/{tid}/findings/{fid}/request-changes human，唤醒 reviewer
POST   /tasks/{tid}/findings/{fid}/reopen          human，推翻已决策结论
DELETE /tasks/{tid}/findings/{fid}                 reviewer（draft）或 human
POST   /tasks/{tid}/findings/complete              reviewer
POST   /tasks/{tid}/findings/reopen                human
POST   /tasks/{tid}/reviewers                      planner/onboarding/human
DELETE /tasks/{tid}/reviewers/{role}               human；不能删最后一个
```

submit 缺字段返回 422，`detail.missing_fields` 列出待补字段；task review 闸门返回 409。
写操作全部走 `assert_task_read` + reviewer/human 校验；reviewer 也算任务可读参与者。

## 6. CLI 与前端

- CLI：`anvil finding template|add|update|submit|list|show|complete|delete`，
  `anvil task reviewers`，`anvil task create --issue-board`。详见 `docs/cli-reference.md` §5.2。
- 前端：
  - 新建任务勾选「启用问题板（安全审计 findings）」；
  - 任务详情条件显示「问题板」页签，待确认数量徽标；支持
    `/tasks/{id}?tab=findings&finding={fid}` 深链；
  - 页签内展示 board 状态、reviewer、状态/等级统计、筛选与搜索、finding 列表；
  - 右侧抽屉展示全部字段分组（定位 / 触发 / 分析 / 攻击路径 / PoC / 修复 / 决策记录），
    human 可确认真实、标记误报、要求重新评估、推翻结论；
  - `in_review` 横幅在问题板未完成/有待确认项时禁用「通过」，并可跳转问题板；
  - 收件箱新增 `finding` 类型：问题板完成且仍有 submitted finding 时出现，
    全部确认后消失（此时普通评审项出现）。

## 7. 数据与迁移

`schema.issue_board_v1` 一次性迁移：

- `tasks.issue_board_enabled INTEGER NOT NULL DEFAULT 0`
- `task_reviewers(task_id, role_slug, source, added_at)`
- `task_finding_boards(task_id, status, summary, created_at, completed_by, completed_at,
  reopened_by, reopened_at, updated_at)`
- `task_findings(...)` + `idx_task_findings_task_status`、`idx_task_reviewers_role`

任务删除级联删除三张表；启动时 `_cleanup_orphans` 清理无主记录。

## 8. 测试

- `tests/test_migrations_issue_board.py`：空库 / v1.30.9 升级 / 幂等。
- `tests/api/test_issue_board_api.py`：端到端、PoC 豁免与误报、退回重评、inbox、
  workflow 覆盖 reviewer、flag/reviewer 权限。
- `tests/test_workflow.py`：workflow/问题板完成顺序无关、reviewer 必须被 workflow 覆盖。
- `tests/cli/test_cli_contract.py`：finding 命令映射、模板字段、`--issue-board` 透传。
