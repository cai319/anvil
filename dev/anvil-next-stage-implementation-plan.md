# Anvil 下一阶段实施建议（7 点反馈落地）

## Context

Anvil 目前已经具备：

- Task 工作空间 + workflow 状态机（steps / depends_on / gate / decision / loop / plan_slices）；
- 持久角色运行时、任务频道、HITL；
- Meeple 模板模型（中文名 + role 名，可选绑定角色）；
- 运行中 workflow 不可替换；人工确认后终止旧执行、创建新 execution 并保留历史。

当前主要缺口不在“功能数量”，而在**流程可解释性、证据质量和并发正确性**：

- gate 只把 step 对象和 40 字摘要丢给人，人类需要自己还原“要决定什么、证据是什么、拒绝后重做什么”；
- 决策没有不可变记录，`workflow_steps.feedback/summary` 会被覆盖；
- `activities` 与 `audit` 两套历史，没有全局序号、workflow/step/run 上下文和分页；
- `workflow_steps` 没有 authority/evidence/decision 字段；
- 人类 gate 驳回只能重置当前步骤，不能选择返工范围；
- step 没有 lease/fencing，`submit` 不校验 run_id；workflow dispatch 绕过治理闸门；
- 没有 planned vs actual 执行图、没有可导出的执行档案；
- 流程长度没有统一指标，无法判断长在等待、返工还是串行 handoff。

目标：按“事件账本地基 → 证据化 HITL → 流程强度与执行档案 → 租约/围栏 → 实验”的顺序推进，先解决真实正确性和人类决策质量问题，再做更重的并发与评估能力。

## 推荐顺序（结论）

| Phase | 目标 | 关键交付 | 粗估 |
|---|---|---|---|
| 0 | 可观测与正确性地基 | 统一事件账本、activities 上下文、run_id 围栏、dispatch 治理检查 | 4–6 人日 |
| 1 | 有证据的 HITL | Review Packet、决策记录、ai/human/joint 权限、升级、审查抽屉 | 8–12 人日 |
| 2 | 流程强度与执行档案 | weight、planned/actual graph、dossier、流程指标、历史执行查看 | 10–14 人日 |
| 3 | 并发与围栏 | step/slice/path lease、fencing、原子 dispatch、scope 检查 | 6–9 人日 |
| 4 | 实验/基准（暂缓） | 合成任务集、light/standard/strict A/B、指标对比 | 后续 |

**如果只做两件事：Phase 0 + Phase 1。**  
Phase 0 是所有审计/指标/Dossier 的地基，并修复当前真实竞态；Phase 1 直接解决“gate 没证据、人类不知道在批什么、决策无记录、AI/人类权限不清”四个最痛的点。

---

## Phase 0：统一事件账本与正确性地基

### 目标

1. 每一次 workflow / run / gate / decision / graph / lease 变化都有全局序号和上下文。
2. 旧 run 在 step 重新派发后不能继续提交。
3. workflow dispatch 必须经过 freeze / budget / 并发闸门。
4. 为 packet、metrics、dossier 提供统一数据源。

### 数据模型

新增 append-only `events` 表：

- `id, ts, event_type, schema_version`
- `task_id, workflow_id, execution_no, step_id, slice_key, run_id`
- `actor_type, actor, causation_id, correlation_id`
- `source, source_id`（activity/audit 回填幂等）
- `payload_json`
- 索引：`(task_id,id)`, `(workflow_id,id)`, `(workflow_id,step_id,id)`, `(event_type,id)`, `(run_id,id)`, `source/source_id` 唯一。

`activities` 增加 `event_id, workflow_id, step_id, slice_key, execution_no` 及索引。  
`events` 是流程语义账本；`activities` / `audit` 继续作为会话和治理投影，**不重写状态机**。

### 后端改动

- 新增 `backend/anvil/services/ledger.py`：
  - `emit(...)`：所有事件统一入口，支持 `mirror_activity` / `mirror_audit`；
  - `list_task_events(...)` / `list_events(...)` / `backfill(...)`；
  - 固定事件命名：`step.*`、`gate.packet_created`、`decision.recorded`、`graph.changed`、`lease.*`、`execution.*`、`workflow.weight_selected`。
