# Anvil v1.50 角色 RSI — Spec

> **状态**：Approved（S0，2026-11-24）；P0 实施中。
> **设计依据**：`docs/v1.50-role-rsi.md`（设计稿，本地内部文档）。
> **实施规范**：SDD（Spec 是唯一真相源）+ TDD（先失败测试，后实现）。
> **发布目标**：v1.50.0；发布前需 owner 确认版本号与 commit。
>
> **P0 进度（2026-11-24）**：迁移/表结构、`role_space` 压缩 CAS + manifest/verify、
> RSI feature flags、提案字段 allowlist、配置修订（kind=config）已落地。
>
> **P1 进度（2026-11-24）**：完成回执（幂等 observation/harvest_seed）、review/dispatch/
> failure/rework 钩子、触发策略（量/严重度/冷却/配额）、任务可抢占、`evolving` 派生、
> RSI 独立 context slot、observations/trigger/retro API 与 `anvil rsi` CLI 已落地。
>
> **P1.1/P3.3 进度（2026-11-25）**：按角色试点门 `definition.rsi.enabled`
> （全局 `role_rsi` 关闭时仍全部停用）；`PUT /roles/{slug}/rsi` 与演化页开关；
> `role_rsi_lock_strategy` 下 agent 直写 instruction 被拒（Charter/策略均不可绕过），
> agent append principle 转 pending 候选，human 直写保持直写。
>
> **P2 进度（2026-11-24）**：隐藏 replay suite（文件为真相、索引不含 prompt/verifier、
> held-in/held-out family 不相交、最低 8+8）、配对 bootstrap/噪声门槛/MDE/成本门、
> 三态 verdict、diagnosis（不含 item ID）、`eval_runs` 持久化、shadow home 隔离
> （无 endpoint/token、真实 home hash 校验）、suite/eval 读 API 已落地。
>
> **P3 进度（2026-11-24，核心）**：候选算子校验（≤2 编辑/≤400 字符/Charter 禁词/
> portrait_refs/diagnosis_ref）、`proposals kind=strategy` 归档与 CAS 状态机、
> 候选应用（memory_card/playbook/principles/instruction.strategy）、人工晋级生成
> active evolution 版本、版本 switch（文件+角色知识卡完整读档、额外 RSI 卡归档、
> human 卡保留/partial_restore）、版本实际表现归因（`runs.role_space_version`）、
> RSI reflect 协议/独立 context slot、candidates/switch/performance API 已落地。
>
> **P3.1 进度（2026-11-24）**：策略/playbook 候选在 `ROLE_RSI_EVALUATION` 开启时
> 必须有 `promote` 的 eval 才可晋级；SOAKING `soak_check` 在成功率/返工超噪声时
> 自动回滚到 baseline 并写 `rsi.auto_rollback`。
>
> **P3.2 进度（2026-11-24）**：反思证据包（幂等 observation/retro，脱敏 token/密钥/
> home 路径，工作集与聚合 diagnosis，不含 held-out item）、反思 prompt 生成、
> 产物自动入库（知识卡自主、审批档候选、角色画像提案、RIP 证据）、run reap 幂等标记
> `rsi.reflection_ingested`；算子 tier 由固定目录派生，输出不能把审批档降为自主档。
>
> **P2.1 进度（2026-11-24）**：真实 L2 回放 runner：按版本 manifest 把
> Strategy/Experience 物化到 shadow home，在临时 workspace 内调用角色 runtime，
> 隐藏 verifier 由 evaluator 执行；无 `ANVIL_ENDPOINT/TOKEN/EVAL_KEY`、真实 home
> 前后 hash 校验、forbidden diff/token cap/超时门；`eval_runs.request_json` 队列 +
> `EvalWorker` 单并发消费，评估完成把候选置 `pending`（promote/inconclusive）或
> `rejected`；`POST /rsi/roles/{slug}/evaluations` 与 `anvil rsi eval` 已接入。
> 修复：baseline hard failure 不再否决 candidate（只有 candidate hard/forbidden
> 触发 reject）。
>
> **P4 进度（2026-11-24，核心）**：`runtime_proposals` 生命周期/签名去重/证据追加/
> md 生成与导出；确定性 detector（unhealthy runtime、spawn/timeout/context_overflow
> run）；API `runtime-proposals` 与 CLI `anvil runtime` 已落地。
>
> **P5.1 进度（2026-11-25）**：`POST /roles/{slug}/versions/baseline`（human）
> 幂等建立首个 active evolution 基线；演化页在无基线时显示说明、历史配置修订计数与
> “建立当前基线”按钮，并兼容旧后端的数组返回，避免空页/渲染异常。
>
> **P5 进度（2026-11-24）**：版本列表分页/筛选/摘要/重复检测、批量 soft purge
> （保护 active/baseline/被引用）、`role@version` 导出 zip、导入 preview/new_role/
> add_version（hash 校验/zip-slip/大小/依赖报告/Charter 差异）、API 与
> `anvil role export|import` 已落地；**工作集（working set）分层**与
> `runs.working_set_hash` / `eval_runs` 双侧 hash 已落地。
>
> **P5.2 进度（2026-11-25）**：修复配置修订膨胀：`_snapshot_version` 在
> `role_rsi` 关闭时也对 `charter_digest`（definition + role_tools + Charter）做 no-op
> 去重，重复保存只累加 `coalesced_count`；human 直写 instruction/principles 也留配置
> 修订；新增 `POST /roles/{slug}/versions/dedupe-config` 与演化页“清理重复配置修订”
> （dry-run + soft delete，每组保留最新一条）。Cindy 实测：738 条同 hash，可清理 737 条。
>
> **P5.3 进度（2026-11-25）**：基本信息页移除“配置版本列表 + 回退”，改为
> 角色空间摘要（当前演进版本/工作集/配置修订数（不含 soft-deleted，单列已清理数）/最近修订）并跳转演化页；
> 演化页区分“切换演进版本”与“恢复配置修订”；`restore_config` 恢复
> definition + manifest 策略文件 + `role_tools_json`，legacy 修订给 dry-run 警告；
> 旧 rollback 入口按 kind 分派到 switch/restore_config。
>
> **P5.4 进度（2026-11-25）**：应用演进版本前先展示只读 diff
> （`preview_switch`：文件 added/modified/unchanged、知识卡恢复/归档/人工保留、
> Charter 差异、工作集；`GET /roles/{slug}/versions/{v}/diff`），确认后再 switch；
> 非 active 按钮为“应用”，active 为“重新应用”，均不重跑评估。
>
> **调度接入（2026-11-24）**：scheduler 第 11 个域 `evolution`，阶段锁定在
> `maintenance` 后：`rsi_dispatch_reflections`、`rsi_reap_runs`、`rsi_soak_check`、
> `runtime_proposal_detect`；tick 不调模型，只查库/建 run/入库/回滚/检测。
>
> **前端（2026-11-24，核心）**：角色工坊新增“演化”页签：当前基线 + 工作集、
> 演进/配置修订切换、状态筛选、服务端分页、当前/RSI/基线/评分/完整性徽标、
> 切换/导出/批量清理、导入预览与 new_role/add_version；新增演化候选列表
> （状态/tier/编辑数/base/eval、入队评估、批准应用、拒绝并写理由）；工具页新增
> 运行时提案板（状态筛选/md 预览/人工流转/导出）；Inbox 聚合 `rsi_candidate` 与
> `runtime_proposal`；`npm run dump:api && npm run gen:api` 与 `npm run build` 通过。
> 后端全量 **1201 passed / 63 skipped**。剩余：P6 试点与发布（含人工 CDP 验收）。
>
> **P1/P2/P3/P4/P5 变更记录**：
> - RSI-OBS-003 的 “retro 写 proposed 角色卡” 在 P1 先记录 `rsi.retrospective` 事件；
>   proposed 卡写入随 P3 知识收割一并实现（P3 已支持 candidate 应用的 memory_card）。
> - RSI-EVAL-003 的“实际 L2 角色执行”已由 `eval_runner.ReplayRunner` 落地：
>   版本物化到 shadow home、临时 workspace 执行角色 runtime、evaluator 执行隐藏
>   verifier；`EvalWorker` 消费 `eval_runs` 队列。真实 repo 的 `git_snapshot`
>   支持按路径/`REPOS_DIR` clone + commit checkout；P6 试点按真实项目配置 item。
> - 隐藏集加密默认关闭（`rsi.eval.encrypt_hidden=0`）；P2 以独立目录/ACL/不暴露路径
>   为主防线，加密作为后续加固项。
> - P3 核心先落地候选/晋级/切换；SOAKING/auto-rollback 与评估门控晋级已由 P3.1
>   收口；reflection 输出自动入库已由 P3.2 收口（知识卡/候选/画像提案/RIP 证据 +
>   幂等 `rsi.reflection_ingested`）。

