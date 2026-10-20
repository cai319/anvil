# Anvil × Raft.build 功能差异与优化建议

**日期**：2026-10-19（P0 已于同日修复，见 `implementation-status.md`）
**对照材料**：`/tmp/raft-build-research.md`（Raft 官网/docs/博客一手调研）、《Anvil 实现状态》（`implementation-status.md`）与当前源码。
**结论一句话**：Anvil 在**结构化交付**（工作流、依赖、gate、diff/scope、成本闸门）上比 Raft 更硬；Raft 在**协作世界与上下文工程**（Inbox 拉模式、Held Draft、Constructed Initial Context、验证矩阵、action card）上明显更成熟。后续优化应优先补齐「上下文经济」与「信任/边界」两类能力，而不是照搬聊天产品形态。

---

## 一、定位与边界

| 维度 | Anvil | Raft.build |
|---|---|---|
| 形态 | 本地单用户多 Agent 编排器（FastAPI + SQLite + LangGraph） | 多机、多人 + Agent 的团队工作区（频道/线程/任务） |
| 执行体 | 本机 CLI Agent（Claude Code 等），角色常驻会话 | 本地 Computer daemon 驱动用户已有 CLI Agent |
| 工作单元 | **Task（高层工作空间）+ Workflow（步骤状态机）** | **Task = 消息 + 编号 + 状态 + 单 owner**，天然挂 thread |
| 协作方式 | Cindy 计划先行 → DAG 步骤 → gate → 最终评审 | 聊天中 @mention/claim 自组织；大任务先提议拆解、人批准 |
| 人的角色 | 定目标、审批工作流/步骤、终审、AI 生成角色 | 定方向、评审、终审；action card 执行管理操作 |
| 不在范围 | 多机/多人/跨组织、生产发布 | 代码 diff/合并、按角色私有 Git worktree、成本闸门 |

> Anvil 不应为了「像 Raft」而引入多频道/多服务器/Joint Channel；这些属于团队产品形态，与单机单人目标冲突。

---

## 二、功能差异矩阵

图例：✅ 已具备；🟡 部分/弱；❌ 缺失。

### 2.1 Agent 身份与运行时

| 能力 | Anvil | Raft | 判断 |
|---|---|---|---|
| 持久身份（跨会话/重启） | ✅ role + home + memory + `role_sessions` | ✅ Agent 成员 | 持平 |
| 运行时解耦、可换模型 | ✅ YAML 运行时（claude/codex/gemini） | ✅ 多 runtime 混用 | 持平 |
| 常驻进程 / 空闲释放 | ✅ stream-json 常驻 + 15min 空闲释放 + `--resume` | ✅ idle/active 生命周期 | 持平（Anvil 实现更新） |
| 三档重置 | ✅ restart / session-reset / full-reset | ✅ 同三档 | 持平 |
| 冷唤醒上下文重建 | ❌ 直接 `--resume`，无会话阈值/重建策略 | ✅ Constructed Initial Context（5 block，成本 −60%） | **Raft 明显领先** |
| 记忆保鲜（Cleaner） | 🟡 周复盘 reminder + lessons 80 上限；无 MEMORY.md 体积监控 | ✅ 本地每小时检查 + 自修剪 | Raft 领先 |
| 名字 vs 角色 | 🟡 只有 role slug/display_name，无「具名实例」语义 | ✅ name=带历史的 instance，role=schema | Raft 领先 |

### 2.2 任务与协作

| 能力 | Anvil | Raft | 判断 |
|---|---|---|---|
| 显式工作流/DAG | ✅ Cindy 生成、gate、依赖、同角色串行、图谱 | ❌ 无显式工作流（靠 agent 协商/phase 标签） | **Anvil 领先** |
| 计划先行 | ✅ 自动 Cindy → 草案 → 人批准 → 执行 | 🟡 官方推荐「大目标 → agent 提议拆解 → 人批准」 | Anvil 更机制化 |
| 认领/防重复 | ✅ claim/lease（临时任务）；工作流步骤由引擎调度 | ✅ claim 抢占 | 持平（模型不同） |
| 任务内实时交互 | ✅ 任务会话（评论/@/运行事件/筛选） | ✅ thread + 频道 + DM + activity feed | Raft 通信面更丰富 |
| 线程/频道/DM | ❌ 单任务时间线；有角色直发消息 API，无 UI | ✅ channel/thread/DM | Raft 领先 |
| Activity feed（人） | 🟡 Inbox（待办）+ 活动历史（全量筛选） | ✅ All/Unread/Mentions/Saved | Raft 略领先 |
| Agent Inbox（拉模式） | ❌ 消息/反馈直接 push 进步骤提示词 | ✅ 可查询 inbox + cursor 两阶段提交 | **Raft 明显领先** |
| 发送冲突检测（Held Draft） | ❌ 无版本标记/四选一 | ✅ held + revise/as-is/silent/override | **Raft 明显领先** |
| 消息幂等/静音/送达恢复 | ❌ 无 idempotencyKey/mute/mentions.pending | ✅ SDK 原语 | Raft 领先 |