- `backend/anvil/services/workflow.py`：
  - `_step_activity` 改为通过 ledger 发事件并补上下文；
  - `submit_step`、`decide_step`、`review_step`、`retry_step`、`fail_step`、`apply_slice_plan`、`create_workflow`、`_finalize_if_done` 全部发事件；
  - `advance()` 在创建 run 前调用 `governance.check_claim_allowed`。
- `backend/anvil/services/runs.py`：`set_status` 发 `run.status_changed`；补齐直接 UPDATE run 的路径（`decide_step` / `apply_slice_plan`）。
- `backend/anvil/services/tasks.py`：`comment` / `mention` / `_audit` 双写事件；`timeline` 增加 `seq/workflow_id/step_id/slice_key/execution_no`。
- `backend/anvil/deps.py`：`Auth` 增加 `run_id`，从 `sessions.run_id` 获取。
- `backend/anvil/api/workflow.py`：submit/decide/plan 校验 `auth.run_id` 与当前 step 绑定一致。
- 新增 `backend/anvil/api/event_log.py`：
  - `GET /api/v1/tasks/{tid}/events?after_id=&limit=&kinds=&workflow_id=`
  - `GET /api/v1/event-log?task_id=&workflow_id=&event_type=&after_id=&limit=`
  - 不占用 `/api/v1/events`（SSE 已使用）。

### 前端

- `frontend/src/api/client.ts`：TimelineEvent 增加 `seq/workflow_id/step_id/slice_key/execution_no`；新增 `listTaskEvents`。
- `TaskDetail.tsx`：时间线显示执行号和步骤 ID；增加“决策/审核门/图变更”筛选。
- 旧的 `/tasks/{tid}/timeline` 返回结构保持兼容，只增加字段。

### 验收

- 任一 step 状态变化恰好产生一条事件；
- 旧 run 在 step 重派后提交返回 `409 stale_run`；
- freeze / budget / 并发达限时 workflow 不创建新 run；
- 旧库启动自动迁移，现有 pytest 全绿。

---

## Phase 1：证据化 Gate、决策记录与权限矩阵

### 目标

把“人看一个对象随手点通过”变成“人回答具体问题、看到可核验证据、留下可追溯决策”。

### 数据模型

新增：

- `gate_packets`：`workflow_id, step_id, revision, kind(gate|decision), status, authority, question, options_json, acceptance_json, evidence_json, upstream_json, risks_json, recommendation_json, evidence_digest, sla/deadline, superseded_by`。
- `decisions`：`packet_id, workflow_id, step_id, authority, decider_type, decider, decision, feedback, reasons_json, rework_targets_json, evidence_digest, override_reason, latency_seconds`。

`workflow_steps` 增加：

- `decision_authority`（空=派生：decision→ai，gate→human）
- `risk_level`、`reversible`、`escalate_json`
- `decision_question`、`decision_options_json`
- `evidence_json`、`evidence_required`
- `packet_id`、`submitted_at`、`scope_paths_json`

### 决策权限

```text
L0 领域角色：调查、产出、独立验证
L1 AI decider：可逆、契约内、证据充分、低风险
L2 人类：不可逆、策略/合规/安全、scope 变更、证据冲突、低置信度、返工耗尽、预算变更
```

`decision_authority`：

- `ai`：AI 可 approve/reject；人类可覆盖但需理由；
- `joint`：AI 可 reject；approve 只能提交建议，由人类确认；
- `human`：**不启动 AI decider run**，依赖满足后直接生成 packet 并进入人类待办。

升级触发器显式化：`out_of_scope_change`、`irreversible_action`、`policy_or_security`、`evidence_missing`、`evidence_stale`、`acceptance_unmet`、`rework_exhausted`、`low_confidence`、`budget_exceeded`、`scope_change`。

### Review Packet

gate 和 decision 共用 packet：

