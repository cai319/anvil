# 角色状态与颜色统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/superpowers/specs/2026-11-11-role-status-color-model-design.md` 统一任务内角色、全局角色、workflow slice 的状态来源、优先级和颜色。

**Architecture:** 新增单一 `roleStatus.ts` 产出 `{key,label,tone,detail,queued}`，任务内和全局各一个入口。`RoleAvatar`、`.role-live-dot`、`.status-light` 统一消费 `tone`。workflow slice/step 使用独立 tone 映射。

**Tech Stack:** React 18、TypeScript、Vite、现有 CSS tokens。

---

### Task 1: 新增统一状态模块

**Files:**
- Create: `dev/frontend/src/roleStatus.ts`
- Test: 临时 Node 脚本编译 `roleStatus.ts` 并断言（项目无前端测试框架）

- [ ] 定义 `StatusTone = "orange" | "yellow" | "red" | "green" | "gray"`。
- [ ] 实现 `taskRoleState(input)`，输入当前 task、role、runtime、workflow、relatedTaskIds、runStatus、returnPending。
- [ ] 实现 `globalRoleState(input)`，输入 runtime、latestRunStatus、hasSubmittedRun。
- [ ] 实现 `workflowRoleState(workflow, role, sliceKey)` 和 `workflowSliceState(steps)`。
- [ ] 队列统计：`runtime.queue_tasks` 中 task id 属于 `relatedTaskIds` 的 count 之和；`@`、委派、指派、派遣、回复都统一按 queue_tasks 处理。
- [ ] 任务内优先级：working > failed/rework > queued > waitingHuman > returnPending > done > idle > released/closed > todo > notInvolved。
- [ ] 全局优先级：working > failed > queued > waitingHuman > idle > suspended/dead。
- [ ] 验证：`npx tsc --noEmit`；Node 临时脚本覆盖“本任务执行/本任务排队/无关任务排队/未参与/等待人工/完成/slice submitted/部分完成”。

### Task 2: 统一头像、点灯、指示灯 CSS

**Files:**
- Modify: `dev/frontend/src/components/RoleAvatar.tsx`
- Modify: `dev/frontend/src/styles/type-scale.css`
- Modify: `dev/frontend/src/styles/components.css`
- Modify: `dev/frontend/src/styles/pages.css`

- [ ] `RoleAvatar` 增加/替换 `tone?: StatusTone`，输出 `data-role-tone`；保留 `status` 作为过渡可选 prop 但不再由页面新增使用。
- [ ] `type-scale.css` 改为 `[data-role-tone="orange|yellow|red|green|gray"]` 五类；删除 `running/submitted/released/blue/amber` 等映射。
- [ ] `components.css` 保留唯一一份 `.role-live-dot`，只定义 `orange/yellow/red/green/gray`；删除 `amber/blue`。
- [ ] `pages.css` 删除重复 `.role-live-dot` 块和旧注释。
- [ ] `.status-light` 改为 `orange/yellow/red/green/gray` 五类；run 生命周期映射由 TS 转 tone。
- [ ] 验证：`rg "role-live-dot\.(amber|blue)|data-role-status"` 无残留（除兼容说明）；`npm run build`。

### Task 3: 任务卡与大厅实时会话

**Files:**
- Modify: `dev/frontend/src/pages/Lobby.tsx`
- Delete/Replace: `dev/frontend/src/taskCardStatus.ts`

- [ ] 任务卡 `TaskRoleAvatar` 调用 `taskRoleState`；`relatedTaskIds` = 当前 task.id + `tasks` 中 `parent_id/source_task_id === task.id` 的 id。
- [ ] 任务卡头像 `tone={state.tone}`，tooltip 用 `state.label` + `state.title`。
- [ ] 大厅实时会话卡调用 `globalRoleState`；头像和 `.status-light` 使用同一 `tone`；文本显示当前任务/run 状态。
- [ ] 删除 `taskRoleStatus.ts` 和 Lobby 内 `runStatusClass`/`runStatusLight`/`AVATAR_STATUS_LABEL`。
- [ ] 验证：`npx tsc --noEmit`；检查任务卡“未参与/排队中/进行中”三分支。

### Task 4: 任务内角色会话与工作状态卡片

**Files:**
- Modify: `dev/frontend/src/pages/RoleSessions.tsx`
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`

- [ ] `TaskDetail` 计算 `relatedTaskIds` = task.id + delegations.map(id) + dispatches.map(target_task_id)。
- [ ] `RoleSessions` 新增 `relatedTaskIds` prop；每行调用 `taskRoleState`，头像/点灯/徽标全部使用 `state.tone`，详情用 `state.detail`。
- [ ] `TaskDetail.roleLiveFor` 改为 `taskRoleState`；`renderRoleLive` 的 avatar/dot/badge 使用同一 tone；删除 `roleLiveState/roleLiveDot` 调用。
- [ ] 对话头像 `status={liveRole?.state.tone}`。
- [ ] 保留 queue 数、切片上下文、subagent 展示；task-scope queue 使用任务级 + 子任务 id。
- [ ] 验证：`npx tsc --noEmit`；确认 `@`/委派/指派子任务排队显示红色“排队中”，无关任务排队显示灰色“未参与”。

### Task 5: 全局角色页

**Files:**
- Modify: `dev/frontend/src/pages/AgentSessions.tsx`

- [ ] 顶部状态改用 `globalRoleState`：runtime busy → 橙；queue → 红；failed → 红；submitted/reviewing → 黄；alive → 绿；dead → 灰。
- [ ] 头像和 `status-light` 使用同一 tone；不再用 `runs.some(...)` 推导历史状态。
- [ ] 验证：历史 submitted run 不会再把当前空闲角色染黄。

### Task 6: Workflow slice/step

**Files:**
- Modify: `dev/frontend/src/workflowGroups.ts`
- Modify: `dev/frontend/src/workflowStatus.ts`
- Modify: `dev/frontend/src/pages/WorkflowGraph.tsx`
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`

- [ ] `STATUS_META` 增加 `tone`，`color` 改为 CSS token（`var(--...)`）。
- [ ] `groupStatus` 改为：failed > rejected > running > submitted > all done > pending。
- [ ] `workflowStatus.roleStatusFromWorkflow` 与 slice 优先级一致；返回 tone 供 TaskRoleState 使用。
- [ ] `WorkflowGraph`、TaskDetail slice/step 圆点和文字使用 tone 对应色；删除页面内 `statusClass` 重复映射。
- [ ] 验证：全部 submitted → 黄；部分完成 + pending → 灰；running → 橙；failed/rejected → 红。

### Task 7: 清理、验证、发布说明

**Files:**
- Modify: `README.md` / release notes（版本号待用户确认）
- Test: `dev/frontend` build

- [ ] 搜索并删除旧 `roleLiveState`/`roleLiveDot` 调用，或保留兼容壳但页面不再使用。
- [ ] `rg "role-live-dot|data-role-status|status-light|STATUS_META"` 检查所有消费方。
- [ ] `npx tsc --noEmit && npm run build`。
- [ ] 如后端未变，不跑全量 pytest；前端构建 + 状态脚本验证。
- [ ] 版本号与提交由用户确认后执行。
