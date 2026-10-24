# 多 Agent Workflow 模板化与人工修订系统设计方案

> 版本：v1.0 ｜ 适用范围：AI 自动生成 Workflow → 人工审阅修订 → 模板发布 → 运行时执行（本机 Agent Runtime）

---

## 目录

1. 背景与目标
2. 业界参考与设计原则
3. 核心概念模型
4. Workflow DSL（状态机定义）详细设计
5. 角色（Role）与运行时（Agent Runtime）绑定设计
6. 后端架构与详细设计
7. 执行引擎（状态机解释器）设计
8. AI 生成 → 人工修订的闭环设计
9. 前端架构与页面设计
10. 人工操作流程（Operator Playbook）
11. 校验规则（Lint / Validation）
12. 版本、发布与回滚
13. 可观测性、安全与治理
14. 数据库表结构
15. API 清单
16. 实施路线图
17. 附录：完整 DSL 示例、伪代码

---

## 1. 背景与目标

### 1.1 现状
- Multi-Agent 项目中的 Workflow 已由 AI（Planner Agent）自动生成，内部包含多节点、分支、循环（loop）。
- 自动生成的流程不可直接信任，需要人工审阅、修订后再发布为**模板**供复用。
- 节点绑定的不是某个具体 agent 实例，而是**角色类型**，角色类型在运行时解析为本机 Agent Runtime 中的具体实现。

### 1.2 目标
| 目标 | 说明 |
|---|---|
| 模板化 | Workflow 以可序列化的 DSL 描述，与具体运行实例解耦，可版本化、可复用、可参数化 |
| 人工可修订 | 人可以在可视化画布上增删改节点、连线（过渡）、条件、循环边界、角色绑定 |
| 状态机语义 | 执行语义清晰：节点=状态，边=过渡，条件=guard，支持 loop、并行、人工介入 |
| 可运行 | 修订后的模板可直接实例化执行，节点由角色类型解析到本机 agent runtime |
| 安全可控 | 循环上限、预算、审批门控、校验规则防止 AI 生成的“坏流程”上线 |
| 可追溯 | AI 版本 / 人工修订版本 / 发布版本全程 diff、审计、回滚 |

### 1.3 非目标
- 不做通用 BPM 平台（不追求完整 BPMN 2.0 覆盖）。
- 不处理跨机器分布式 Agent 调度（预留扩展点，当前只绑定本机 runtime）。

---

## 2. 业界参考与设计原则

### 2.1 参考对象
| 来源 | 借鉴点 |
|---|---|
| **LangGraph** | 图 = 节点 + 条件边；共享 State；Checkpointer 持久化；`interrupt()` 实现 human-in-the-loop；子图（subgraph）复用 |
| **Temporal / Durable Execution** | 事件溯源、确定性重放、Activity 重试策略、Signal/Query 介入运行中的流程 |
| **XState / SCXML** | 分层状态机、guard/action、显式 transition、可视化编辑器 Stately |
| **BPMN 2.0** | Gateway（排他/并行/包容）、Event、SubProcess、Boundary Event（超时/异常边界）等成熟建模词汇 |
| **Dify / n8n / Flowise / Coze** | 画布编辑体验：节点面板、属性侧栏、变量引用 `{{node.output.x}}`、单节点调试、试运行 |
| **Anthropic《Building Effective Agents》** | Orchestrator-Workers、Evaluator-Optimizer（天然 loop）、Routing；“优先最简单方案”“让 agent 可被观测” |
| **Claude Code Subagent** | 子任务在干净上下文中执行，只回传摘要；主流程保留决策权 |
| **Argo / GitHub Actions** | 声明式 YAML DSL + 模板参数 + 版本固定（pin） |
| **React Flow (xyflow)** | 前端画布事实标准 |

### 2.2 设计原则
1. **DSL 是唯一真相源（Single Source of Truth）**：画布只是 DSL 的投影；AI 生成、人工修改、执行引擎全部面向同一份 DSL。
2. **模板（Template）与实例（Instance）分离**：模板不可变（immutable 版本），实例引用模板版本 + 输入参数。
3. **节点绑定角色而非实现**：`roleType` → 运行时通过 Registry 解析 → 本机 agent runtime。模板可移植。
4. **显式优于隐式**：所有分支、循环、退出条件必须显式建模，禁止“agent 自己决定跳哪里”而不经过 Router 节点。
5. **可中断、可恢复**：每个节点完成即 checkpoint；人工审批、超时、失败均可从断点恢复。
6. **安全默认值**：循环必须有 `maxIterations`；并行必须有 `maxBranches`；节点必须有超时与预算。
7. **AI 生成的内容是“草稿”**：必须经过 Lint + 人工审核 + 发布流程才能被实例化。

---

## 3. 核心概念模型

### 3.1 概念总览

```
WorkflowTemplate (模板, 带版本)
 ├── metadata (名称/描述/标签/作者/来源: ai|human)
 ├── inputs / outputs schema
 ├── variables (全局状态 schema)
 ├── nodes[]  ──绑定──▶ RoleType ──Registry解析──▶ AgentRuntime(本机实现)
 ├── edges[]  (过渡: source → target, guard 条件, 优先级)
 ├── policies (预算/超时/循环上限/审批策略)
 └── layout (画布坐标, 仅前端用)

WorkflowInstance (实例, 运行态)
 ├── templateVersionId
 ├── state (全局变量当前值)
 ├── currentNodes[] (支持并行: 活跃 token 集合)
 ├── nodeRuns[] (每个节点的每次执行记录, loop 会多次)
 ├── events[] (事件溯源日志)
 └── status: pending|running|waiting_human|paused|completed|failed|cancelled
```

### 3.2 节点类型（Node Types）

