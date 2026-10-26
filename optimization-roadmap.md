# Anvil 优化路线图（v3.1 提案）

**基于**：design.md v3.0 对照 + 代码实现核查 + 2026-10-18 五子棋多角色实测复盘 + raft.build / multica / 业界多agent实践调研。
**日期**：2026-10-18。**状态**：owner 已批准（含修订：P1-0 计划先行编排——多角色任务先自动生成 workflow，人工确认后再运作）。

---

## 一、实测复盘：系统真实跑起来的样子

2026-10-18 晚，四角色（pm/designer/developer/tester）+ 真实 Claude Code 并行执行「开发一个五子棋 Web 程序」，持续约 10 分钟。这是系统第一次真实多agent运行，证据链完整（DB 状态、run 日志、角色 home 产物、共享仓库提交）。

### 1.1 时间线

| 时刻(UTC) | 事件 |
|---|---|
| 11:53:08 | 4 角色并行认领成功（`task_role_claims` 全部 working），4 个真实 claude 进程启动 |
| ~11:54–11:59 | PM 产出 `docs/PRD.md`、`docs/acceptance.md`（AC-1~20）、`docs/plan.md`（含文件归属+接口基线），提交共享仓库 `repos/gomoku`（commit 3c1f0a4） |
| 11:59:41–49 | PM @mention 委派三角色：designer→M2 设计、developer→M3 实现（「不用等设计定稿，先做 engine/ai/tests」）、tester→M4 测试（先评审验收清单）；随后 PM 挂起等待 |
| 11:59:41–49 | 三角色被唤醒（run 状态 running→projecting→running，续跑协议生效） |
| **12:03:09** | **designer/developer/tester 三个 run 同时标 failed** = 11:53:08 + 600s，分秒不差——超时回收误杀（见 §2.1-B1） |
| ~12:03 | 服务随宿主会话退出；PM run 卡 `running`（挂起态无人处理）；4 个 claude 进程成为孤儿继续消耗额度 |

### 1.2 实测验证了什么（值得保留的设计）

| 机制 | 实测结果 |
|---|---|
| 多角色并行认领（task_role_claims） | ✅ 4 角色同时 working，互不阻塞 |
| @mention 角色间直接协作 | ✅ PM→三角色委派生效，消息内容含里程碑分工 |
| @ 即唤醒（挂起-续跑协议） | ✅ 三角色被 @mention 唤醒，带着会话上下文续跑 |
| PM 自发任务分解 | ✅ M1–M5 里程碑、验收标准、文件归属、决策记录——**涌现出完整的 leader 协调行为**（与 multica Squad leader 模式同构） |
| PM 挂起等待（@了别人不 submit） | ✅ 协议被正确遵守 |
| 依赖交接 @下游（notify_downstream） | ✅ 已实现（本次未触发路径） |

### 1.3 实测暴露了什么（按严重度）

| # | 问题 | 证据 |
|---|---|---|
| E1 | **续跑即被误杀**：`started_at` 只在 run 创建时写入，resume 续跑不重置；monitor 用原始时间判超时 → 任何被唤醒的 run 都在「创建+600s」被杀 | 三 run 12:03:09 同时 failed；`runs.py:44`（唯一写入点）+ `scheduler.py:87` |
| E2 | **双超时源矛盾**：`RUN_TIMEOUT_SECONDS=600` vs 运行时 YAML `timeout_seconds: 3600`，短者获胜，YAML 形同虚设 | `config.py:46` |
| E3 | **共享工作区未机制化**：任务未绑 `repo` → worktree 静默回退私有 home → PM 在笔记里发明「共享仓库约定」纯靠 prompt 传播 → designer/developer **各自重复实现 engine/ai/tests**，产出分散无法合并 | designer home 有 `gomoku-web/js/engine.js`；developer home 有 `checkers/`；共享仓库只有 PM 的 4 个文件 |
| E4 | **角色边界无执行**：designer 越界写实现代码；`scope_paths` 为空 → diff 越界校验无对象 | 同上 |
| E5 | **无优雅停机**：服务退出无 drain/孤儿回收；挂起态 run（PM）永久卡 `running`；孤儿进程继续烧钱 | DB 状态 + 孤儿进程观测 |
| E6 | **CLI 契约缺口**：PM 直接 `sqlite3 data/anvil.db` 查会话增量——agent 被迫发明绕过手段，会话令牌安全模型被打穿 | PM-notes.md「挂起等待中」段落原文 |
| E7 | **会话注入无预算**：`context_for_task` 全量注入 MEMORY.md + 20 lessons + 5 notes，无截断无审计（design §4.4 未兑现） | `memory.py:105-116` |

