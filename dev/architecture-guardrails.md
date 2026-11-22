# 架构护栏与复杂度预算（P0.8）

本文件约束后续改动，避免状态/队列/事件继续扩散。违反下列规则的改动必须先补
单写者函数与不变量测试，否则不合并。

## 单写者规则

| 事实 | 唯一写者 | 不变量测试 |
|---|---|---|
| `tasks.status` | `services.tasks.transition_task` | `tests/test_task_transition.py` |
| `dispatch_status` | 派生视图（`_attach_dispatch_status`），不落库 | `tests/test_task_transition.py` / `tests/test_dispatch_task.py` |
| `activities` / `audit` / `events` | `services.ledger.emit_*` | `tests/test_event_ledger.py` |
| 角色状态 | `services.role_state.derive_task_role_state`（API 输出） | `tests/test_role_state.py` |
| 子任务视图 | `services.child_tasks.list_children`（delegation/dispatch 归一） | `tests/test_child_tasks.py` |
| durable work item 读取 | `services.work_items.list_items` | `tests/test_work_items.py` |
| Prompt 协议与 run 分类 | `graph/render.py` 常量 + `nodes._run_prompt_kind/_protocols_for_run` | `tests/test_prompt_snapshot.py` |
| 前端状态词汇 | `src/statusVocabulary.ts`（task/run/dispatch）+ `roleStatus.statusByKey` | `tsc --noEmit` + `npm run build` |

## 禁止事项

- 禁止业务模块直接 `INSERT INTO activities` / `INSERT INTO audit` / `INSERT INTO events`；
  必须走 `services.ledger`。
- 禁止新增 `tasks` 上的状态镜像列（如 `dispatch_status`）；需要查询时派生。
- 禁止绕过 `transition_task` 写 `tasks.status`；静态守卫见 `tests/test_task_transition.py`。
- 禁止删除/改写 `events` 的历史行；状态表仍是权威，events 只追加。
- 新增持久状态列必须同时提供：单写者函数、CAS/幂等语义、不变量测试。

## 复杂度预算（当前 → 目标）

| 指标 | 当前 | 目标 |
|---|---:|---:|
| 持久状态家族 | ~30 | ≤18（P0 后） |
| 状态 token | 61 | ≤40 |
| `workflow.py` LOC | 75（门面；最大子模块 1,618） | ≤2,500/文件 ✅ P1.9 |
| `daemon.py` LOC | 165（门面+核心；最大子模块 1,100） | ≤2,000/文件 ✅ P1.10 |
| `TaskDetail.tsx` LOC | 1,831（已拆 7 个组件） | ≤1,500（剩余 chat/workflow 面板） |
| scheduler 阶段 | 22 | ≤12 |
| 前端状态映射 | 1 份/实体（task/run/dispatch/role/step） | 保持 |
| 镜像状态字段 | 0（`dispatch_status` 已派生） | 0 |

超出预算的新增需要在本文件记录例外与回收计划。

## P0.7 清理记录

- 删除 `cost_ledger` 建表：旧美元成本账本已无读写，Token 统计见 `token_usage`。
- 删除 `repo_split_map` 建表与迁移写入：一次性迁移日志无读取方；迁移效果由
  `task_workspaces` / `task_knowledge_repos` 验证。
- 保留 legacy step 状态 `rejected`：历史数据仍可能出现，读路径（retry/restart）
  继续兼容；新写入不再产生该状态。
- 保留 one-shot 运行时路径：仍是流式协议不可用时的回退，不是 dead code。