| 类型 | 说明 | 关键配置 |
|---|---|---|
| `start` | 唯一入口 | 输入映射 |
| `end` | 终止（可多个，带结果码 success/fail） | 输出映射 |
| `agent` | 由某个 **RoleType** 的 agent 执行 | roleType, promptTemplate, tools 白名单, outputSchema, timeout, budget, retry |
| `tool` | 直接调用确定性工具/函数（无 LLM） | toolId, params 映射 |
| `router` | 条件分支（排他网关）。条件由表达式或 LLM 分类器决定 | mode: `expression` / `llm_classifier`; 出边带 guard |
| `parallel` | 并行 fan-out（并行网关） | branches, maxBranches |
| `join` | 汇聚（等待 all / any / n-of-m） | joinMode, timeout |
| `loop` | 显式循环容器（含 body 子图） | loopMode: `while` / `for_each` / `until`; maxIterations; exitCondition |
| `human` | 人工介入（审批/补充信息/选择分支） | formSchema, assignees, slaTimeout, escalation |
| `subworkflow` | 引用另一个模板版本 | templateRef, inputMapping, outputMapping |
| `spawn` | 动态生成子任务/子代理（Orchestrator-Workers） | plannerRole, workerRole, maxDepth, maxBreadth, aggregator |
| `wait` | 等待外部事件/定时 | eventType, timeout |

> 循环有两种建模方式，二选一或并存：**(a) 回边（back-edge）**：允许 edge 指向祖先节点，engine 对回边计数并强制 `maxIterations`；**(b) `loop` 容器节点**：body 作为子图。推荐：**AI 生成时允许回边（更自然），Lint 阶段提示人工“折叠为 loop 容器”**；两者执行语义等价，容器更利于可视化和设定边界。

### 3.3 边（Edge / Transition）

```ts
interface Edge {
  id: string;
  source: string;            // nodeId
  sourceHandle?: string;     // router 的分支出口名 / 节点的 success|failure|timeout 出口
  target: string;
  guard?: Guard;             // 条件；无则无条件过渡
  priority: number;          // 同源多条边的求值顺序（排他时第一个为真者胜）
  isDefault?: boolean;       // 兜底边（else）
  kind: 'normal' | 'back' | 'error' | 'timeout';  // back = 回边(循环)
  label?: string;            // 人类可读
  maxTraversals?: number;    // 对回边：最大经过次数
}

type Guard =
  | { type: 'expression'; expr: string }     // JSONLogic / CEL / JS 子集, 如 state.score < 0.8
  | { type: 'llm'; prompt: string; options: string[] } // 语义路由
  | { type: 'always' };
```

**出口（Handles）约定**：每个可执行节点默认拥有 `success`、`failure`、`timeout` 三个出口；未连 `failure`/`timeout` 时走节点级默认策略（retry → 失败则整个实例 fail）。这对应 BPMN 的 Boundary Event。

### 3.4 状态（State / Variables）

- 全局 `state` 为 JSON 对象，有 Schema（JSON Schema）。
- 节点输出通过 **输出映射** 写入 `state`，例如 `state.draft = {{nodes.writer.output.text}}`。
- 变量引用语法统一为 `{{ ... }}`，支持 `inputs.*`、`state.*`、`nodes.<id>.output.*`、`loop.index`、`loop.item`。
- 并行分支使用 **reducer** 合并（append / merge / last-write-wins），借鉴 LangGraph 的 channel reducer。

### 3.5 模板与实例的关系

```
Template v3 (published) ──instantiate(inputs)──▶ Instance #1024
                       ──instantiate(inputs)──▶ Instance #1025
Template v4 (draft, from AI)  ──human edit──▶ v4 (review) ──approve──▶ v4 (published)
```
- 正在运行的实例**固定**其模板版本；新版本不影响在途实例（可选提供“迁移”能力，默认不做）。

---

## 4. Workflow DSL 详细设计

### 4.1 顶层结构（YAML/JSON 等价）

```yaml
apiVersion: workflow/v1
kind: WorkflowTemplate
metadata:
  id: wf_content_pipeline
  name: 内容生产流水线
  version: 4
  status: draft            # draft | in_review | published | deprecated
  origin: ai               # ai | human | ai+human
  generatedBy: planner-agent@2026-10-20
  tags: [content, review-loop]
spec:
  inputs:
    type: object
    properties:
      topic: { type: string }
      maxRounds: { type: integer, default: 3 }
    required: [topic]
  outputs:
    properties:
      finalArticle: { type: string }
  state:
    properties:
      draft: { type: string }
      review: { type: object }
      round: { type: integer, default: 0 }
  policies:
    maxTotalTokens: 200000
    maxWallClockSec: 1800
    defaultNodeTimeoutSec: 300
    defaultRetry: { maxAttempts: 2, backoffSec: 5 }
    requireHumanApprovalBeforePublish: true
  nodes:
    - id: start
      type: start
    - id: writer
      type: agent
      role: { type: "content.writer", minCapabilities: [long_form] }
      config:
        prompt: |
          主题：{{inputs.topic}}
          {{#if state.review}}上一轮审稿意见：{{state.review.comments}}{{/if}}
        tools: [web_search]
        outputSchema: { type: object, properties: { text: {type: string} } }
        timeoutSec: 400
        budget: { maxTokens: 30000 }
      outputMapping:
        state.draft: "{{output.text}}"
        state.round: "{{state.round + 1}}"
    - id: reviewer
      type: agent
      role: { type: "content.reviewer" }
      config:
        prompt: "审阅以下稿件并给出 score(0-1) 与 comments：{{state.draft}}"
        outputSchema: { type: object, properties: { score: {type: number}, comments: {type: string} } }
      outputMapping:
        state.review: "{{output}}"
    - id: gate
      type: router
      config: { mode: expression }
    - id: approve
      type: human
      config:
        form:
          fields:
            - { name: decision, type: enum, options: [approve, reject] }
            - { name: note, type: text }
        assignees: { roles: [editor] }
        slaSec: 86400
        onTimeout: escalate
    - id: end_ok
      type: end
      config: { result: success }
      outputMapping: { outputs.finalArticle: "{{state.draft}}" }
    - id: end_fail
      type: end
      config: { result: failure }
  edges:
    - { id: e1, source: start, target: writer }
    - { id: e2, source: writer, sourceHandle: success, target: reviewer }
    - { id: e3, source: reviewer, sourceHandle: success, target: gate }
    - id: e4
      source: gate
      target: approve
      priority: 1
      guard: { type: expression, expr: "state.review.score >= 0.8" }
      label: "质量达标"
    - id: e5
      source: gate
      target: writer
      kind: back
      priority: 2
      maxTraversals: "{{inputs.maxRounds}}"
      guard: { type: expression, expr: "state.round < inputs.maxRounds" }
      label: "返修"
    - { id: e6, source: gate, target: end_fail, isDefault: true, label: "超出轮数" }
    - id: e7
      source: approve
      target: end_ok
      guard: { type: expression, expr: "nodes.approve.output.decision == 'approve'" }
    - { id: e8, source: approve, target: writer, kind: back, isDefault: true, maxTraversals: 2 }
layout:
  nodes: { start: {x: 0, y: 0}, writer: {x: 200, y: 0}, ... }
```