## 1. 范围

本 Spec 覆盖 v1.50 的六个交付面：

1. **角色 RSI**：任务完成观测 → 批量反思 → 有界候选 → 评估 → 人工晋级 → SOAKING。
2. **评估算法**：隐藏真实任务回放、held-in/held-out、配对统计、噪声门槛、`inconclusive`、
   diagnosis/direction 回馈 RSI。
3. **运行时改进提案（RIP/RTP）**：运行时部件问题 → 确定性检测/反思抽取 → 去重 →
   md → human 实施；agent 永不直接改运行时。
4. **版本管理**：版本 = RSI 演进的角色空间快照；切换/读档；压缩 CAS + manifest hash；
   实际表现标记；筛选/分页/批量清理。
5. **导出/导入**：`<role>@v<N>` 可移植包；new_role / add_version；依赖与安全检查。
6. **SDD/TDD 实施规范**：需求 ID、验收、测试矩阵、阶段 Gate。

非目标：
- 模型权重训练/微调；直连模型 API；
- RSI 自动改 Charter/runtime/tools/skills/MCP/model；
- 无人值守自动晋级；
- 合成评测任务；
- 替代现有 Cindy 任务级知识收割（RSI 是增量层）。

## 2. 术语

| 术语 | 定义 |
|---|---|
| **Charter** | 角色宪章：definition_json + role_tools + runtime 绑定；RSI 只读/只提案 |
| **Strategy** | instruction 策略段、PRINCIPLES、playbooks；RSI 可提案，人工晋级 |
| **Experience** | 角色私有知识：`notes/memory/cards/**` + `notes/knowledge/**`（scope=role） |
| **EvolutionVersion** | `role_versions.kind='evolution'`：完整角色空间快照，用户可见的“版本” |
| **ConfigRevision** | `role_versions.kind='config'`：手动配置修订，默认隐藏，不进演进主视图 |
| **Candidate** | 未激活的演进版本（`status=candidate`），来自 RSI 或导入 |
| **Switch** | 读档：把某个 EvolutionVersion 置为 active，下一次 run 生效 |
| **RIP/RTP** | 运行时改进提案（Runtime Improvement Proposal），供 human 实施 |
| **SOAKING** | 晋级后的在线观察窗（默认 5 episodes/14 天） |
| **Charter guard** | 静态 + LLM 校验，确保候选不违反 Charter/安全边界 |