---

## 二、业界对照

### 2.1 对照矩阵

| 机制 | Anvil 现状 | raft.build | multica | 差距判定 |
|---|---|---|---|---|
| 编排范式 | 任务板 + 多角色并行 + mention 协调 | 频道/线程 + claim 认领 | Issue/Run 1:N + Squad leader 路由 | Anvil 已具备骨架；**leader 模式未机制化**（PM 是自发行为） |
| 挂起-续跑 | ✅ 协议已实现 | — | ✅ 评论合并(coalescing)进 pending run | **E1 bug 使续跑不可用**；缺 coalescing |
| 上下文组装 | 全量注入，无预算 | **CIC 五块组装**（冷唤醒 -60%） | Issue 时间线即上下文 + 双层 prompt | **最大提升空间**（§4.4 兑现 + CIC 模式） |
| 共享产出 | worktree 已实现但**依赖任务绑 repo**，未绑则静默回退 | 频道附件 | GitHub repo 默认 worktree 模式；目录锁 | 需**默认化**，不能靠 prompt 约定 |
| 角色/身份 | 角色=配置+home+记忆 | **命名实例**（带历史的 name） | agent=身份+配置，run 是执行 | 一致；Anvil 私有 home + 记忆设计得到验证 |
| 记忆 | 私有 MEMORY/notes + shared lessons（80 上限） | 每 agent 独立记忆+消息互联，**反 company brain** | 工作记录即持久化 + Skills 制度化记忆 | Anvil 立场与 raft 一致；**Skills 作为团队记忆**可吸收 |
| 评审 | HITL 全任务评审（人工收口可选） | builder≠verifier 验证矩阵；**trust 是 state 不是 event** | review gate：leader 最高推到 in_review，done 留给人类 | Anvil 一致；可吸收**先写 gate 的三角配方** |
| 状态契约 | agent 显式 claim/submit ✅；但 run failed 会牵连 | — | 系统不代写状态（仅失败回滚/PR merge 两例外） | 部分一致 |
| 防循环 | _wake 按 (task,role) 判重 | — | self-trigger guard、显式@即路由、run 去重、评论合并 | 需补 coalescing |
| 失败处理 | failed/abandoned 二值 + RUN_TIMEOUT | 失败归因「不干净输入的地图」 | **双分类**：平台侧(runtime_offline/timeout/…) vs agent 侧(agent_error.*) | 需补 taxonomy |
| 成本观测 | 手动注入 only | — | usage analytics 按 run/agent/issue | **未实现**（§6.5） |
| 安全 | run 级短时会话令牌 ✅ | action card 硬边界 | run 级短时 token + 诚实安全声明 | Anvil 已对齐；PM 直查 DB（E6）是旁路 |

### 2.2 业界共识（Anthropic / LangGraph / MetaGPT / Cognition）

- **Orchestrator-worker 是主旋律**（Anthropic 多agent研究系统：orchestrator 拆解分发、subagent 各自带上下文、token 消耗 ~15× 单agent——**成本预算必须是硬约束**）。Anvil 的 PM-in-task 模式是其变体，验证可行。
- **Cognition 批评**（design §13.1 已引）：并行子agent上下文割裂 → 冲突产出、合并成本超收益。**实测完全应验**（E3：designer/developer 重复造轮子）。解药不是砍并行，而是**共享工作区 + 文件归属 + 边界执行**。
- **MetaGPT SOP 编码**：把协作流程写进机制而非 prompt。Anvil 的工作流模板（design-dev-test 等 4 个静态模板）是雏形；实测证明**动态分解（PM 临场拆 M1–M5）比静态模板更贴合真实任务**——两者应共存：模板做默认、leader 动态拆解做增强。
- **LangGraph checkpointing + interrupt**：Anvil 已采用（run 8 态 + HITL interrupt）✅。

---

## 三、优化路线图

> 原则（design §13.5 延续）：先让「多角色并行 + 续跑 + 合并产出」真正顺畅，再叠加智能。每阶段有明确验收，可独立停止。

### P0 · 可靠性地基（不修则一切白搭）——约 3 天