```text
具体问题
选项（approve/reject/escalate，reject 必填理由或原因）
验收清单（met/unmet + 证据引用）
证据（file/test/command/artifact，sha256，captured_at，fresh/stale/missing）
上游结论与决策
风险
AI 建议与置信度
拒绝后的返工范围
SLA / 已等待时间
```

- 文件证据只允许 repo / agent home 内路径，拒绝绝对路径和 `..`；限制数量/大小。
- 决策记录保存当时的 `evidence_digest`。
- 证据 stale 时 AI 不得 approve，必须 escalate；人类可 `acknowledge_stale` 并写理由。

### 后端改动

- 新增 `backend/anvil/services/reviews.py`：
  - `build_packet`、`refresh_packet`、`latest_packet`、`decisions_for_step`；
  - `decide`、`escalate`、`_authority`、`_check_escalation_triggers`、`_reject_closure`。
- `workflow.py`：
  - `submit_step` 接收 evidence 并生成 gate packet；
  - `decide_step` / `review_step` 委托 reviews.decide；
  - `advance`：human authority 不建 run，直接 packet + submitted；
  - `gates()` 扩展；新增 `human_decisions()`；
  - `step_prompt` 注入 packet；
  - `_plan_steps` / `_validate_loop` 校验 authority、risk、reversible、options。
- `inbox.py`：gate item 使用 packet（问题、证据摘要、等待/SLA、推荐）；新增 `kind=decision`；按逾期优先排序。
- `graph/render.py`：STEP_PROTOCOL 增加证据提交和 escalate；PLANNING_PROTOCOL 增加 authority/risk/reversible 规则。
- CLI：`step submit --evidence-file`、`step decide --rework-targets --reason`、`step escalate`、`step packet`。

### API

```text
GET  /workflows/{wid}/steps/{sid}/packet
POST /workflows/{wid}/steps/{sid}/packet/refresh
POST /workflows/{wid}/steps/{sid}/submit     { summary, evidence }
POST /workflows/{wid}/steps/{sid}/decide     { approve, feedback, packet_id, rework_targets, reasons, override_reason, acknowledge_stale }
POST /workflows/{wid}/steps/{sid}/escalate   { reason, triggers, proposed_decision, proposed_feedback }
POST /workflows/{wid}/steps/{sid}/review     # human gate，扩展 packet 字段
```

错误语义：`403 human_decision_required`、`409 escalation_required`、`409 evidence_stale`、`409 packet_superseded`、`409 stale_run`。

### 前端

- 新增 `frontend/src/components/GateReviewDrawer.tsx`：问题、选项、验收清单、证据表、上游结论、风险、AI 建议、决策历史、理由、返工范围树。
- `Inbox.tsx`：gate/decision 卡片显示具体问题、证据摘要、等待/SLA、推荐；点击打开抽屉；新增 decision tab。
- `TaskDetail.tsx`：时间线增加决策/审核门筛选；workflow 详情显示 authority、packet 摘要、决策记录。
- `WorkflowGraph.tsx`：节点增加“人工决策/联合决策/证据缺失/过期”徽标；详情面板可打开 packet。
- 前端不再硬编码驳回理由。

### 验收

- Inbox 的 gate 展示具体问题 + 选项 + 证据状态；
- 每次 approve/reject/escalate 都有 `decisions` 行和事件；
- human authority 不消耗 AI run/token；
- 旧 API `{approve, feedback}` 兼容；旧运行中 workflow 首次访问时惰性生成 packet。

---

## Phase 2：流程强度、执行档案与指标

### 目标

- Cindy 按任务/角色选择 `light | standard | strict`；
- 人类可覆盖，但运行中仅下一次执行生效；
- 执行过程成为可导出、可评估的交付物；
- 能量化流程长度。

### 流程强度

- `light`：小范围、可逆、1–2 角色；flat steps；默认 0 个人工门；AI 决策；loop 可省略 fix/verify2。
- `standard`：当前行为。
- `strict`：安全/审计/发布/迁移/权限/支付/宽 scope；证据必填；高风险/不可逆节点 human/joint；最终人工决策。

