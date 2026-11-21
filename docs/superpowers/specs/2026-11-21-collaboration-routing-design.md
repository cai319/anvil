# 协作方式判断与角色一致性设计（SDD）

> 状态：已批准（2026-11-21）
> 关联：`2026-11-10-slice-context-dispatch-design.md`、
> `2026-11-21-runtime-subagent-observability-design.md`

## 1. 目标

slice/step 角色在发现新工作/请求时，明确知道该：
自己做 / 用 subagent / mention / 建委派 / 建派遣 / @cindy / 只记录。
同时保证最终 workflow 评审后，**任务参与者与 workflow 角色一致**。

## 2. 四模式边界

| 模式 | 归属 | 谁执行 | 阻塞父 step | 跨 turn | 独立验收 | 结果/唤醒 |
|---|---|---|---|---|---|---|
| subagent | 运行时 | 同一角色 | 否 | 否（turn 级） | 否 | 同 turn 读取；不唤醒 |
| mention | Anvil 消息 | 另一角色 | 否 | 是 | 否 | 唤醒对方；无 child task |
| 委派 delegation | Anvil child task | 另一角色 | **是** | 是 | **是** | 完成后唤醒父 step；等 done/closed |
| 派遣 dispatch | Anvil child task | 另一角色 | 否 | 是 | 否（submit 即 done） | 不等待；回切仅原切片 running 时 |

## 3. 判定算法（Q1–Q5）

输入：工作 `W`；当前 step `S`；当前 slice `L`；workflow `WF`。

1. **Q1 workflow 已由其他步骤/阶段承担？**
   - 对应步骤已执行/执行中（running/submitted/approved/done，或角色已有该 slice 上下文）：
     - 本 step 需要结果 → mention 负责角色处理；
     - 不需要 → mention 知会/协调。
   - 对应步骤尚未启动（pending 且无该 slice 上下文）：
     - 本 step 需要结果 → 记录 workflow 缺口 + @cindy/@human；评估是否新增委派；
       委派后给该未来步骤写 context_note“已由委派处理，启动时先验收，不要重复”；
     - 不需要 → context_note 写入 task-wide 系统活动，不唤醒，后续步骤启动时带入上下文。
   - W 本来就是本 step instructions 的一部分 → 不算 covered，进入 Q2。
2. **Q2 本 step 的 submit/decide 依赖 W？（blocking）**
   - 是（不能 dispatch）：
     - 本角色能做 → 自己做；
     - 同角色只读并行 → subagent（hint+支持）否则串行；
     - 仅另一角色评审/挑战/澄清（无独立交付物）→ mention；
     - 另一角色独立产出并被验收 → 委派。
   - 否 → Q3。
3. **Q3 需要另一角色？** 否 → 自己做/记录，不建工作。
4. **Q4 需要独立上下文/持久 child task，且本 step 不等待？**
   - 是 → dispatch；否 → mention（评审/知会/协调）。
5. **需要改 workflow/角色/顺序 → @cindy；不可逆 → @human。**

逻辑示意图：

```
[W]
 ├─ Q1 WF 已有（其他）步骤承担？
 │    ├─ 是 → 对应步骤已执行/执行中？
 │    │      ├─ 是 → 需要结果 ? mention 处理 : mention 知会
 │    │      └─ 否 → 需要结果 ? 记录+@cindy/@human+评估委派 : context_note
 │    └─ 否 ↓
 ├─ Q2 本 step 依赖 W（blocking）？
 │    ├─ 是 → 自己能做? self : 同角色只读? subagent : 仅评审? mention : 独立验收? 委派
 │    └─ 否 ↓
 ├─ Q3 需要另一角色？否 → self/record
 ├─ Q4 独立上下文且不等待 → dispatch，否则 mention
 └─ 改 workflow/角色/顺序 → @cindy；不可逆 → @human
```

## 4. 角色一致性

- human 勾选/Roster 角色 = 必须参与的关键角色，不是上限；Cindy 必须把它们安排到强相关 slice/阶段。
- 其他 slice 由 Cindy 从系统现有角色中选择；现有角色可直接写入 workflow，最终审批 auto-add 为参与者。
- 新角色（系统中不存在）必须走 role_suggestion 并经 human 批准。
- 最终审批：
  - workflow 中已有、但不在 task_assignees 的现有角色 → 自动加入参与者（human 审批即确认；需 preview/confirm）。
  - task_assignees 中存在、但 workflow 未安排的角色 → 阻止审批。
  - loop/slices 与 flat 都要求最终 `workflow roles == task participants`。
- 模板 Meeple `role_slot` 只是编辑期占位；校验用编译后的真实 role slug。
- 不放开 `is_task_participant` / 委派 403；一致性由审批同步参与者保证。

## 5. 提示词

完整原文见 `dev/backend/anvil/graph/render.py` 的：
`COLLABORATION_PROTOCOL`、`DELEGATION_PROTOCOL`、`DISPATCH_PROTOCOL`、`SUBAGENT_PROTOCOL`。
`STEP_PROTOCOL` 组合顺序：COLLABORATION → DELEGATION → DISPATCH。
生成的 static/dynamic slice 步骤 instructions 追加上 `SLICE_COLLAB_RULE`（一次）。

## 6. 未来步骤上下文（context_note）

- 普通 comment 受当前 slice 作用域和“过滤自己发出”限制，不能可靠跨 slice。
- `tasks.context_note` 写 task-wide、system 作者（`slice_key=NULL`、`role_slug=NULL`）的
  comment 活动，payload 保留 author/for_step/body。
- `agent_inbox` 对任意切片都包含它；不产生 mention、不唤醒；后续步骤 prompt 的未读/上下文带入。
- CLI：`anvil task note <task-id> --for-step <step-id> "信息"`。

## 7. 死锁/异常状态

见计划文件“死锁与异常状态审视”表，必须全部有回归测试：
同角色委派、孤儿委派、execution 作用域、父任务 cancel、角色禁用、wake 丢失/HITL 绕过、
审批同步非原子、未来步骤重复、context_note 误判等。

## 8. 验收标准

- 所有关键逻辑有先红后绿的测试；全量 pytest + 前端 build 通过。
- 提示词评测：15 个场景，critical 100%，总体 ≥90%（`scripts/collaboration_eval.py`）。
- 无死锁/异常状态回归。
- 真实 slice 运行后 DB 计数符合预期（dispatch/delegation 不滥用，mention 不塌缩）。