### 2.3 上下文工程

| 能力 | Anvil | Raft | 判断 |
|---|---|---|---|
| 注入预算 | ❌ 步骤提示词附最近 24 条会话，无 token 上限/截断日志 | ✅ 按 token 预算设计（CIC ~10K） | **Raft 领先** |
| 记忆注入 | ✅ MEMORY.md + FTS notes + 标签匹配 lessons | ✅ MEMORY 头 + 文件/消息块 | 持平（Anvil 有 FTS） |
| 冷启动重建 | ❌ | ✅ 5 block + 三条负规则 | Raft 领先 |
| Context rot 防护 | 🟡 常驻会话持续增长，无 compaction 策略 | ✅ 会话阈值 + 新建会话 + 后台进程检查 | Raft 领先 |
| AX（Agent Experience）设计 | 🟡 CLI 契约 + 协议文本 | ✅ AX 四问 + 信息/动作配对 | Raft 领先 |

### 2.4 质量、评审与信任

| 能力 | Anvil | Raft | 判断 |
|---|---|---|---|
| 人工 gate | ✅ 工作流 gate + 任务终审 + 收件箱 | ✅ review + 终审 | 持平 |
| builder ≠ verifier | 🟡 协议要求交叉评审，但**未强制**；gate 由人审 | ✅ 硬规则 + 验证矩阵 | **Raft 领先** |
| 契约先行 | ❌ 无 step contract/invariants | ✅ contract-first，决策落文本契约 | Raft 领先 |
| 验证矩阵 | ❌ 只有 acceptance 列表 | ✅ 功能路径/property/trace/持门人四类格子 | Raft 领先 |
| 信任状态 vs 代码评审 | ❌ 仍以 diff/评审为主要信号 | ✅ 读系统信号（bug 聚类、mutation、碰撞） | Raft 领先 |
| 失败归因 | 🟡 技术分类（timeout/exit）；无输入归因 | ✅ 失败指向不干净输入（spec/契约/测试面） | Raft 领先 |
| diff/scope 越界 | ✅ worktree + scope + `.anvilignore` | ❌ 不管理代码合并 | **Anvil 领先** |

### 2.5 HITL 与安全边界

| 能力 | Anvil | Raft | 判断 |
|---|---|---|---|
| 待办聚合/就地操作 | ✅ Inbox（评审/@我/草案/提案/审核门） | ✅ Activity + action card | 持平（形态不同） |
| 危险操作硬边界 | ❌ 评审/删除等 API 未强制 human-only（见 P0） | ✅ 生产按钮 agent 直接 error；agent ≤ Admin | **Raft 领先（关键）** |
| Action card（agent 准备、人执行） | ❌ | ✅ 一等公民 UI 原语 | Raft 领先 |
| 权限模型 | 🟡 本地 token + 角色/运行级 token；无账号体系 | ✅ Server Admin/Owner | 单用户场景可接受 |

### 2.6 观测、成本与自动化

| 能力 | Anvil | Raft | 判断 |
|---|---|---|---|
| 运行日志/生命周期 | ✅ 8 态 + 失败分类 + 日志文件 | ✅ execution log + 回放 | 持平 |
| 成本 | ✅ 四层闸门 + 每 run 成本 + 按角色/日聚合 | ✅ usage analytics | Anvil 更细 |
| 质量信号/评测 | ❌ | ✅ trace coverage、mutation、DAA | Raft 领先 |
| Reminder | ✅ cron/at/when，Agent 可自建 | ✅ 锚定消息/thread，可 snooze | 持平 |
| 自演化 | ✅ proposals/versions/lessons/instruction | ✅ 自改 description + 周审 | 持平 |

---

## 三、Anvil 已经优于 Raft 的部分（保持，不要照搬掉）

1. **Workflow-first 交付**：显式 DAG、gate、同角色串行、失败重试、图谱可视化——比聊天式自组织更可预测，尤其适合「交付一个软件」。
2. **共享 Git 工作区**：worktree 隔离、scope/diff 越界校验、提交时强制检查——Raft 不做代码合并。
3. **成本治理**：角色/运行时/系统/日成本四层闸门 + 每 run 真实成本。
4. **本地隐私**：数据与订阅都在本机；这是产品前提。
5. **角色 AI 生成**：一句话生成能力/定位/职责/系统提示，降低角色搭建成本。
6. **运行可靠性**：常驻进程复用、空闲释放 + resume、停机自愈、失败分类。

---

## 四、关键差距与优化建议（按优先级）

### P0 · 必须先修（正确性/安全）

#### P0-1 人工审批必须硬性 human-only
现状：`POST /tasks/{id}/review`、`POST /workflows/{id}/steps/{id}/review`、`DELETE /tasks/{id}` 只校验会话，不校验角色——**Agent 的角色令牌可以代替人通过评审/删除任务**，违反项目 HITL 规则与 Raft 的硬边界原则。
建议：
- 新增 `require_human(auth)`：`auth.role` 非空且不是 `"human"` → 403；应用到任务评审、gate 审核、删除任务、board 草案批准、lesson/proposal 批准。
- 测试覆盖：用角色 token 调用这些端点应返回 403。