Cindy 在 workflow 草案根级输出 `weight` + `weight_rationale`；服务端缺省时按任务/角色启发式补全；人类可覆盖并记录原因。

### 数据模型

- `workflows` 增加 `weight, weight_source, definition_hash, finished_at`。
- `tasks` 增加 `workflow_weight, workflow_weight_source, workflow_weight_reason`。
- 新增 `workflow_dossiers`（终态持久化，JSON + digest）。
- 指标不建表：从 `workflow_steps` 时间戳、packets/decisions、events、`token_usage JOIN runs` 计算；终态写入 dossier。

### 后端

- 新增 `services/weights.py`、`services/metrics.py`、`services/dossier.py`。
- `create_workflow` 解析/快照 weight、definition_hash；human override 优先。
- `_finalize_if_done` / `_terminate_running_workflow` 持久化 dossier。
- `apply_slice_plan` 记录 `graph.changed`（新增节点、改挂边、scope、授权来源）；越界 scope 生成人工决策，不得静默扩张。
- `list_for_task` 返回 weight + metrics 摘要。
- `_dynamic_slice_steps` 支持 weight：light 生成 execute→verify→decide，减少阶段；strict 要求证据/最终 human decision。

### API

```text
GET  /workflows/{wid}/metrics
GET  /workflows/{wid}/graph?mode=planned|actual|diff
GET  /workflows/{wid}/dossier[?format=md]
POST /workflows/{wid}/dossier/rebuild       # human
GET  /tasks/{tid}/dossier?workflow_id=
POST /tasks/{tid}/workflow-weight           # human；运行中返回 requires_restart
DELETE /tasks/{tid}/workflow-weight
POST /tasks/{tid}/workflow/restart          # human；新 execution
```

### 前端

- WorkflowSummary 增加 weight 徽标、Cindy 依据、指标条（步数/门/决策/返工/人工等待/耗时/tokens）。
- 执行历史行可点击加载历史 workflow；历史只读。
- “调整流程强度”对话框：运行中说明需重启；重启后新 execution 使用新 weight。
- “导出执行档案” JSON/Markdown。
- WorkflowGraph 支持 planned/actual/diff；显示 graph change、authority、packet、决策记录、每步 token。
- 新增 `ExecutionDossier.tsx` + 路由 `/tasks/:id/dossier`。

### 验收

- Cindy 草案包含 weight 和依据；
- 人类覆盖 weight 不改运行中 workflow，重启后生效；
- planned/actual 图能解释每个动态节点；越界 scope 不静默通过；
- dossier 含决策、证据、返工、指标、token，可下载；
- 流程长度指标可见，并能按 weight 预算告警。

---

## Phase 3：Step/Slice/Path Lease 与 Fencing

> 保留完整方案（已确认）。安排在 Phase 2 之后，作为并发、多进程、多角色同仓库写入的可靠性基础设施，不因当前单机场景降级。

### 目标

把 check-then-act + 进程内锁升级为数据库级租约；补 artifact/path 冲突防线；dispatch 与治理闸门原子化。

### 数据模型

- 新增 `leases`：`resource_type(step|slice|path|task), resource_key, holder_type, holder, run_id, fence, status, acquired_at, renewed_at, expires_at, meta_json`。
- 唯一活跃租约：`(resource_type, resource_key) WHERE status='active'`。
- `workflow_steps` 增加 `lease_id, fence_token`。
- task lease 继续使用现有字段；`leases` 管理 step/slice/path。

### 实现

- 新增 `services/leases.py`：`acquire/renew/release/active/reap_expired/check_path_conflicts`。
- `workflow.advance` 在 `BEGIN IMMEDIATE` 中获取 step lease；避免并发 double start。
- 创建 run 后写 lease_id/fence_token；submit/plan/decide 校验 run_id + lease active + fence。
- `submit_step` 收集 changed paths，校验 slice scope/path lease；越界拒绝或升级。
- scheduler 续租/回收；governance dispatch 检查复用 `check_claim_allowed`。
- `tasks.claim(paths=...)` 实际写 path lease；submit 检查冲突；释放租约。
- API：`GET /tasks/{tid}/leases`、`POST/DELETE /tasks/{tid}/reservations`。
- UI：WorkflowGraph 显示 lease holder/run/expiry/fence；TaskDetail 显示路径预留/冲突。