### 4.2 表达式语言选型
- 推荐 **CEL（Common Expression Language）** 或 **JSONLogic**：无副作用、可沙箱、可静态校验、前后端可同构实现。
- 禁止任意 JS `eval`。
- 前端提供表达式编辑器 + 变量补全 + 即时求值预览（基于“模拟 state”）。

### 4.3 DSL 的 JSON Schema
为 DSL 提供严格 JSON Schema（`workflow.schema.json`），AI 生成、前端保存、后端入库三处共用同一份 Schema 校验。

---

## 5. 角色与运行时绑定设计

### 5.1 三层解耦

```
节点 (Node)
  └─ role.type: "content.reviewer"          ← 模板里只写角色类型
        │
        ▼ RoleRegistry.resolve(roleType, constraints, context)
RoleType 定义 (RoleCatalog)
  ├─ id: content.reviewer
  ├─ description / capabilities / inputSchema / outputSchema
  ├─ defaultPrompt / defaultTools / defaultModelTier
  └─ implementations[] ──▶ AgentRuntime 实现 (本机)
                             ├─ impl: reviewer-v2 (python, in-process)
                             ├─ impl: reviewer-fast (cheap model)
                             └─ selector: by version / by capability / by cost tier
```

### 5.2 RoleType 定义

```yaml
kind: RoleType
id: content.reviewer
name: 审稿人
capabilities: [critique, scoring]
contract:
  input:  { type: object, properties: { draft: {type: string} } }
  output: { type: object, properties: { score: {type: number}, comments: {type: string} }, required: [score] }
defaults:
  modelTier: standard
  tools: []
  systemPrompt: "你是一名严谨的审稿人..."
implementations:
  - id: reviewer-v2
    runtime: local        # 本机 agent runtime
    entry: agents.reviewer:ReviewerAgent
    version: 2.1.0
    isDefault: true
  - id: reviewer-fast
    runtime: local
    entry: agents.reviewer_fast:FastReviewer
    costTier: low
```

### 5.3 本机 Agent Runtime 接口（统一契约）

```python
class AgentRuntime(Protocol):
    role_type: str
    def run(self, ctx: NodeContext) -> NodeResult: ...
    # NodeContext: inputs(已渲染 prompt/变量), tools, budget, deadline, cancel_token, trace
    # NodeResult: status(success|failure|needs_human), output(dict, 按 outputSchema), usage, logs, artifacts[]
```

- **约束**：runtime 不得自行决定“下一个节点”；只能返回结果 + 可选 `suggestedBranch`（供 router 的 llm 模式参考）。
- **解析策略**：节点 `role.selector` 可指定 `implId`（硬绑定）、`capability`（软匹配）、`costTier`。模板一般只写 `type`，环境配置（Environment Binding）在发布/实例化时决定具体 impl，便于同一模板在 dev/prod 用不同实现。

### 5.4 环境绑定（Environment Binding）
```yaml
kind: EnvironmentBinding
env: prod
bindings:
  content.writer:   { impl: writer-v3 }
  content.reviewer: { impl: reviewer-v2 }
```
模板 + 绑定 = 可执行配置。人工在页面上既能改模板中的角色类型，也能在“环境绑定”页单独改实现映射。

### 5.5 Meeple：模板中的角色契约

Anvil 模板不直接把某个具体角色写死在步骤里，而是使用 **Meeple**。

> **Meeple 不是角色，而是一类角色的代表。**
> 它描述“这里需要一个什么样的角色”，具体由哪个系统角色来做，在套用模板或任务规划时决定。

这样设计的原因：

- 模板需要跨任务复用，不能假设任务里一定有同名角色；
- 具体角色可能带有定制 skill、工具权限或运行时配置；
- 同一类角色在不同任务中可能有不同实现，但工作流结构不变；
- 模板可以用 Meeple 表达职责，由 Cindy 或人类在运行时完成角色匹配。

#### 5.5.1 Meeple 数据结构

```json
{
  "key": "role_tester",
  "label": "测试工程师",
  "role": "tester",
  "bound_role": "sec-tester"
}
```

字段说明：

| 字段 | 必填 | 含义 |
|---|---|---|
| `key` | 是 | 模板内部引用 ID，固定为 `role_<role>`，例如 `role_tester` |
| `label` | 是 | 中文名/展示名，例如“测试工程师” |
| `role` | 是 | 通用英文 role 名，例如 `tester`；2–32 位小写英文、数字、`-`、`_` |
| `bound_role` | 否 | 预先创建的具体角色 slug，例如 `sec-tester`；可为空 |

`key` 只用于模板内部引用，不是系统角色 slug，也不是可执行角色。步骤、loop 阶段、验证矩阵通过它引用 Meeple：

```json
{
  "steps": [
    { "id": "S1", "title": "独立验证", "role_slot": "role_tester" }
  ],
  "loop": {
    "execute": { "role_slot": "role_developer" },
    "verify": { "role_slot": "role_tester" }
  },
  "verification": [
    { "cell": "功能路径", "method": "回归测试", "verifier_slot": "role_tester" }
  ]
}
```