## 3. 需求

优先级：P0 = v1.50 必须；P1 = v1.50 试点必须；P2 = 可延后但设计已包含。
每条需求至少一个自动化测试（前端可 CDP 脚本），并在 §8 测试矩阵登记。

### 3.1 版本、存档、切换（RSI-VER）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-VER-001 | 版本 = 角色空间快照 | Given 一次 RSI 晋级，When 生成 evolution 版本，Then 版本 manifest 含 Strategy + 全部 scope=role 知识卡 + Charter digest/配置引用，且不含 logs/task 快照/shared 知识 | P0/P3 |
| RSI-VER-002 | 手动配置修订与演进版本分离 | Given 人工修改 profile/tools/instruction，When 保存，Then 只生成 `kind=config` 修订，不改变 evolution active，不进入演进主视图 | P0 |
| RSI-VER-003 | 版本去重与合并 | Given content_hash 与同 kind 最新版相同，When 保存，Then 不新增版本；Given 60s 内同 actor 连续手动保存，Then 合并为一行 | P0 |
| RSI-VER-004 | 压缩 CAS 存档 | Given 创建版本，When 写存档，Then 每个文件以 sha256 命名存入 `space-cas/v1/*.z`（zlib；不可压/小文件 raw），manifest 记录 hash/size/encoding | P0 |
| RSI-VER-005 | Manifest 完整性 | Given 版本 manifest，When `manifest.sha256` 或任一 blob 被篡改，Then `verify` 失败、切换中止、`integrity_status=corrupt`，不自动删除 | P0 |
| RSI-VER-006 | 切换 = 完整读档 | Given 选旧版本 switch，When 执行，Then Strategy 文件 + 角色知识卡按 manifest 恢复；版本后 RSI 新建的卡归档到 `notes/archive/switch-vN/`；human 卡保留；human 编辑过的 RSI 卡 `partial_restore` | P3 |
| RSI-VER-007 | 当前版本与实际表现 | Given 角色在版本 v active 期间跑任务，When 查询 v，Then 显示 success/rework/token/quality（带 n）；run 创建时记录 `role_space_version` | P3 |
| RSI-VER-008 | 版本列表筛选/分页 | Given 600+ 版本，When 打开列表，Then 默认只查 evolution、服务端分页（默认 50）、可按 source/status/verdict/评分/实际表现/时间/关键词筛选，首屏 <1s | P5 |
| RSI-VER-009 | 批量删除 | Given 选择 archived/rejected 版本，When 批量删除，Then 先 dry-run；active/baseline/被引用版本拒绝；soft delete + 快照删除 + 审计；config 修订可单独批量清理 | P5 |
| RSI-VER-010 | 版本导出 | Given `role@vN`，When 导出，Then 生成 `role-<slug>-v<N>-<hash8>.anvil.zip`，含 export.json/manifest/manifest.sha256/files/charter.json/README，且不含 logs/task/shared/eval/密钥 | P5 |
| RSI-VER-011 | 版本导入 | Given 合法包，When `new_role`，Then 新建角色为 `source=import` baseline；When `add_version`，Then 同 slug 新 evolution 版本且不自动切换；缺 runtime/skill/MCP 时新角色 `enabled=0` + 依赖报告；Charter 不一致需 `allow_charter_change` | P5 |
| RSI-VER-012 | CAS GC | Given 删除版本后孤儿 blob，When GC，Then 按 manifest 引用计数删除、支持 dry-run、写审计；共享 blob 不删 | P0/P5 |
| RSI-VER-013 | 历史版本迁移 | Given 现有 600+ role_versions，When 迁移，Then 标记 `kind=config`、不设 active、按需生成 manifest/压缩 definition；启动不阻塞；人工预览后清理 | P0 |
| RSI-VER-014 | 工作集与基线分层 | Given 每日收割新增 RSI 角色卡，When 写入，Then 不生成 evolution 版本、不改 active manifest；`working_set_hash` 变化；run 记录 `(role_space_version, working_set_hash)`；晋级时工作集快照入版并清零；切换时工作集归档 | P5 |
| RSI-VER-015 | 配置修订恢复 | Given 手动配置修订，When 恢复，Then 基本信息页不再直接回退；演化页 dry-run 后恢复 definition + 策略文件 + `role_tools_json`；legacy 修订给出 warning；不改变 active evolution；写 `role.config_restored` | P5 |
| RSI-VER-016 | 应用前 diff | Given 应用 evolution 版本，When 打开确认，Then 只读返回文件 added/modified/unchanged、知识卡恢复/归档/人工保留、Charter 差异；取消不发请求，确认后才 switch；不重跑评估 | P5 |

