# Workflow 增量修订：安全变更不重启任务

> 实施状态（2026-10-29）：后端影响分析/原地应用、API、CLI、收件箱与任务详情审批已落地；
> v1 只支持调整未来（pending）切片与 pending 静态步骤。局部回滚重做、切片重排、
> loop 阶段/角色热替换留给后续版本。

## 问题

执行角色在切片 loop 中常会反馈「还需要多轮/少一轮/换一个范围」。此前这类反馈只能让
Cindy 提交一份新的完整 workflow 草案；human 批准后系统终止当前执行，新建
`execution_no+1` 从第一步重跑。已完成切片、当前 run 与全部证据都会作废，代价过高。

目标：**只影响未来步骤的修订在原 execution 内原地生效**，不终止 run、不新建 execution、
不重跑已完成步骤；一旦会影响 running/submitted/approved 步骤或其契约，就不自动改，
回退到完整 workflow 草案 + human 明确确认「重启或放弃」。

## 修订载体

复用 `board_drafts`，新增 `kind=workflow_revision`，不新增表/迁移。草案 `tree_json`：

```json
{
  "schema_version": 1,
  "base_workflow_id": "wf-...",
  "base_execution_no": 1,
  "base_updated_at": "2026-10-29T...",
  "rationale": "切片角色反馈：认证模块还需补一轮",
  "intent": {
    "add_slices": [
      {
        "id": "auth2",
        "title": "认证补充",
        "scope_paths": ["src/auth/**"],
        "objective": "...",
        "instructions": "...",
        "acceptance": ["..."],
        "execute_steps": [{"id": "static", "title": "补充审计"}]
      }
    ],
    "remove_slices": ["pay2"],
    "update_slices": [{"id": "pay", "patch": {"title": "...", "acceptance": ["..."]}}],
    "patch_steps": [{"step_id": "F1", "fields": {"instructions": "...", "acceptance": ["..."]}}]
  },
  "impact": {"classification": "safe_loop", "safe": true, "summary": "...", "blocked_by": []}
}
```

规则：

- 同一任务同时只允许一个待审的 `workflow` 或 `workflow_revision` 草案：
  存在 pending 完整 `workflow` 草案时不允许再建修订草案，反之亦然（409）。
- `workflow_revision` 只能通过修订端点创建，`anvil board draft --kind workflow_revision`
  会被拒绝。
- 审批成功后才标记 `approved`；stale/invalid 返回 409 并保持 pending。驳回意见写入任务
  activity 并 mention 提交者。

## 影响分类（后端权威，不信任 LLM 自评）

`preview_revision(conn, wid, intent)` 返回：

```json
{
  "classification": "safe_loop | safe_future | requires_restart | stale | invalid",
  "safe": true,
  "summary": "追加 1 个未来切片；当前 running 步骤不受影响",
  "affected_steps": ["P1.auth2.static", "P1.auth2.execute"],
  "blocked_by": [{"step_id": "F1", "status": "approved", "reason": "..."}],
  "diff": {"add": ["auth2"], "remove": [], "update": [], "patch_steps": ["F1"]}
}
```

判定要点：

1. workflow 必须 `running`，且已有审批通过的 `plan_slices` 切片计划与 loop 模板。
2. 切片状态由该片全部步骤推导：全部 `pending` 且无 run/lease → 可改；
   任一步 running/submitted/approved → `requires_restart`。
3. `add_slices` 只能追加到尾部；稳定前缀（已开始/已完成的切片）之后重新生成可重建尾部，
   新片接在尾部之后。若依赖旧最后 decide 的静态下游步骤已经 active/settled →
   `requires_restart`（不能插到已开始的最终步骤之前）。
4. `remove_slices` / `update_slices` 目标片必须全部 mutable；`update_slices` 仍只用现有
   loop 角色模板，不允许写角色字段。
5. `patch_steps` 目标必须 `pending` 且无 run/lease；不得把 active/settled 步骤变成新步骤的
   下游依赖，不得修改 active/settled 步骤的契约。