修改 Meeple 的 `role` 时，系统会同步更新 `key` 及所有引用（步骤、loop、验证矩阵、`{{slot:key}}` 占位符），避免悬空引用。

#### 5.5.2 创建与绑定

创建 Meeple 只需要两个输入：

1. **中文名**：例如“测试工程师”；
2. **Role 名**：例如 `tester`。

系统自动生成 `role_tester`。Role 名有格式和长度限制，避免生成不可用或不可读的 key。

创建后可以可选地绑定一个**预先创建好的具体角色**：

```text
测试工程师 / tester
绑定角色：sec-tester（可选）
```

- 绑定角色可以带有定制 skill、工具权限或运行时配置；
- 也可以不绑定，由 Cindy 在任务创建时匹配或建议创建；
- 绑定只作为“优先选择”，不改变模板的通用性。

#### 5.5.3 角色解析顺序

使用模板时按以下顺序解析：

```text
1. Meeple 绑定了具体角色，且该角色存在、启用
       → 优先使用绑定角色
2. 未绑定：Cindy 读取 Meeple 的 label / role / 绑定步骤上下文
       → 与 Roster 中角色的 description / responsibilities / capabilities 匹配
3. 仍无法匹配
       → 提交 role_suggestion，等待 human 批准后创建角色
```

重要规则：

- 不允许虚构角色；
- 角色创建仍走人工审批（HITL），不会由 Cindy 静默创建；
- 绑定角色不可用时，自动回退到匹配流程；
- 人类在“套用模板”时可以覆盖 Cindy/默认匹配结果。

#### 5.5.4 Cindy 获取的上下文

Cindy 不会只拿到一个 Meeple 名称，而是拿到：

1. Meeple 的中文名；
2. role 名；
3. 绑定的具体角色（如果有）；
4. 绑定到该 Meeple 的步骤/阶段；
5. 这些步骤的依赖、验收标准以及模板中的边关系。

因此 Meeple 不需要手工维护输入/输出字段。输入/输出由绑定步骤自动推导：

- **输入**：上游依赖步骤；loop 阶段取上一阶段；
- **输出**：步骤验收项；没有验收项时取步骤标题；审核 Meeple 输出“审核结论”。

这些推导结果只用于 Cindy 规划上下文，不要求用户在模板编辑器里手工填写。

#### 5.5.5 与 RoleType / 具体角色的关系

| 概念 | 层级 | 作用 |
|---|---|---|
| Meeple | 模板层 | 表达“需要一类什么角色”，例如 tester |
| RoleType | 角色目录层 | 描述能力、输入输出契约、默认实现 |
| 具体角色 | 运行层 | 实际参与任务的角色，可带定制 skill/工具/权限 |

- Meeple 是模板可移植性的保证；
- 具体角色是运行时执行者；
- 绑定角色是两者之间的“优先映射”，不是硬编码。

#### 5.5.6 UI 约定

- 模板编辑器中统一使用 **Meeple** 这一术语，不再使用“角色槽位/slot”；
- 卡片只展示：中文名、Role 名、绑定角色、使用状态；
- 不展示绑定步骤、输入/输出等推导信息；
- 卡片保持紧凑，输入/选择框随分辨率自适应；
- 选中步骤/边时，属性面板优先显示，Meeple 列表下移。

#### 5.5.7 兼容性

内部 JSON 字段仍为 `role_slots` / `role_slot` / `reviewer_slot` / `verifier_slot`，用于兼容已有模板、草案和已发布版本；这些字段属于数据层，不是用户可见术语。新模板的 Meeple key 统一使用 `role_<role>`。

---

## 6. 后端架构与详细设计

### 6.1 模块划分

```
┌─────────────────────────────────────────────────────────────┐
│ API Gateway (REST + WebSocket/SSE)                           │
├──────────────┬──────────────┬──────────────┬────────────────┤
│ Template Svc │ Role Catalog │ Instance Svc │ Review Svc     │
│ (CRUD/版本/  │ (RoleType/   │ (实例化/控制 │ (AI草稿→人工   │
│  Lint/Diff)  │  Impl/Binding│  /查询)      │  修订/审批)    │
├──────────────┴──────────────┴──────────────┴────────────────┤
│ Workflow Engine (状态机解释器, 事件溯源, checkpoint)           │
│   Scheduler │ Node Executor │ Guard Evaluator │ Join/Loop Mgr │
├─────────────────────────────────────────────────────────────┤
│ Agent Runtime Host (本机) │ Tool Registry │ Human Task Queue  │
├─────────────────────────────────────────────────────────────┤
│ Storage: PostgreSQL (元数据/事件) │ Object Store (产物) │ Redis│
│ Observability: OpenTelemetry Trace │ Metrics │ Audit Log      │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 Template Service
- 保存草稿（自动保存，带 `draftRevision` 乐观锁）。
- `validate()`：Schema 校验 + Lint 规则（§11）。
- `diff(v_a, v_b)`：按节点/边/配置产生语义 diff（非文本 diff）。
- `publish()`：仅 Lint 无 error 且审批通过后允许；发布后版本不可变。
- `fork()`：从已发布版本派生新草稿。

### 6.3 Instance Service
- `instantiate(templateVersionId, inputs, envBinding)` → 创建实例并投递到 Engine。
- 控制命令：`pause / resume / cancel / retryNode / skipNode / injectState / submitHumanTask`。
- 查询：当前活跃节点、state 快照、节点运行历史、trace。

### 6.4 Review Service（AI 草稿 → 人工修订）
- 记录 AI 生成的原始版本（`origin: ai`）、人工修改后的每次保存（修订记录）。
- 审批流：`draft → in_review → approved → published`，支持评论与驳回。
- 产生“人工修改反馈”数据集（可用于改进 Planner Agent）。

---

## 7. 执行引擎设计

### 7.1 执行模型：Token 驱动的状态机 + 事件溯源

- 实例启动时在 `start` 节点放置一个 **token**。
- 调度循环：取一个就绪 token → 执行其所在节点 → 写 `NodeCompleted` 事件 → 对出边按 `priority` 求值 guard → 将 token 移动到目标节点（`parallel` 复制 token，`join` 合并 token）。
- 每个事件持久化（append-only），实例状态可由事件重放恢复（Temporal 思想）。
- 所有节点执行完成后立即 **checkpoint**（state + tokens）。

### 7.2 核心伪代码

```python
def run_instance(inst):
    while tokens := inst.ready_tokens():
        for tok in tokens:
            node = inst.template.node(tok.node_id)
            if inst.policies.exceeded(): fail(inst, "budget_exceeded"); return
            try:
                result = execute_node(node, inst, tok)
            except Timeout: result = NodeResult(status="timeout")
            except Exception as e: result = NodeResult(status="failure", error=str(e))

            emit(inst, "NodeCompleted", node.id, result)
            apply_output_mapping(inst.state, node, result)

            if result.status == "needs_human":
                create_human_task(inst, node, tok); inst.status = "waiting_human"; return

            handle = result.status  # success|failure|timeout
            edges = inst.template.out_edges(node.id, handle)
            next_edges = select_edges(edges, inst)          # 排他: 第一条 guard 为真; parallel: 全部
            if not next_edges: next_edges = fallback(node, handle)  # 默认边或失败策略

            for e in next_edges:
                if e.kind == "back":
                    if inst.traversals[e.id] >= resolve(e.maxTraversals):
                        emit(inst, "LoopLimitHit", e.id); continue_default_or_fail(); continue
                    inst.traversals[e.id] += 1
                move_token(inst, tok, e.target)
            checkpoint(inst)
    finalize(inst)   # 所有 token 到达 end 或无活跃 token