### 验收

- 两个 scheduler/API 线程同时 advance，只产生一个 run；
- run 崩溃后 lease 到期，step 恢复 pending 或失败；
- stale run 提交被拒；
- 越界路径/冲突预留被拒或升级人工。

---

## Phase 4：实验（暂缓）

等 Phase 2 指标和事件稳定后，做内部 light/standard/strict 对照：

- 周期时间、活跃时间 vs 等待时间；
- 返工率、人工介入次数；
- 重复劳动/冲突；
- 审计完整度；
- 单次交付成本；
- 人工 gate 的决策质量。

现在只保留 weight 和指标接口，不建完整 benchmark 平台。

---

## 现在不要做

- 运行中图谱随意编辑；继续禁止替换 workflow，只允许“终止 → 新 execution”；切片展开必须记录和校验。
- 自动创建角色/自动改绑；保持 `role_suggestion` 人类批准硬边界。
- 事件溯源重写状态机；状态表继续权威。
- 一次性替换所有 activities/audit 写入；先覆盖 workflow/run/gate/decision/lease/graph。
- 为 metrics/evidence/rework 各建表；先计算/JSON，确认瓶颈再物化。
- SLA 自动通过/自动批准。
- 在事件里存完整 prompt/transcript；存 ref/hash/摘要。
- 所有历史数据强制回填 packet；旧执行按需惰性生成。

---

## 关键文件

现有：

- `backend/anvil/services/workflow.py`
- `backend/anvil/db.py`
- `backend/anvil/services/inbox.py`
- `backend/anvil/services/tasks.py`
- `backend/anvil/services/governance.py`
- `backend/anvil/scheduler.py`
- `backend/anvil/deps.py`
- `backend/anvil/api/workflow.py`
- `frontend/src/pages/TaskDetail.tsx`
- `frontend/src/pages/WorkflowGraph.tsx`
- `frontend/src/pages/Inbox.tsx`

新增：

- `backend/anvil/services/ledger.py`
- `backend/anvil/services/reviews.py`
- `backend/anvil/services/leases.py`
- `backend/anvil/services/weights.py`
- `backend/anvil/services/metrics.py`
- `backend/anvil/services/dossier.py`
- `backend/anvil/api/event_log.py`
- `frontend/src/components/GateReviewDrawer.tsx`
- `frontend/src/pages/ExecutionDossier.tsx`

---

## 验证

后端：

- 新增 `tests/test_event_ledger.py`、`test_review_packets.py`、`test_decision_authority.py`、`test_rework_scope.py`、`test_workflow_weight.py`、`test_workflow_metrics.py`、`test_dossier.py`、`test_workflow_leases.py`、`test_graph_mutation.py`。
- API：packet/decide/escalate/review、event-log、weight/restart、inbox gate/decision、CLI 契约。
- 迁移测试：旧表旧数据 → `init_schema` → 新表/列/回填幂等。
- 并发测试：双 advance 只启动一次；双 decide 只成功一次；lease 过期与 submit 竞争。
- 全量 `pytest -q`。

前端：

- `cd dev/frontend && npm run build`。
- Phase 1 引入 `@playwright/test` + `frontend/e2e/`：
  1. Inbox gate 抽屉展示问题/证据；空理由驳回被阻止；
  2. AI 决策升级后 Inbox 出现 decision item；
  3. 执行历史切换后图谱只读；
  4. dossier 下载；
  5. weight 覆盖提示“下一次执行生效”。

兼容：

- 旧 `/tasks/{tid}/timeline`、旧 gate `{approve, feedback}`、旧 workflow 行仍可用；
- 旧 DB 新增列有默认值，新表不被旧代码查询。
