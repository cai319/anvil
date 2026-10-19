# Inbox 信息架构重构 — 设计规格（spec）

**日期**：2026-10-19。**状态**：owner 已批准（含布局选型 B：独立收件箱页面）。
**参考**：raft.build Activity feed / action card / 状态灯语义；`optimization-roadmap.md` §业界实践补充。

## 目标

把分散在各页面的四类人机决策点统一为「待我处理」一等入口：评审请求、@human 提及、待审工作流草案、待审提案（lessons + proposals）。每条为 action card——能就地操作的不跳页。

## 范围

**做**：`/inbox` 独立页面（导航首位 + 待办徽章）、后端只读聚合端点、看板 in_review 卡高亮直达、状态灯图例（灰点=空闲≠故障，design.md §11.1 未兑现项）。
**不做**：大厅右栏活动流（保持不动）、Inbox 内嵌回复框（提及跳转任务详情回复）。

## 后端

### `GET /api/v1/inbox`（新，只读）

```json
{
  "count": 5,
  "items": [
    {"id": "review:T-xxx", "kind": "review", "title": "任务标题",
     "detail": "developer 已提交 · 等你通过或驳回", "task_id": "T-xxx", "created_at": "..."},
    {"id": "mention:T-xxx:<activity_id>", "kind": "mention", "title": "PM：……",
     "detail": "需要你回复后 agent 才继续", "task_id": "T-xxx", "created_at": "..."},
    {"id": "draft:<draft_id>", "kind": "draft", "title": "工作流草案 — <goal>",
     "detail": "by agent:pm · N 个子任务", "task_id": "T-xxx|null", "created_at": "..."},
    {"id": "lesson:<id>", "kind": "lesson", "title": "经验内容摘要",
     "detail": "by <role> · 标签 …", "task_id": null, "created_at": "..."},
    {"id": "proposal:<id>", "kind": "proposal", "title": "提案字段摘要",
     "detail": "by <role> · 依据/风险", "task_id": null, "created_at": "..."}
  ]
}
```

- items 按 `created_at` 倒序；`count = len(items)`。
- **聚合口径**：
  - `review`：`tasks WHERE status='in_review'`；detail 含已提交角色（role_claims 中 submitted/approved）。
  - `mention`：`activities WHERE kind='mention' AND json target='human'`，且其后该任务**无** human 的 comment/review（已处理判定）；每任务只取最近一条。
  - `draft`：`board_drafts WHERE status='pending'`；detail 含子任务数与 created_by。
  - `lesson`：`lessons WHERE status='pending'`。
  - `proposal`：`proposals WHERE status='pending'`。
- 操作不在此端点——前端复用现有 API：`POST /tasks/{id}/review`、`POST /board/drafts/approve|reject`、lessons 更新、`POST /proposals/{id}/approve|reject`。
- 鉴权与现有读端点一致（本地 session 轻校验）。

### 实现

- 新 `backend/anvil/services/inbox.py`：`collect(conn) -> dict`（纯查询）。
- 新 `backend/anvil/api/inbox.py`：`GET /api/v1/inbox`。
- `main.py` 挂路由。

## 前端

- **`pages/Inbox.tsx`**（新）：
  - tab 分区：全部 / 评审 / @我 / 草案 / 提案（计数徽章）。
  - 条目卡片：kind 徽章（配色区分）+ title/detail + 操作按钮。
  - 就地操作：评审（通过/驳回）、草案（批准落板/驳回）、lesson（批准/拒绝）、proposal（批准/拒绝）；操作后从列表消失（乐观更新 + 重取）。
  - 提及：单按钮「去回复」→ 跳 `/tasks/{task_id}`。
  - SSE 订阅：task / 其他相关 topic → 重取。
- **`App.tsx`**：NAV 首位 `{href: "/inbox", label: "收件箱"}` + 徽章（组件级 state，SSE 后重取 count）；路由加 inbox。
- **`Lobby.tsx`**：
  - 看板 `in_review` 卡：新增「等人类评审」高亮样式（左侧色条 + 徽章），徽章点击跳 `/inbox`。
  - 角色栏底部图例行：「● 运行中 ○ 空闲（非故障）」。
- **`client.ts`**：`getInbox()`；InboxItem 类型；复用现有 review/approveDraft/lesson/proposal 方法。

## 测试

- 后端 TDD：`tests/api/test_inbox.py`
  - 空库 → `{count: 0, items: []}`。
  - in_review 任务出现 review 条目；review 通过后消失。
  - @human 提及出现 mention 条目；该任务其后有 human 评论 → 消失；每任务只一条（旧的被新的替代）。
  - pending 草案/lesson/proposal 各出现；批准后消失。
- 前端：`npm run build` 通过。
- 手工验收（owner）：创建 @human 提及 + 提交任务 + 产出草案 → Inbox 全链路 + 徽章 + 看板高亮。

## 验收标准

1. 打开 `/inbox` 能看到四类条目并就地完成 通过/驳回/批准/拒绝。
2. 导航徽章数 = 待处理数，SSE 实时更新。
3. 大厅 in_review 卡有「等人类评审」高亮，点击直达 Inbox。
4. 角色卡有状态灯图例。
5. `python3 -m pytest tests/ -q` 全绿（在 138 passed 基础上新增）。