```

### 7.3 关键子机制

| 机制 | 设计 |
|---|---|
| **循环控制** | 回边计数器 `traversals[edgeId]`；`loop` 容器维护 `loop.index/item`；超限走默认边或 fail；记录每轮 state 快照便于对比 |
| **并行与 Join** | `parallel` 产生带 `branchId` 的 token；`join` 按 `all/any/n-of-m` 等待；分支 state 写入用 reducer 合并，避免覆盖 |
| **人工节点** | 引擎挂起该 token（不占用线程），写入 Human Task Queue；人提交表单后 `resume(tokenId, payload)`；SLA 超时触发 `timeout` 出口或升级 |
| **重试** | 节点级 `retry` 策略；幂等键 = `instanceId+nodeId+attempt`；工具调用需声明是否幂等 |
| **超时** | 节点 deadline + 实例总 wall-clock；超时触发 `timeout` 出口（边界事件） |
| **预算** | token/费用/工具调用次数累计，超出进入 `budget_exceeded` 终止 |
| **动态 spawn** | `spawn` 节点内：planner 产出子任务列表（结构化 spec）→ 为每个子任务创建 **子实例**（可用 subworkflow 或 worker role）→ aggregator 汇总；受 `maxDepth/maxBreadth` 约束；子实例回传结构化摘要而非全上下文 |
| **取消** | cancel_token 传入 runtime；工具调用需支持协作式取消 |
| **确定性** | guard 求值只依赖 state 快照；LLM 路由结果写入事件以便重放不再调用模型 |

### 7.4 子工作流与 spawn 的上下文隔离
- 子实例只接收 `inputMapping` 指定的切片；大产物存对象存储，传引用 ID。
- 子实例返回 `{status, summary, artifacts[], confidence, openIssues[]}` 结构化结果。

---

## 8. AI 生成 → 人工修订闭环

### 8.1 流程

```
[用户需求] → Planner Agent 生成 DSL (origin: ai)
          → 后端 Schema 校验 + Lint → 自动修复可修复项(如补默认 maxIterations)
          → 保存为 Draft v(n) → 通知审阅人
          → 人工在画布修订 (每次保存=修订记录, 可 diff 回 AI 原稿)
          → 试运行 (Dry-run / Sandbox Run) 验证
          → 提交审批 → 审批人复核 diff + Lint 报告 → 发布
          → 人工修改的差异回流到 Planner 的 few-shot / 评估集