### 3.2 完成观测与触发（RSI-OBS）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-OBS-001 | 每任务完成回执 | Given 任务 done/最终驳回/dispatch auto-done，When 完成钩子执行，Then 每个参与者有幂等 `rsi.observation`（key 含 completion_key）；重试不重复、reopen 后再完成产生新 observation | P1 |
| RSI-OBS-002 | 不阻塞任务 | Given RSI 入队/回填失败，When 任务完成，Then 任务状态不受影响；错误只记日志，scheduler backfill 补漏 | P1 |
| RSI-OBS-003 | 角色补复盘 | Given 角色执行 `anvil rsi retro --task`，When 写入，Then 生成 retro 回执 + ≤2 条 proposed 角色卡，记录 portrait refs | P1 |
| RSI-OBS-004 | 触发策略 | Given 新 observation，When 达到 ≥3 条/严重失败/每周/人工触发，Then 入队反思；冷却 24h、每日 1 次/每周 3 次、预算不足则 skip 并记录 | P1 |
| RSI-OBS-005 | 静默窗口与抢占 | Given 角色有活跃任务/待办/全局负载高/非静默时段，When due，Then defer；Given RSI 运行中任务唤醒，Then 终止反思、销毁槽、写 `rsi.preempted`，任务立即服务 | P1 |
| RSI-OBS-006 | Context 隔离 | Given RSI 反思 run，When 执行，Then 使用独立 `__rsi_reflect__` 槽、新会话（无 resume）、只读证据包/Strategy/聚合 diagnosis；任务会话与任务 slot 不受影响；不读 held-out/verifier/evaluator 代码 | P1 |
| RSI-OBS-007 | 角色状态 | Given 活跃 `rsi_reflect` run，When 查询角色状态，Then 派生 `evolving`；不写 task_role_claims/lease；不显示为任务 | P1 |
| RSI-OBS-008 | 按角色试点门 | Given 全局 `role_rsi` 开启，When 角色 `definition.rsi.enabled` 非 true，Then 不写 observation、不反思、不建 run；开关仅 human 可改 | P1 |

### 3.3 评估算法（RSI-EVAL）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-EVAL-001 | 隐藏回放集只来自真实任务 | Given 已完成 done 任务且有可复用 verifier，When 人工策展，Then 生成 item；不生成合成任务/变体；角色不能读写 suite | P2 |
| RSI-EVAL-002 | held-in/held-out | Given suite，When 建集，Then 至少 8+8；family（repo+mechanism）两侧不相交；held-in 只给方向，held-out 只做验收；不足 → inconclusive | P2 |
| RSI-EVAL-003 | 配对回放 | Given 候选与基线，When 回放，Then 同一 item 两臂、同 runtime/model/timeouts、顺序按 hash 平衡；shadow data/home、无 endpoint/token、真实 home 前后 hash 一致 | P2 |
| RSI-EVAL-004 | 确定性 verifier 优先 | Given L2，When 执行，Then hard checks 全过才 pass；forbidden changes 立即失败；超时/token cap → budget_exhausted；角色看不到 verifier 命令/期望输出 | P2 |
| RSI-EVAL-005 | 统计门控 | Given 配对结果，When 计算，Then 分层 bootstrap 95% CI、A/A noise floor、MDE 0.05；held-in/out Δ≥0 且至少一侧 CI 下界>noise 才 promote；负向/成本>1.5×/hard 违规 reject；其余 inconclusive | P2 |
| RSI-EVAL-006 | Diagnosis 回馈 RSI | Given 评估完成，When 输出，Then 含 dimensions/clusters/regressions/integrity/recommended_actions；不含 item ID/单例 held-out；RSI prompt 必须引用 diagnosis | P2/P3 |
| RSI-EVAL-007 | A/A 与环境 pin | Given 每次 eval，When 开始，Then 校验 evaluator/suite/runtime/model/Anvil 版本 hash；不匹配 abort；每 cycle 4 个 A/A item，漂移超阈值暂停晋级 | P2 |
| RSI-EVAL-008 | 漂移与 SOAKING | Given 版本 active，When 滚动 20 episodes 连续 2 窗口退化超 noise，Then 暂停晋级/告警；SOAKING 内触发自动回滚到上一批准版本 | P3 |
| RSI-EVAL-009 | 隐藏集隔离 | Given 隐藏集，When 存储/访问，Then 独立目录 0700 + ACL（可选 AES-GCM）；不进入角色投影/上下文/日志；diagnosis 不含 item；可选加密时 key 不进 agent env | P2 |
| RSI-EVAL-010 | 评估成本上限 | Given 评估，When 单 item >60k tokens / suite >1.5M / 角色 >1 次/周，Then abort → inconclusive + 通知 | P2 |
| RSI-EVAL-011 | 工作集卷积分解 | Given 版本 v 与工作集 W，When 评估候选编辑，Then 默认 baseline 为 `(v,W)`、candidate 为 `(v,W,Δ)`；diagnosis/eval_runs 记录两侧 working_set_hash；知识效应单独评估，报告中分列 edit/workingset/interaction | P5 |