| # | 项 | 方案 | 来源 |
|---|---|---|---|
| P0-1 | **修续跑误杀** | resume/spawn 时重置 `started_at`（或 monitor 改用「本进程启动时间」） | 实测 E1 |
| P0-2 | **统一超时源** | 单一真相 = 运行时 YAML `invocation.timeout_seconds`（默认 3600s）；`RUN_TIMEOUT_SECONDS` 降级为未配置时的兜底；monitor 读 run 级超时 | 实测 E2 |
| P0-3 | **优雅停机 + 启动自愈** | ① SIGTERM handler：活跃 run 标 `suspended`（新态，区别于 failed），不杀进程不等待；② 启动 reconcile：`running` 但无进程 → 依「挂起语义」转 suspended（可被 mention 唤醒恢复）；③ 按记录 pid 清理孤儿进程 | 实测 E5 + multica daemon 心跳语义 |
| P0-4 | **失败分类** | runs 加 `failure_class`：`platform_timeout` / `platform_killed` / `agent_exit_<code>` / `agent_error`；时间线展示分类 | multica 双分类 |

**验收**：四角色五子棋任务重跑，PM 委派后三角色续跑 >15 分钟不被误杀；中途重启服务，恢复后 suspended run 可被 @mention 唤醒继续；无孤儿进程残留。

### P1 · 协作机制化（计划先行 + 共享产出）——约 2 周

> **owner 2026-10-18 决策**：偏好 raft 式「计划先行」——多角色任务**先自动生成 workflow**（谁主导、谁干哪些事、依赖顺序、出问题反馈给谁），**人工确认后再运作**。

| # | 项 | 方案 | 来源 |
|---|---|---|---|
| **P1-0** | **计划先行编排（Plan-First）** | ① 多角色任务派发时先进入**规划阶段**：唤醒 leader（planner 角色，如 PM）产出 workflow 草案——目标分解（子任务树）、角色分工（**谁主导、谁执行哪些事**）、依赖顺序（depends_on）、**反馈/升级路径**（出问题 @谁、何时升级到 human）；② 草案载体复用 `board_drafts`（已有：draft → 人工审阅 → approve 落板），扩展 draft schema 加 `leader` 与 `feedback_rules`；③ 新 CLI `anvil board draft`（agent 写入草案树）+ `anvil board submit-draft`；④ 规划 prompt 注入三段式（Operating Protocol：只规划不执行、dispatch 后即停 / Roster：成员+mention 语法+职责 / 任务上下文）；⑤ **HITL 门**：人类批准后才落板执行（依赖感知派发 + notify_downstream 已实现）；⑥ 执行中 leader 可动态增改子任务（`created_by: agent:<slug>` 已支持） | owner 决策（raft 偏好）+ 实测 PM 自发 M1–M5 行为 + multica Squad + MetaGPT SOP |
| P1-1 | **共享工作区默认化** | 创建任务时：显式选 repo，或勾选「自动创建共享仓库」（`repos/<slug>/` git init + base branch）；无 repo 模式需显式确认并在 UI 标注「私有产出，不可合并」。worktree per (task,role) 强制生效 | 实测 E3 + multica「GitHub repo 默认 worktree 模式」 |
| P1-2 | **scope 机制** | 任务创建时按模板生成默认 `scope_paths`（如 designer→`docs/**`、developer→`src/** tests/**`）；角色权限 ∩ 任务 scope = diff 校验依据；越界在提交时拒绝（已实现）+ UI 红标；**workflow 草案中的分工可作为 scope 来源** | 实测 E4 + design §4.3 |
| P1-3 | **Leader 三段式注入** | leader（P1-0 的 planner）spawn 时注入：**Operating Protocol**（规划/委派为主、每轮留评估记录、dispatch 后即停、成员回报后被唤醒决策）+ **Roster**（成员一行：slug + mention 语法 + 职责）+ 用户路由指令；v1 软协议（注入+留痕，不硬禁止 leader 实现） | multica Squad + 实测 PM 行为固化为机制 |
| P1-4 | **防循环规则** | ① 评论合并：同角色已有 pending run 时，新 @mention 合并进该 run 的 prompt 而非新开；② 显式 @ 即路由信号（被 @ 的目标处理，其他人不围观）；③ self-trigger guard | multica 防循环规则集 |
| P1-5 | **CLI 契约补全** | `anvil task events --since <id>`（增量读取任务会话，替代 PM 直查 DB）；`anvil task claims`（查看各角色认领状态）；`anvil ws status/diff`（design §7.2 已设计未实现） | 实测 E6 + design §7.2 |
| P1-6 | **工作流模板重定位** | 静态模板（design-dev-test 等）降级为**无 planner 角色时的快速模式默认**；新增 **PM/Engineer/Reviewer 三角模板**（raft 配方：PM 定验收标准 → Engineer 锁实现契约 → **Reviewer 先写可验证 gate**）作为 plan-first 的一个预设 | raft 三角配方 + 实测 M1–M5 动态分解 |