```

### 8.2 AI 生成侧要求（Planner 输出契约）
- 必须输出符合 `workflow.schema.json` 的 JSON，并为每个节点附 `rationale`（为什么需要该节点）和每条边附 `label`。
- 对循环必须给出 `exitCondition` 与 `maxIterations`，否则 Lint 判 error。
- 对角色只允许使用 RoleCatalog 中存在的 `roleType`（生成时注入目录）；不存在者标记 `role.type: "UNRESOLVED:xxx"`，画布高亮要求人工绑定。
- 建议 Planner 自带 **自检步骤**（Evaluator）：对生成的 DSL 跑一遍 Lint，并把 warning 一并输出。

### 8.3 人工修订的“AI 辅助”
- 画布内提供 “让 AI 修改此节点 / 根据评论重构此分支” 的局部重生成能力，但结果仍进入 draft 并走 diff。
- 自然语言→DSL 补丁（JSON Patch），人可逐条接受/拒绝。

---

## 9. 前端架构与页面设计

### 9.1 技术选型
- React + TypeScript；画布 **React Flow (xyflow)**；状态管理 Zustand；表单 react-hook-form + JSON Schema 表单渲染（如 rjsf）；表达式编辑 Monaco（CEL 语法高亮 + 变量补全）；布局 dagre/elk 自动排版；Diff 视图自研（基于语义 diff 结果着色）。
- 与后端通过 REST + WebSocket（运行态实时事件）。

### 9.2 页面地图

| 页面 | 作用 |
|---|---|
| 模板列表 | 搜索/筛选（状态、来源 ai/human、标签）、版本树 |
| **模板编辑器（核心）** | 画布 + 节点面板 + 属性侧栏 + Lint 面板 + 版本/Diff 抽屉 + 试运行面板 |
| 角色目录 | RoleType 列表、契约、实现、环境绑定 |
| 审批中心 | 待审模板 diff、Lint 报告、评论、通过/驳回 |
| 实例列表 / 实例详情 | 运行画布（高亮当前节点、循环轮次、并行分支）、state 面板、trace、人工任务 |
| 人工任务收件箱 | 待处理 `human` 节点表单 |

### 9.3 模板编辑器布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ 顶栏: 模板名 | v4 (draft, from AI) | 保存 ● | Lint: 2 err 3 warn | 试运行 | 提交审批 | ⋯ │
├──────────┬───────────────────────────────────────────┬───────────────┤
│ 左: 节点 │ 中: 画布 (React Flow)                     │ 右: 属性面板  │
│ 面板     │  - 节点以类型着色/图标                    │  (选中节点/边 │
│ - Agent  │  - 回边以虚线+循环图标+ "≤3次" 标注       │   时显示)     │
│ - Tool   │  - Router 出口显示 guard 标签             │  Tabs:        │
│ - Router │  - 并行区域用分组框                       │  基础/角色/   │
│ - Loop   │  - Lint 错误节点红色描边, 悬浮显示原因     │  提示词/输入  │
│ - Human  │  - 小地图 + 缩放 + 自动布局               │  输出/策略/   │
│ - Join   │                                           │  高级(JSON)   │
│ - Sub-WF │                                           │               │
│ - Spawn  │                                           │               │
│ 角色列表 │                                           │               │
│ (拖拽创建│                                           │               │
│  Agent)  │                                           │               │
├──────────┴───────────────────────────────────────────┴───────────────┤
│ 底部抽屉: Lint 问题列表 | 变量浏览器(state/inputs schema) | 试运行日志 | 版本 & Diff │
└──────────────────────────────────────────────────────────────────────┘
```

### 9.4 画布交互细节

**节点操作**
- 从左侧面板拖入创建；双击打开属性；右键菜单：复制、删除、禁用（跳过）、折叠为子流程、替换角色、“AI 重写此节点”。
- 删除有出入边的节点时提示“自动桥接（将入边接到出边目标）/ 仅删除”。
- Agent 节点卡片展示：角色类型 badge、实现（环境绑定解析后）、超时/预算摘要、输出 schema 字段。

**边（过渡）操作**
- 从节点出口 handle（success/failure/timeout 或 router 分支）拖到目标节点创建边。
- 点击边 → 右侧面板编辑：label、guard（表达式编辑器/LLM 语义路由选项）、priority（同源多边可拖拽排序）、设为默认边、kind。
- 连接到祖先节点时自动识别为回边：弹出“设置最大循环次数”（必填），边变虚线并带计数标注。
- Router 节点出口顺序 = priority，面板中可拖拽改序，画布实时更新序号。

**循环容器**
- 可框选一组节点 → “折叠为 Loop”：生成 loop 节点，内部子图可展开/收起编辑；配置 while/for_each/until、maxIterations、exitCondition。

**条件编辑器**
- 表达式模式：Monaco + 变量树（点击插入 `state.review.score`）+ 实时校验 + “用样例 state 试算”按钮。
- LLM 语义路由模式：填写分类提示与选项列表，每个选项对应一条出边；提供“用历史样本测试分类”。

**Lint 联动**
- 保存或变更时后端/前端同构 Lint；问题列表点击定位到节点/边；error 阻止提交审批。

**版本与 Diff**
- 版本抽屉列出 AI 原稿、各修订、已发布版本；选两版 → 画布 Diff 模式：新增绿色、删除红色、修改黄色，侧栏显示字段级差异；可“还原此节点为 AI 原稿”。

**试运行（Sandbox Run）**
- 输入 inputs → 以沙箱绑定（可选 mock 角色实现 / 低成本模型）执行；画布实时高亮 token 流转，回边显示“第 k/N 轮”，底部输出每节点 I/O；支持在任意节点设置断点、单步、修改 state 后继续（LangGraph 的 time-travel 思路）。
- 单节点调试：给定 mock state 只执行一个节点。

### 9.5 实例运行视图
- 复用同一画布组件（只读），叠加运行态：当前活跃节点脉冲高亮、已完成节点打勾、失败节点红色、循环节点显示轮次、并行分支显示各自 token。
- 右侧：state 快照（按事件时间轴可回溯）、节点运行列表（每轮一条，含耗时/tokens/日志/产物）、Trace 链接。
- 操作：暂停/恢复/取消、重试节点、跳过节点、注入 state（需权限并记审计）、处理人工任务。

### 9.6 前端数据流

```
DSL(JSON) ⇄ EditorStore(Zustand, normalized: nodesById, edgesById, selection, lint)
            ⇄ React Flow (nodes/edges 投影, layout 仅存 position)
            ⇄ 属性面板表单 (JSON Schema 驱动, 按节点类型选择 schema)
自动保存: debounce 1s → PUT /templates/:id/draft (携带 revision, 冲突则提示合并)
Lint: 本地快速规则即时执行; 保存后服务端全量 Lint 回填
```

---

## 10. 人工操作流程（Operator Playbook）

| 场景 | 操作步骤 |
|---|---|
| 审阅 AI 生成的流程 | 打开草稿 → 看 Lint 面板 → 逐节点查看 `rationale` → 必要时试运行 |
| 修改节点角色 | 选中 Agent 节点 → 角色 Tab → 从目录选择 roleType（显示契约兼容性检查：输出 schema 是否满足下游引用） |
| 修改提示词/输出结构 | 提示词 Tab 编辑，变量用 `{{}}` 插入；修改 outputSchema 后 Lint 会检查下游引用失效 |
| 增加分支条件 | 在 Router 添加出口 → 连线 → 写 guard → 设置默认边 |
| 修改循环退出条件/轮数 | 点击回边或 Loop 容器 → 修改 guard / maxIterations |
| 插入人工审批 | 拖入 Human 节点到边上（自动拆边插入）→ 配置表单与负责人与 SLA |
| 折叠复用 | 框选 → 折叠为子流程 → 另存为新模板 → 原处替换为 Sub-WF 引用 |
| 试运行验证 | 顶栏试运行 → 输入样例 → 观察流转，必要时断点修改 state |
| 提交审批与发布 | Lint 无 error → 提交 → 审批人看 diff 与试运行记录 → 发布 → 选择环境绑定 |
| 回滚 | 版本抽屉 → 选择历史发布版 → “设为当前发布版” |
| 运行中干预 | 实例详情 → 暂停 → 修改 state / 重试 / 跳过 → 恢复 |