### 3.4 反思、候选、晋级（RSI-CAND）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-CAND-001 | 反思 run | Given 触发，When 创建，Then `origin='rsi_reflect'`、`task_id=None`、`[anvil:rsi-reflect]`、`RSI_REFLECT_PROTOCOL`；`_run_prompt_kind` 识别；无 claim/submit 协议 | P3 |
| RSI-CAND-002 | 输出 schema | Given 反思输出，When 解析，Then 严格 JSON；schema 校验失败重试一次（仅格式），再失败 `reflection_failed`，不影响任务 | P3 |
| RSI-CAND-003 | 有界编辑 | Given 候选，When 校验，Then 每 cycle ≤2 候选、每候选 ≤2 编辑、每条 ≤400 字符、不得整段重写/跨角色导入/密钥/运行时目标 | P3 |
| RSI-CAND-004 | 候选归档 | Given 候选编辑，When 写入，Then 存 `proposals(kind/path/tier/manifest/predictions/diagnosis/dedupe_key)`，状态 CAS，active dedupe 唯一；no-op 写事件不建行 | P3 |
| RSI-CAND-005 | Charter/字段守卫 | Given 候选，When charter_digest 不一致/编辑含 Charter/runtime/tools/skills 变更/无 diagnosis_ref，Then 拒绝或降级为 proposal-only | P3 |
| RSI-CAND-006 | 人工晋级 | Given promote 结论，When human 批准，Then 原子应用、写 evolution active、审计/SSE；拒绝记录理由；不自动晋级 | P3 |
| RSI-CAND-007 | 巩固 | Given 5 个已应用候选或 30 天，When 巩固，Then 去重/合并/淘汰零命中；PRINCIPLES≤2048、Strategy 段≤4096、playbooks≤40 条/20KB、notes≤200 active | P3 |
| RSI-CAND-008 | 算子元学习 | Given 候选归档，When 排序，Then 按 attempts/promotes/regressions/mean delta + Laplace/UCB；近 20 次占比>60% 降权；预测准确性写回报告 | P3 |
| RSI-CAND-009 | 知识收割 | Given 反思知识项，When 写入，Then 角色私有卡自主（实践+质量+去重）；topic/channel 只 proposed；shared 只 lesson pending；越界不写角色空间 | P3 |
| RSI-CAND-010 | Charter 提案 | Given 职责/能力/工具/技能/MCP 变更，When 输出，Then 写现有 proposals/role_suggestion，人工审批；运行时/模型走 RIP | P3 |

### 3.5 运行时提案（RSI-RIP）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-RIP-001 | 分类与禁止目标 | Given runtime_issue，When 分类，Then 使用固定 taxonomy（skill/mcp_server/runtime_yaml/feature_flag/tool_preset/model/environment/harness_part）；eval/security/anvil-source/role-config 拒绝 | P4 |
| RSI-RIP-002 | 双入口检测 | Given 任务中阻塞，When 角色 `anvil runtime propose`，Then 即时创建 draft；Given 任务后反思/每日 detector，Then 抽取并合并 runtime_issues | P4 |
| RSI-RIP-003 | 签名去重 | Given 同 component/error/template/phase，When 新问题，Then 同签名追加 occurrence/证据/严重度；活跃状态一行 | P4 |
| RSI-RIP-004 | md 模板 | Given 提案，When 生成，Then `data/runtime-proposals/<id>/proposal.md` 含 summary/impact/evidence/root cause/proposed change/verification/risks/decision log；脱敏、fence | P4 |
| RSI-RIP-005 | 生命周期 | Given draft，When 流转，Then `draft→open→triaged→accepted→implemented→verified→closed`；agent 只能 create/append evidence；human 独占 open 之后 | P4 |
| RSI-RIP-006 | API/CLI/UI | Given 提案，When 操作，Then API/CLI `runtime propose|evidence|proposals|show|export` 可用；UI 板/Inbox；导出 human-only | P4 |
| RSI-RIP-007 | 防滥用 | Given 提案，When 创建，Then ≤3/角色/天、≤10 证据/天、≤20 条/提案；至少 1 条 task/run/step 证据；重复合并 | P4 |