**验收**：重跑五子棋任务——① 派发后 PM 先产出 workflow 草案（子任务树+分工+依赖+反馈路径），人工批准后各子任务按依赖顺序自动派发；② 产出全部落在共享仓库的 `agent/<role>/<task>` 分支，designer 提交 docs、developer 提交 src/tests，无重复文件；③ PM 的评估记录出现在时间线；④ scope 越界提交被拒；⑤ `anvil task events --since` 可用。

### P2 · 上下文工程（成本与续跑质量的杠杆）——约 1 周

| # | 项 | 方案 | 来源 |
|---|---|---|---|
| P2-1 | **注入预算落地（design §4.4）** | 按固定优先级组装（身份 800 / 任务 600 / 记忆 1200 / 共享 500 / 协议 400 tok），超预算从后往前截断，截断事件写日志（频繁截断 = 记忆该整理的信号，design 原话） | design §4.4 未兑现 |
| P2-2 | **CIC 冷唤醒组装** | 会话超限/过旧时开新会话，首输入按五块组装：MEMORY 头 / 最近会话 N 条 / 近时文件清单（旧文件只列名）/ 最近动作 / 唤醒消息；raft 实测冷唤醒成本 -60% | raft Constructed Initial Context |
| P2-3 | **transcript 瘦身** | `_conversation_transcript` 当前全量注入任务会话——长任务必爆；改为最近 N 条 + 更早摘要（或只给 `events --since` 指针，raft 三负规则：不宣布重置、不列未决、>2h 只给指针） | raft 三负规则 + 代码现状 |

**验收**：注入内容有预算审计日志；长会话任务（>30 轮 mention）续跑时 prompt 长度可控；五子棋任务重跑的每 run 注入 token 有量化对比。

### P3 · 质量与观测——约 1 周

| # | 项 | 方案 | 来源 |
|---|---|---|---|
| P3-1 | **成本自动记账（design §6.5）** | claude-code 运行时 invocation 加 `--output-format json`，run 结束解析 `usage`/`total_cost_usd` 写 cost_ledger（各运行时 YAML 配各自的解析规则）；前端成本页从「手动注入」变真实数据 | design §6.5 未兑现 |
| P3-2 | **验证前移** | 任务 `acceptance` 支持可执行条目（命令 + 期望），submit 时自动跑一遍作为 reviewer 参考；对应 raft「Reviewer 先写 gate」「trust 是 state 不是 event」 | raft 验证矩阵 |
| P3-3 | **失败信号看板** | 角色指标页增加：失败聚类（同任务/同角色/同错误类）、每角色 token/成本趋势（P3-1 数据源）——人从读 diff 转向读系统信号 | raft「Trust doesn't live in code review」 |

**验收**：五子棋任务完成后成本页显示真实 per-run 花费；AC 可执行条目在 submit 时自动执行并在时间线留结果。

### P4 · 维持 design.md 推迟决策（触发条件不变）

向量检索（>80 lessons 且检索不准）、score 加权认领（抢错任务频发时）、条件加载/MCP HTTP 握商、容器强隔离（v4）、raft 的 Held Draft / action card / trigger preview（多角色写冲突成为实际痛点时）。

### 业界实践补充（第三路调研融入，2026-10-18）

第三路深度调研（Anthropic 工程博客×5、CC agent-teams/subagents 文档、LangGraph、CrewAI、AutoGen、OpenAI Agents SDK、MetaGPT、Magentic-One、MAST、A2A）与既有路线对照后，按阶段吸收以下增量项：

