# 运行时 subagent 观测设计（D3）

> 状态：草案（2026-11-21）
> 关联：`2026-11-10-slice-context-dispatch-design.md`、`dev/backend/anvil/services/daemon.py`
> 前置决策（已与用户确认）：subagent 由运行时全权管理；Anvil 只采集、展示、提供只读查询，
> **不参与调度/唤醒/状态机决策**。派遣/委派才是 Anvil 管理的 child task。

## 1. 边界

| 概念 | 归属 | 生命周期 | Anvil 职责 |
|---|---|---|---|
| dispatch 子任务 | Anvil | 跨 turn、非阻塞 | 状态机、回切、调度 |
| delegation 子任务 | Anvil | 跨 turn、阻塞父 step | 状态机、验收、唤醒 |
| 运行时 subagent | 父运行时进程 | turn 级 | 只观测 + 只读查询 |
| subagent_hint | 规划期提示 | 随 step | 提示词建议（D4 契约） |

硬约束：

- `scheduler` / `workflow` / `leases` / `context_manifest` / `context_router` 永不读取
  `run_subagents` 或 `active_subagents`；源码契约测试固定。
- 子代理完成不产生 activity/mention/wake；结果只在本 turn 由运行时交给模型。
- 跨 turn 的产物必须由父角色写入 step summary/evidence，或改用委派工单。

## 2. 数据模型 v2

`run_subagents` 现有字段：`id/run_id/role_slug/task_id/tool_use_id/tool_name/
description/prompt_summary/status/started_at/ended_at/result_summary/context_available/
updated_at`。

新增（迁移 `schema.subagents_v2`，向后兼容）：

| 字段 | 含义 |
|---|---|
| `subagent_type` | 运行时报告的子代理类型（例如 Explore） |
| `proc_generation` | 父角色进程代次（`_Role.proc_generation`，每次 spawn +1） |
| `native_session_id` | 父 run 的原生会话 id，区分同 run 换会话 |
| `parent_tool_use_id` | 运行时 sidechain 的父 tool_use（原始值） |
| `slice_key` / `step_id` | 归属上下文（从 run 回填） |
| `interrupt_reason` | `turn_ended/run_suspended/run_failed/run_deleted/daemon_restart/process_lost` |

索引：`(run_id,status)`、`(role_slug,started_at)`、`(task_id,started_at)`。
已有行按 `runs` 回填 `native_session_id/slice_key/step_id`。

终态三态：`running / completed / interrupted`。`interrupted` 覆盖 turn 结束、进程退出、
restart/reset/cancel、任务删除、daemon 重启自愈。

## 3. 写入侧（daemon）

- `_track_subagent(r, ev, proc)`：tool_use 时捕获 `proc_generation/native_session_id/
  parent_tool_use_id`；`_persist_subagent` 落库。
- tool_result：内存命中 → `completed`；内存未命中 → 回查 DB：
  - `status='running'` → CAS 改 `completed` 并写 `result_summary`；
  - `status='interrupted'` → 保留 `interrupted`，仅补记 `result_summary`；
  - 晚到结果不把已中断的记录伪装成 turn 内完成。
- 重复 tool_use upsert 不得把终态重置为 `running`；终态下保留 `ended_at/result_summary`。
- `_finish_subagents` 在 turn result 时把所有仍 `running` 的项标 `interrupted
  (turn_ended)`；这是“turn 结束即终止”的唯一事实来源。UPDATE 严格按当前
  `run_id`（`WHERE run_id=? AND status='running'`），不会误伤其它 run/角色的子代理。
- `DaemonManager.__init__` 自愈：daemon 重启后不存在任何存活子代理，把残留
  `running` 统一标 `interrupted (daemon_restart)`。
- 任务删除：`tasks.delete` 清理该任务下 `run_subagents`。

## 4. 兜底 reaper（scheduler）

新增阶段 `reap_interrupted_subagents`（只写观测表，不影响任何调度判定）：

- run 不存在 → `run_deleted`；
- run 非 `running` → `run_<status>`；
- run 仍 `running` 但 daemon 已不认为该 tool_use 活跃 → `turn_ended/process_lost`；
- daemon 未启用/未知 → 不凭内存推断，等 turn result 或 run 状态变化。

reaper 与 `_awaiting_external_work`/nudge **无任何交互**。

## 5. 只读 API / CLI

- `GET /runs/{rid}/subagents?status=&limit=`：`access.assert_run_read`；父角色可读。
- `GET /roles/{slug}/subagents?status=&limit=`：Agent 只能读自己；human 任意。
- `GET /tasks/{tid}/subagents?status=&limit=`：绑定该任务的会话可读。
- `anvil subagent list --run <id> | --role <slug> | --task <id> [--status] [--limit] [--json]`
  只读；没有写/唤醒/取消命令。

## 6. UI

- RoleSessions：实时 `active_subagents`（已有）+ **历史列表**（running/completed/
  interrupted、类型、耗时、result_summary 摘要）。
- TaskDetail：保留运行中子代理 chip；无编辑入口。
- 标签明确写“运行时管理，Anvil 只观测”，不与委派/派遣卡混排。

## 7. 提示词

`SUBAGENT_PROTOCOL` 增补：

- 子代理是 **turn 级**：turn 结束仍未完成的会被终止，不会跨 turn 存活，也不会唤醒你；
- 不要以“等子代理”为理由结束 turn；等待必须在同一 turn 内完成；
- 跨 turn 产出写 step evidence 或改用委派工单。

## 8. 验证

- turn 结束 → `interrupted(turn_ended)`；进程退出/restart/reset/cancel/任务删除/
  daemon 重启 → reaper/清理后无 `running` 孤儿。
- 晚到 tool_result 的 CAS 行为。
- 重复 tool_use 不重置终态。
- 只读 API/CLI 按 run/role/task 查询与权限。
- 源码契约：scheduler/workflow/leases/context_manifest 不含 `run_subagents`/
  `active_subagents`；运行中的 subagent 不影响 nudge。

## 9. 非目标

- 不创建/终止/重试 subagent；
- 不把 subagent 纳入 task/workflow/lease 状态机；
- 不为 subagent 建 Anvil 任务卡、收件箱项或唤醒。