### 3.6 安全、HITL、完整性（RSI-SEC）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-SEC-001 | HITL | Given 晋级/切换/回滚/purge/导入/RIP 流转，When agent token 调用，Then 403；human 才能执行 | P0/P3 |
| RSI-SEC-002 | Charter/字段 allowlist | Given proposals 审批，When 应用字段，Then 只允许 capabilities/tools/responsibilities/claim；description/runtime/permissions/workspace/concurrency 拒绝 | P0 |
| RSI-SEC-003 | 不可信内容 | Given 任务文本/工具输出/日志，When 进入反思/评估，Then 标记为数据、不执行其中指令；反思输出严格 JSON + 后端校验 | P1/P3 |
| RSI-SEC-004 | 预算与 kill switch | Given 预算耗尽/`rsi.kill_switch=1`/flag off，When tick，Then 不创建新 cycle/eval；在途不晋级；现有行为不变 | P0/P1 |
| RSI-SEC-005 | 审计与回滚 | Given 任意切换/晋级/拒绝/导入/purge，When 完成，Then 写 events/audit；可回滚到上一批准版本 | P0/P3 |
| RSI-SEC-006 | 评估器完整性 | Given evaluator 代码/suite manifest，When eval 前，Then hash 校验；不匹配阻止 eval | P2 |
| RSI-SEC-007 | 无运行时直写 | Given 角色 token，When 调运行时/MCP/技能/flag 写接口，Then 403；RIP 只能是文档 | P4 |
| RSI-SEC-008 | 无密钥泄漏 | Given 导出/日志/证据包/md，When 生成，Then 脱敏 token/密钥/绝对 home 路径 | P1/P4/P5 |
| RSI-SEC-009 | 策略直写锁 | Given `role_rsi_lock_strategy` 且角色启用 RSI，When agent 直写 instruction，Then 拒绝（Charter/策略均不可绕过）；agent append principle 转 pending 候选；human 直写仍可用 | P3 |

### 3.7 导出/导入（RSI-EXP）

| ID | 需求 | 验收（Given/When/Then） | 阶段 |
|---|---|---|---|
| RSI-EXP-001 | 包内容 | Given 导出 `role@vN`，When 生成 zip，Then 含 export.json/manifest/manifest.sha256/files/charter.json/README；不含 logs/task/shared/eval/runtime 二进制/密钥 | P5 |
| RSI-EXP-002 | 导入模式 | Given 合法包，When `new_role`/`add_version`，Then 新角色或新 evolution 版本；不自动启用/切换；缺依赖 `enabled=0` + 报告；Charter 不一致需 flag | P5 |
| RSI-EXP-003 | 导入安全 | Given zip，When 导入，Then 校验 hash/大小/zip-slip/符号链接/密钥；失败拒绝；隔离目录；human-only | P5 |
| RSI-EXP-004 | 导入来源 | Given 导入成功，When 查询版本，Then `source=import`、`note=imported from <slug>@v<N>@<hash>`、UI `导入` 徽标 | P5 |

## 4. 数据模型（实施约束）

以设计文档 §5.1 为准；Spec 只锁定不可变约束：

- 新增持久状态家族仅 2 个：`eval_runs`、`runtime_proposals`；
- observation/cycle 用 append-only `events`（`source='rsi_observation'`, `source_id=int64(sha1(role|task|completion_key)[:16])`）；
- 候选状态在 `proposals.status`（`draft|queued|evaluating|pending|applied|rejected|inconclusive|superseded|rolled_back|no_change`）；
- `role_versions` 新增 `kind`（`evolution|config`）、`status`、`source`、`content_hash`、`space_manifest_path/hash`、`charter_digest`、`eval_run_id`、`integrity_status`、`last_verified_at`、`deleted_at`；每个 slug 至多一个 active evolution；
- `runs.role_space_version` 记录 run 创建时的 active evolution 版本；
- `notes.origin`（human/rsi/system）、`source_cycle_id`、`source_role_version`、`last_modified_by`；
- 历史 `role_versions` 迁移为 `kind=config`，不设 active；首次启用 RSI 建 baseline；
- 单写者：`role_space`、`proposals.transition_candidate`、`role_eval`、`runtime_proposals.transition`、`ledger.emit_*`。

## 5. 契约（摘要）

### 5.1 API（全部 `/api/v1`）

| 能力 | 端点 | 权限 |
|---|---|---|
| 角色空间/诊断 | `GET /rsi/roles/{slug}/space`、`/diagnosis`、`/observations` | role self/human |
| retro | `POST /rsi/roles/{slug}/retro` | role self/human |
| 触发/候选 | `POST /rsi/roles/{slug}/reflect`、`GET /rsi/candidates`、`GET /rsi/candidates/{id}` | human/system / role self |
| 晋级/拒绝/回滚 | `POST /rsi/candidates/{id}/approve|reject|rollback`、`POST /rsi/roles/{slug}/versions/{v}/rollback` | human |
| 评估 | `POST /rsi/roles/{slug}/evaluations`、`GET /rsi/evaluations/{id}` | human |
| suite | `POST /rsi/suite/items`、`GET /rsi/suite/status` | human |
| 版本 | `GET /roles/{slug}/versions`（filters/page）、`summary`、`duplicates`、`{v}/manifest`、`{v}/performance` | role self/human（写仅 human） |
| 切换/恢复 | `POST /roles/{slug}/versions/{v}/switch`、`{v}/restore-config` | human |
| 存档 | `POST /roles/{slug}/versions/{v}/verify`、`GET .../archive`、`POST /roles/{slug}/versions/gc` | human |
| 导出/导入 | `GET /roles/{slug}/versions/{v}/export`、`POST /roles/import/preview`、`POST /roles/import`、`GET /roles/imports/{id}` | human |
| RIP | `GET/POST /runtime-proposals`、`{id}`、`{id}/evidence`、`{id}/transition`、`{id}/export` | agent 证据/human 流转 |