| 阶段 | 增补项 | 来源 |
|---|---|---|
| P0-4 | failure_class 命名对齐 MAST 失败分类学（规格/错位/验证缺失三大类）；CC 官方坑清单（任务状态滞后阻塞依赖、lead 过早完工、遇错即停不自救、停机慢、无会话恢复）作为**回归测试用例清单** | MAST (arXiv:2503.13657) + CC agent-teams 文档 |
| P1-0 | ① 委派四要素模板：子任务卡默认含 目标/输出格式/工具信源指引/任务边界（防重复搜索与覆盖缺口）；② **工作量伸缩规则写进 planner prompt**：简单=1 角色顺序做、中等=2–4 角色、复杂=按里程碑分工，防止「小任务全员并跑」 | Anthropic 多agent系统 + CC |
| P1-4 | 信任边界公理：agent 间消息**不携带批准权**（与本项目 HITL 规则一致：评审必须自然人点击）；agent 输出扫描层（标记模仿系统指令形状的文本，防提示注入传话） | CC agent-teams 安全模型 |
| P2 | ① compaction 最轻档：长会话续跑前清工具结果类历史；② **计划外置纪律**：planner 产出先落文件（draft/PM-notes）再委派——实测 PM 已自发做到✅ | Anthropic context engineering |
| P3-2 | 评审员**新鲜上下文**规则：reviewer 只看 diff+验收标准、看不到产出过程推理；注入协议加防找茬措辞（「只标记影响正确性的问题」） | CC best practices |
| P3-3 | ① 四层闸门统一为**可组合终止谓词**（AutoGegen MaxMessage/TokenUsage/Timeout/… 的组合表达）；② 记忆复合评分：FTS5 相关度+新近度（半衰期）+重要性，≥阈值 LLM 判 keep/update 合并——防 lessons 膨胀；③ 终态评测+离散检查点（验最终状态不验过程） | AutoGen + CrewAI + Anthropic |
| 边界原则 | **强依赖任务保留串行路径**（Cognition：需共享上下文的任务串行+完整交接，不硬拆并行）；「3 个专注角色胜过 5 个散漫」，默认并行度 3–5 | Cognition + Anthropic |

**调研对现有设计的验证**（无需改动）：Anvil 的 agent-as-driver ≈ CC 邮箱+任务列表混合且更持久；file-is-truth 即黑板架构 2025 回潮方向；文件锁式认领（claims/leases）与 CC agent teams 同构；context projection（按角色裁剪+预算）与业界一致；LangGraph checkpointer+interrupt 底座选型正确。

新增长期参考项（P4 观察清单）：OpenAI HITL 的**粘性决策**与**挂起版本标记**（挂起时快照 prompt/模型/工具版本，防恢复后不兼容）；Magentic-One **双账本**（Task Ledger+Progress Ledger，停滞→重写计划）作为看板字段演进的参考。

---

## 四、实施顺序与依赖

```
P0（可靠性地基）──必须最先，其余一切建立在其上
 └─> P1（协作机制化：计划先行 + 共享产出）
       ├─ P1-0 计划先行（核心，owner 决策）──依赖 P0-3 挂起语义（leader dispatch 后挂起）
       ├─ P1-1 / P1-2（共享仓库 + scope）──与 P1-0 并行，互相独立
       └─ P1-3/4/5/6 ──依赖 P1-0 的 leader 概念
            └─> P2（上下文工程）──P2-3 依赖 P1-5 的增量读取语义
                 └─> P3（质量与观测）──P3-3 依赖 P3-1 数据源
```

每阶段完成后建议真实跑一次多角色任务回归验证（五子棋是现成的验收用例）。

---

## 附：调研来源

- **multica**：multica.ai（docs/concepts、squads、triggering-agents、security-model 等）+ github.com/multica-ai/multica（VISION.md、README）——调研报告全文见会话记录
- **raft**：raft.build（官网、文档）——完整调研报告：`<临时目录>/raft-build-research.md`
- **业界实践**（第三路，已完成）：Anthropic 工程博客（multi-agent research system / building effective agents / context engineering / writing tools / CC best practices）、Claude Code docs（sub-agents / agent-teams）、LangGraph、CrewAI（crews/tasks/memory）、AutoGen（teams/termination/HITL）、OpenAI Agents SDK（multi_agent/handoffs/human_in_the_loop）、MetaGPT（arXiv:2308.00352）、Magentic-One（arXiv:2411.04468）、MAST（arXiv:2503.13657）、More Agents Is All You Need（arXiv:2402.05120）、Cognition、A2A——完整报告：`<临时目录>/anvil_research/multi_agent_best_practices_zh.md`