6. 重新校验 DAG、重复 ID、缺失依赖、on_reject 目标与角色覆盖；失败 `invalid`。
7. 审批时在 `BEGIN IMMEDIATE` 内重新分类；目标已推进或分类改变 → stale 409，零修改。

Cindy 的 `claimed_impact` 只作提示，最终以服务端分类为准。

## 原地应用

`apply_revision(conn, wid, intent, actor, draft_id)`：

1. 事务内重载 workflow 并再次分类；非 safe → `RevisionConflict`（409 + impact）。
2. 拆分稳定前缀与可重建尾部，应用 add/remove/update 后更新 plan 步骤的 `plan_json`。
3. 删除可重建尾部的旧 pending 步骤，用 `_dynamic_slice_steps(..., previous_decide=...)`
   重新生成：首片依赖稳定前缀的最后 decide（无稳定前缀则依赖 plan 步骤），
   ID 仍为 `<plan>.<slice>.<phase>`。
4. 把所有 pending 静态步骤中「依赖旧最后 decide」的改为「依赖新最后 decide」。
5. 应用 `patch_steps`，同步镜像回 `definition_json` 并重算 `definition_hash`。
6. 更新 workflow `updated_at`；**不新增 workflow 行、不改变 `execution_no`、不终止当前 run**。
7. 写任务 activity 与 ledger `workflow.revised`（base hash、diff、actor、draft id）。

scheduler 下一 tick 自动派发新生成的 pending 步骤；当前 running 步骤继续执行，
其 lease/run 不受影响。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/workflows/{wid}/revisions/preview` | 只分析影响，不落草案 |
| POST | `/api/v1/workflows/{wid}/revisions` | 创建修订草案；非 safe 返回 409 + impact |
| POST | `/api/v1/board/drafts/approve` | 修订草案批准后原地应用；stale 返回 409 + 最新 impact |
| POST | `/api/v1/board/drafts/reject` | 保存 feedback、写 activity、mention 提交者 |

`GET /api/v1/workflows/{wid}` 与步骤详情会附带 `pending_revision`（draft id + impact）。
修订端点只允许 task planner / onboarding（Cindy）与 human；执行角色只能反馈，不能改 workflow。

## CLI

```bash
anvil workflow revise <wf> --file revision.json --preview   # 仅分析
anvil workflow revise <wf> --file revision.json             # 创建修订草案
anvil step show <wf> --json                                # 查看 pending_revision
```

分类处理：

- `safe_loop` / `safe_future`：提交修订草案并 @human，说明「不重启、只改未来步骤」。
- `requires_restart`：不要提交修订草案；改为完整 workflow 草案，并在 @human 消息中列出
  受影响步骤与原因，由 human 决定重启或放弃。
- `stale` / `invalid`：重新 `step show` 后再判断，不盲目重试。

## UI

- 收件箱：修订卡展示 classification / summary / blocked_by；按钮为「应用修订（不重启）」，
  批准不弹重启确认；驳回可填写意见发回 Cindy。
- 任务详情：待审草案卡展示 impact；修订批准直接应用；完整 workflow 草案在运行中仍走
  「终止并重新执行」确认（需填写原因）。

## v1 不做

- 局部回滚重做（把已 approved 步骤及其下游 reset 后重跑）。
- 切片重排、修改 loop 阶段/角色模板、增删静态步骤。
- 修改 running/submitted/approved 步骤的 title/instructions/acceptance/contract/role。

以上一律分类为 `requires_restart`，走完整 workflow 草案 + human 重启确认。
定向重做需要单独处理产物、证据、下游依赖与 HITL，留待后续版本设计。

## 验证

```bash
cd dev
PYTHONPATH=. .venv/bin/pytest tests/test_workflow_revision.py tests/api/test_workflow_revision_api.py -q
PYTHONPATH=. .venv/bin/pytest tests/ -q
cd frontend && npm run build
```

覆盖场景：当前切片 running 时追加未来切片（execution_no 不变、run 不终止、最终静态步骤
等待新片）；删除/修改 pending 切片；patch pending 静态步骤；patch running/approved 步骤
返回 `requires_restart`；审批前状态推进导致 stale 409 且零修改；驳回反馈唤醒 planner。