错误码：
- 403：agent 调 human-only；
- 404：role/version/candidate 不存在；
- 409：版本冲突、active 保护、重复 dedupe、并发 CAS 失败；
- 422：schema/Charter/字段 allowlist/超限；
- 429：预算/配额；
- 410：已删除/已过期。

### 5.2 CLI

```
anvil rsi show|retro|propose|candidates|candidate|approve|reject|rollback|eval|diagnosis
anvil rsi suite add|status
anvil runtime propose|evidence|proposals|show|export
anvil role export <slug> --version <v> --out <zip>
anvil role import --preview <zip>
anvil role import <zip> --mode new_role --slug <new>
anvil role import <zip> --mode add_version --slug <existing> [--allow-charter-change]
```

### 5.3 UI

- 角色工坊版本列表（复用 `Roles.tsx:564-595`）：主视图 evolution + 当前/RSI/评分/实际表现/
  存档 hash/完整性；次视图 config 修订；筛选/分页/批量清理；切换/导出。
- 导入角色：预览弹窗（hash/依赖/Charter diff/冲突/密钥）→ 确认。
- 角色卡 `evolving`；任务板不出现 RSI 任务；Inbox 增加 candidate/RIP。
- 工具页/治理页 RIP 板；治理页完整性面板（suite digest/A/A/kill switch/预算）。

## 6. 不变量（必须测试）

1. 每个 slug 至多一个 `kind=evolution AND status='active'`；
2. observation 幂等；候选 dedupe 唯一；
3. Charter digest CAS；候选无 Charter/runtime/tool/skill 变更；
4. 任务完成路径不被 RSI 阻塞；任务可抢占 RSI；
5. 隐藏集/verifier 不出现在角色上下文/日志/DB；诊断不含 item ID；
6. human-only：晋级/切换/purge/导入/RIP 流转；
7. 版本存档：manifest + 每文件 hash 校验通过才切换；损坏不自动删除；
8. 关闭所有 RSI flag 时，现有行为/延迟/UI 无变化。

## 7. 测试矩阵

> 详细测试文件规划见设计文档 §8；下表锁定“需求 → 测试 → 阶段”，实施时以实际测试名为准。

| 需求段 | 测试文件 | 阶段 |
|---|---|---|
| RSI-VER-001/006 | `tests/api/test_role_space_snapshot_completeness.py` | P0/P3 |
| RSI-VER-002/003 | `tests/api/test_role_versions_dedupe.py`（实际 `tests/test_role_versions_manage.py`） | P0 |
| RSI-VER-002/009 | `tests/test_role_versions_manage.py`（no-op 去重 + config dedupe） | P0/P5 |
| RSI-VER-015 | `tests/test_config_restore.py`（definition/文件/tools/legacy 警告） | P5 |
| RSI-VER-016 | `tests/test_role_space_diff.py`（应用前文件/知识卡/working set diff，只读） | P5 |
| RSI-VER-004/005/012 | `tests/api/test_role_space_archive.py` | P0/P5 |
| RSI-VER-007/008 | `tests/api/test_role_versions_list.py` | P3/P5 |
| RSI-VER-009 | `tests/api/test_role_versions_purge.py` | P5 |
| RSI-VER-010/011/EXP-001..004 | `tests/api/test_role_export_import.py` | P5 |
| RSI-VER-013 | 迁移测试（`tests/test_migrations.py` 或现有迁移测试） | P0 |
| RSI-OBS-001/002/003 | `tests/api/test_rsi_observations.py`、`tests/cli/test_rsi_cli.py` | P1 |
| RSI-OBS-004/005 | `tests/api/test_rsi_preemption.py` | P1 |
| RSI-OBS-006 | `tests/graph/test_rsi_context_isolation.py` | P1/P3 |
| RSI-OBS-007 | `tests/api/test_rsi_role_state.py` | P1 |
| RSI-OBS-008、RSI-SEC-009 | `tests/test_rsi_role_gate.py`、`tests/test_strategy_lock.py` | P1/P3 |
| RSI-EVAL-001..010 | `tests/api/test_role_eval.py`、`test_eval_context_isolation.py`、`test_rsi_safety.py` | P2 |
| RSI-EVAL-003/006/007、RSI-SEC-004 | `tests/test_eval_runner_replay.py`（L2 runner + queue worker） | P2/P3 |
| RSI-CAND-001..010 | `tests/api/test_rsi_candidates.py`、`tests/graph/test_rsi_protocol.py`、`test_prompt_snapshot.py` | P3 |
| RSI-CAND-001/002/009、RSI-SEC-003/008 | `tests/test_rsi_reflection_ingest.py` | P3/P4 |
| RSI-RIP-001..007 | `tests/api/test_runtime_proposals.py`、`tests/cli/test_rsi_cli.py` | P4 |
| RSI-SEC-001..008 | `tests/api/test_security_human_only.py`、`test_rsi_safety.py`、`test_contract_models.py` | P0–P5 |
| RSI-API/CLI/UI | `tests/api/test_contract_models.py`、`tests/cli/test_rsi_cli.py`、前端 `npm run build` + CDP | P0–P5 |

前端：仓库无单测框架，不强制引入；以 `npm run build` + CDP 手测为准；纯过滤/徽标逻辑
可选 Vitest（需 owner 同意）。