权限（RBAC）：`viewer`（只读）、`editor`（编辑草稿、试运行）、`approver`（审批发布）、`operator`（实例干预）、`admin`（角色目录与环境绑定）。

---

## 11. 校验规则（Lint）

| 级别 | 规则 |
|---|---|
| error | 有且仅有一个 `start`；至少一个 `end`；所有节点从 start 可达；所有节点可达某个 end |
| error | 回边 / loop 必须设置 `maxTraversals/maxIterations`（>0 且 ≤ 全局上限） |
| error | Router 同源出边必须有默认边或 guard 覆盖完备（无法静态证明时降为 warning） |
| error | 变量引用 `{{...}}` 指向不存在的节点/字段，或类型不匹配 outputSchema |
| error | `role.type` 不在目录中或当前环境无实现绑定 |
| error | `parallel` 无对应 `join`（或 join 无法匹配分支）；`join` 的 n-of-m 参数非法 |
| error | 表达式语法错误 / 使用禁用函数 |
| error | `subworkflow` 引用的模板版本未发布或形成递归引用 |
| warning | 节点无超时 / 无预算（将套用默认值） |
| warning | Agent 节点 `failure` 出口未连接（将采用默认失败策略） |
| warning | 环路未折叠为 Loop 容器；循环内无 state 变化（可能死循环） |
| warning | 人工节点无 SLA；并行分支写同一 state 字段且无 reducer |
| warning | 连续两个 agent 节点使用相同角色与提示（可能冗余） |
| info | 节点缺少描述 / rationale；边缺少 label |

Lint 引擎前后端同构（TS 实现，后端用 Node 或通过 WASM/子进程复用），保证一致。

---

## 12. 版本、发布与回滚

- 版本号单调递增；每个版本保存完整 DSL 快照 + 语义 diff 摘要 + 来源（ai / human:userId）+ Lint 报告 + 审批记录。
- `published` 版本不可变；修改需 `fork` 出新 draft。
- 每个模板维护一个 `currentPublishedVersion` 指针，回滚 = 移动指针（并记审计）。
- 实例固定 `templateVersionId`；提供“查看该实例所用版本”入口。
- 模板可参数化：`inputs` 即模板参数；也支持 `variants`（例如 fast/thorough）作为策略覆盖层，不复制整份 DSL。

---

## 13. 可观测性、安全与治理

- **Trace**：OpenTelemetry；实例 = root span，节点运行 = child span，循环每轮 = 独立 span，spawn 子实例 = link；记录 prompt hash、tokens、耗时、工具调用。
- **Metrics**：实例成功率、平均轮数、循环命中上限比例、人工节点等待时长、每模板成本。
- **审计**：模板每次修改、发布、回滚、实例干预（注入 state/跳过）全部记录 who/when/diff。
- **安全**：表达式沙箱；工具白名单在节点级与角色级双重限制；提示词注入防护（用户输入与指令分离渲染）；敏感操作（写外部系统）必须经 Human 节点或策略允许。
- **成本治理**：模板级 `policies` 上限 + 组织级配额；超限自动暂停并通知。

---

## 14. 数据库表结构（PostgreSQL）

```sql
-- 模板与版本
workflow_templates(id PK, name, description, tags jsonb, current_published_version int, created_by, created_at)
workflow_template_versions(id PK, template_id FK, version int, status text, origin text,
    dsl jsonb, layout jsonb, lint_report jsonb, parent_version int, created_by, created_at,
    UNIQUE(template_id, version))
workflow_draft_revisions(id PK, version_id FK, revision int, dsl jsonb, patch jsonb, author, created_at)
template_reviews(id PK, version_id FK, reviewer, decision text, comments jsonb, created_at)

-- 角色目录
role_types(id PK, name, capabilities jsonb, contract jsonb, defaults jsonb)
role_implementations(id PK, role_type_id FK, runtime text, entry text, version text, cost_tier text, is_default bool)
environment_bindings(env text, role_type_id FK, impl_id FK, PRIMARY KEY(env, role_type_id))

-- 实例与运行
workflow_instances(id PK, template_version_id FK, env, inputs jsonb, state jsonb, status text,
    tokens jsonb, traversals jsonb, usage jsonb, started_at, finished_at, error text)
node_runs(id PK, instance_id FK, node_id text, attempt int, loop_iteration int, branch_id text,
    status text, input jsonb, output jsonb, usage jsonb, started_at, finished_at, trace_id text)
instance_events(id BIGSERIAL PK, instance_id FK, seq int, type text, payload jsonb, created_at)  -- 事件溯源
human_tasks(id PK, instance_id FK, node_id text, token_id text, form_schema jsonb, assignees jsonb,
    status text, submitted_by, payload jsonb, due_at, created_at)
artifacts(id PK, instance_id FK, node_run_id FK, uri text, mime text, size bigint, meta jsonb)
audit_logs(id PK, actor, action, target_type, target_id, diff jsonb, created_at)
```

---

## 15. API 清单

**模板**
- `POST /templates/generate` — 由 Planner Agent 生成草稿（body: requirement, constraints）
- `GET /templates`, `GET /templates/:id`, `GET /templates/:id/versions`
- `PUT /templates/:id/versions/:v/draft` — 保存草稿（携带 revision）
- `POST /templates/:id/versions/:v/validate` — Lint
- `GET /templates/:id/diff?from=&to=` — 语义 diff
- `POST /templates/:id/versions/:v/submit-review`, `/approve`, `/reject`, `/publish`
- `POST /templates/:id/versions/:v/fork`
- `POST /templates/:id/rollback {version}`
- `POST /templates/:id/versions/:v/patch-by-ai {instruction, scope}` — 返回 JSON Patch 供接受/拒绝