#### P0-2 builder ≠ verifier（至少对 gate 强制）
建议：
- `workflow_steps` 增加 `reviewer`（Cindy 规划时指定，默认与被审步骤不同角色）；或新增 step 类型 `verification`，由引擎保证 `verifier != implementer`。
- gate 仍由人做最终裁决，但要求先有跨角色验证证据（验证步骤 approved）才能进入 gate。
- 前端图谱显示 reviewer/验证步骤。

#### P0-3 上下文注入预算
现状：步骤 run 每次注入最近 24 条会话，长任务会膨胀。
建议：
- 按 token 预算（如身份 800 / 步骤 600 / 会话 1200 / 共享 500 / 协议 400）截断，记录截断事件；优先保留 @ 本角色的消息与最近动作。

### P1 · 上下文经济与协作原语（1–2 周）

#### P1-1 Agent Inbox（拉模式）
- 新增 `role_inbox`（role, task_id, kind, payload, delivered_at/committed_at, cursor）或由 activities 派生。
- 唤醒消息只给指针（「有 N 条新消息 / @ 你」），agent 用 `anvil inbox check` 拉取、`anvil inbox commit` 提交 cursor（两阶段，at-least-once）。
- 步骤提示词只带当前步骤 + inbox 指针；反馈不再全量 push，减少上下文污染与 token。
- 与现有「@ 唤醒 + 最近会话注入」并存，逐步替换。

#### P1-2 Held Draft（简化版）
- 发送评论/@ 时携带 `expect_activity_id`（或 task version）；若之后已有新事件，返回 `held` + 新增内容摘要；agent 选择 `revise / send-as-is / silently-drop / force`。
- 直接解决多角色同时响应/互相覆盖；「沉默」是合法结果。

#### P1-3 Constructed Initial Context（冷唤醒）
- 跟踪会话 token（`result.usage`）；空闲释放后若会话超阈值（如 128K）→ 不用 `--resume`，按 5 block 组装 ~10K 首输入：MEMORY 头 / 最近 25 条消息 / 近 2h 文件（旧文件只列名）/ 最近 12 条 action / 唤醒消息。
- 三条负规则：不宣布重置、不列未决问题、>2h 只给指针。

#### P1-4 具名 Agent 与反馈粘性
- role 增加 `name`（带历史的人名实例），路由与反馈归因到 name；role 仅作 schema。
- 周复盘 prompt 增加「检查 description 是否仍准确并更新」；纠正通过 lessons/instruction 落到具名 agent。

### P2 · 质量与信任体系（2–3 周）

#### P2-1 契约先行
- workflow step 增加 `contract`（不变量/接口承诺），下游步骤自动注入；gate checklist 对照 contract。
- Cindy 规划时要求每个实现步骤写 contract，验证步骤写「如何证明契约未被违反」。

#### P2-2 验证矩阵
- 任务级 verification plan：功能路径 / property / trace 覆盖 / 回归；每格具名验证者与方法。
- 模板化到 Cindy 的 workflow；gate 需矩阵中关键格子通过。

#### P2-3 失败归因与信号
- 步骤失败分类扩展为 `spec_ambiguity / contract_gap / test_surface / environment / agent_error`；要求人修输入而非反复重跑。
- 指标页增加：失败聚类、同 seam 碰撞、每步骤成本/时长、每角色活跃度（DAA 类）。

#### P2-4 Action Cards 与硬边界清单
- `anvil action prepare`（合并、部署、建仓、改成员等）→ 收件箱卡片 → 人一键以自己身份执行。
- 硬边界：发布/合并/删除/审批对 agent token 直接 403（与 P0-1 合并实现）。

### P3 · 长期能力

- Cleaner：MEMORY.md 体积本地监控 + 限频自修剪。
- Trace coverage / mutation testing 接入。
- CIC 行为保真评测（多模型 judge 盲评）。
- Activity feed 的 Unread/Saved；DM UI（如果需要）。
- 不引入频道/多服务器/Joint Channel（超出单机单人范围）。

---

## 五、建议的落地顺序

```
P0-1 human-only 审批（安全，立即）
P0-2 gate 的 builder≠verifier + P0-3 注入预算
P1-1 Agent Inbox 拉模式
P1-2 Held Draft（简化）
P1-3 CIC 冷唤醒 + token 监控
P1-4 具名 Agent / 反馈粘性
P2 契约先行 / 验证矩阵 / 信号看板 / action cards
P3 Cleaner / trace / mutation / judge 评测
```

每一步都应配一个真实任务回归（当前「跳棋」工作流是现成用例），并优先保持「工作流可预测」这一 Anvil 的核心优势，不被聊天式协作稀释。

---

## 附：主要来源

- Raft 调研报告：`/tmp/raft-build-research.md`（含 docs.raft.build、官方博客与用例链接）
- 本项目：`design.md`、`dev/implementation-status.md`、`dev/optimization-roadmap.md`