## 8. 阶段与 Gate

| Gate | 内容 | 退出标准 |
|---|---|---|
| **S0** | Spec 评审 | 用户确认需求/验收/测试矩阵；未决项取默认 |
| **P0** | 地基：迁移、表、CAS/manifest、flags、role_space 骨架、字段 allowlist、Spec 关联 | 迁移幂等；CAS 验证；flags off；全量测试绿 |
| **P1** | 观测/回执/触发/preemption/context/evolving | observation 覆盖；不阻塞；隔离测试 |
| **P2** | 评估 suite/runner/统计/diagnosis/A-A | inconclusive 规则；隐藏隔离；成本门 |
| **P3** | 反思/候选/晋级/switch/SOAKING/版本实际表现 | 候选校验；完整读档；自动回滚 |
| **P4** | RIP 检测/去重/md/API/CLI/UI | 去重；human 流转；模板 |
| **P5** | 版本列表/筛选/分页/批量清理/导出/导入 | 600+ 首屏<1s；round-trip；恶意包拒绝 |
| **P6** | 试点 2–3 角色 4 周 | 无回归；体验/成本/人工时间达标；go/no-go |

每阶段结束：目标测试 + 全量 `pytest` + `npm run build`；不通过不进入下一阶段。

### 8.1 P6 试点操作清单（owner）

1. **选角色**：2–3 个有真实 done 任务的角色；角色 `definition.rsi.enabled=true`（人工）。
2. **开开关**：`role_rsi`、`role_rsi_reflection`、`role_rsi_autonomous_knowledge`，
   评估另开 `role_rsi_evaluation`（建议先在离线窗口）；`role_rsi_lock_strategy=1`
   只对试点角色；`role_rsi_auto_rollback=1`。
3. **建基线**：人工在角色工坊“演化”页确认 `kind=evolution, source=baseline` active；
   历史手动版本保持 `kind=config` 折叠。
4. **策展回放集**：每个角色至少 8+8 条真实 done 任务且 family 不相交；`suite add` →
   `suite freeze`；item 的 verifier 必须由 evaluator 执行，角色不可见。
5. **跑循环**：任务 done → observation →（静默窗口）反思 → 候选；候选必须人工在
   Evolution/Inbox 审阅。
6. **评估**：候选行“评估”入队 → `EvalWorker` 单并发 shadow 回放 → `promote/reject/
   inconclusive`；只有 promote 可被人工批准；inconclusive 保留待补样本。
7. **晋级/观察**：人工批准生成 active 版本；SOAKING 期间看实际运行指标；
   连续退化超噪声 → 自动回滚到上一批准版本。人工可随时切换历史版本。
8. **度量**：任务完成 p95 增量 <50ms；因 RSI 导致的任务等待 = 0；每角色每周人工
   评审 ≤30 分钟；token 在预算内；无 Charter/safety 违规。
9. **停机**：`role_rsi=0` 或 kill switch 后不再新 cycle/eval；在途 run 允许完成但不
   自动晋级；所有版本/评估/负结果保留审计。

## 9. 未决项与默认

| # | 问题 | 默认 | Gate |
|---|---|---|---|
| 1 | Charter/Strategy 标记 | P0 引入标记 + UI 一键拆分；未拆分整份视为 Charter | S0 |
| 2 | L2 回放载体 | shadow data dir + 临时 role home；runtime 强制需要角色行时才用隐藏 eval 角色 | P2 |
| 3 | 隐藏集加密 | 默认关闭；独立目录+ACL 为主；`rsi.eval.encrypt_hidden=1` 可选 | P2 |
| 4 | RIP 导出 git 跟踪 | 导出但不自动 commit，由 human 决定 | P4 |
| 5 | 停用 agent 直写 instruction | `role_rsi_lock_strategy` 开启时转候选；试点角色必须开启 | P1/P3 |
| 6 | rubric judge | 默认非主评估器；无 verifier 时只产诊断，verdict 封顶 inconclusive | P2 |
| 7 | `data/agents` git | 可选；默认 DB 快照/CAS | P4 |
| 8 | 冻结手动策略编辑 | v1.50 不硬冻结；手动标 config，默认隐藏；P6 owner 决定 | P6 |

## 10. Spec 变更控制

- 任何需求/验收/契约变更：先改 Spec + 测试矩阵，再改代码；
- 变更记录：日期、变更人、需求 ID、影响测试/阶段；
- 代码与 Spec 冲突时，Spec 为真相源；若 Spec 本身有误，走变更记录后更新；
- 每个提交引用 Spec ID（如 `feat(rsi): version switch [RSI-VER-006]`）。

### 10.1 变更日志

- **2026-11-25（owner）**：补充设计文档 §12「v1.50 实现对齐」：反思入库、L2
  ReplayRunner/EvalWorker、应用前 diff、配置修订恢复/去重、按角色试点门、
  策略锁、RIP/detector 当前边界与已知限制；不改变既有需求验收，Spec 与代码
  继续以 `RSI-*` 测试矩阵为准。按 owner 要求随 `rsi` 分支提交设计文档与 Spec。

## 11. 参考

设计文档 `docs/v1.50-role-rsi.md` §10 已列全部论文与业界资料；Spec 不重复。