**角色**
- `GET/POST /roles`, `GET/POST /roles/:id/implementations`, `GET/PUT /environments/:env/bindings`

**实例**
- `POST /instances {templateVersionId, inputs, env, sandbox?}`
- `GET /instances/:id`, `GET /instances/:id/events`, `GET /instances/:id/node-runs`
- `POST /instances/:id/{pause|resume|cancel}`
- `POST /instances/:id/nodes/:nodeId/{retry|skip}`
- `POST /instances/:id/state {patch}` — 注入 state（审计）
- `POST /instances/:id/debug/{breakpoint|step}` — 试运行调试
- `WS /instances/:id/stream` — 实时事件

**人工任务**
- `GET /human-tasks?assignee=me`, `POST /human-tasks/:id/submit {payload}`

---

## 16. 实施路线图

| 阶段 | 交付 |
|---|---|
| M1（2–3 周） | DSL + JSON Schema；Lint 引擎；Template CRUD/版本；基础画布（节点/边/属性面板/保存） |
| M2（3–4 周） | 执行引擎（token/事件溯源/checkpoint/回边计数/超时重试）；RoleRegistry 与本机 runtime 适配；实例视图 |
| M3（2–3 周） | Router 条件编辑器、Loop 容器、Parallel/Join、Human 节点与任务收件箱 |
| M4（2 周） | AI 生成接入 + Diff + 审批发布流 + 回滚；试运行与断点调试 |
| M5（持续） | Spawn/子工作流、环境绑定、成本治理、观测看板、AI 局部重写与修改回流 |

---

## 17. 附录

### 17.1 节点类型配置 Schema 摘要（TypeScript）

```ts
type Node =
  | { id: string; type: 'start' }
  | { id: string; type: 'end'; config: { result: 'success' | 'failure' }; outputMapping?: Mapping }
  | { id: string; type: 'agent'; role: RoleRef; config: AgentConfig; outputMapping?: Mapping }
  | { id: string; type: 'tool'; config: { toolId: string; params: Mapping; timeoutSec?: number }; outputMapping?: Mapping }
  | { id: string; type: 'router'; config: { mode: 'expression' } | { mode: 'llm_classifier'; role: RoleRef; prompt: string; options: string[] } }
  | { id: string; type: 'parallel'; config: { maxBranches: number } }
  | { id: string; type: 'join'; config: { mode: 'all' | 'any' | { nOf: number }; timeoutSec?: number; reducers?: Record<string, 'append'|'merge'|'last'> } }
  | { id: string; type: 'loop'; config: { mode: 'while'|'until'|'for_each'; condition?: Guard; items?: string; maxIterations: number; body: { nodes: Node[]; edges: Edge[] } } }
  | { id: string; type: 'human'; config: { form: FormSchema; assignees: Assignees; slaSec?: number; onTimeout?: 'escalate'|'timeout_edge'|'auto_approve' } }
  | { id: string; type: 'subworkflow'; config: { templateId: string; version: number; inputMapping: Mapping; outputMapping: Mapping } }
  | { id: string; type: 'spawn'; config: { plannerRole: RoleRef; workerRole: RoleRef; maxDepth: number; maxBreadth: number; aggregatorRole?: RoleRef; subtaskSchema: JsonSchema } }
  | { id: string; type: 'wait'; config: { eventType: string; timeoutSec: number } };

interface RoleRef { type: string; selector?: { implId?: string; capability?: string[]; costTier?: 'low'|'standard'|'high' } }
interface AgentConfig { prompt: string; systemPrompt?: string; tools?: string[]; outputSchema: JsonSchema;
  timeoutSec?: number; budget?: { maxTokens?: number; maxToolCalls?: number }; retry?: { maxAttempts: number; backoffSec: number } }
type Mapping = Record<string, string>; // "state.x": "{{output.y}}"
```

### 17.2 出边选择算法

```python
def select_edges(edges, inst):
    edges = sorted(edges, key=lambda e: e.priority)
    if source_is_parallel(edges): return edges                # 并行: 全部
    for e in edges:
        if e.isDefault: continue
        if eval_guard(e.guard, inst.snapshot()): return [e]   # 排他: 第一个为真
    default = [e for e in edges if e.isDefault]
    return default                                            # 无默认 → 空 → fallback
```

### 17.3 回边语义 Diff 示例（供审批页展示）

```json
{
  "nodes": { "added": ["approve"], "removed": [], "modified": { "writer": { "config.prompt": {"from": "...", "to": "..."} } } },
  "edges": { "added": ["e7","e8"], "removed": ["e_old_direct_end"], "modified": { "e5": { "maxTraversals": {"from": 10, "to": "{{inputs.maxRounds}}"} } } },
  "policies": { "modified": { "requireHumanApprovalBeforePublish": {"from": false, "to": true} } }
}
```

### 17.4 关键取舍说明
- **为什么不用纯 BPMN**：词汇太重、与 LLM 节点（预算/提示词/输出 schema）不匹配；只借用 Gateway/Boundary Event 概念。
- **为什么回边与 Loop 容器并存**：AI 生成天然产出回边；人工审阅时折叠成容器更可控。二者执行语义统一到 `traversals` 计数器。
- **为什么模板只绑角色类型**：保证模板跨环境可移植、便于替换实现与做 A/B；具体 impl 由环境绑定决定。
- **为什么事件溯源**：循环/并行/人工等待使运行态复杂，事件日志是调试、重放、审计与断点调试的基础。

---

如需，我可以基于这份文档进一步输出：`workflow.schema.json` 完整定义、Lint 规则的 TS 实现骨架、或 React Flow 编辑器的组件结构代码。