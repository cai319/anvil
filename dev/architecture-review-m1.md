# Anvil M1 实现架构审查

**作者**：anvil-qa · **日期**：2026-09-16 · **对象**：dev commit `a5cd0b0`（M1 收尾）
**依据**：design.md §1.5 顶层架构 + 核心原则（文件是真相/SQLite 是索引、平台抽象、asyncio 单进程编排、原子认领、diff 三交集、凭据不落明文）
**结论**：分层（api→services→db）与平台抽象干净，但**并发模型有实质缺陷**（claim 非原子 + 共享连接跨线程），**文件真相与 DB 有漂移**，另有 1 处依赖逻辑倒置 + 若干参数/简化偏离。严重度分级如下。

---

## P1（高 · 违反设计核心原则 / 并发正确性）

### #1 claim 非原子（TOCTOU 竞态）
- **位置**：`backend/anvil/services/tasks.py:88-101`
- **问题**：`claim` 是 read-check-write（`get` → 查 status → `UPDATE`），不是 design §5.4 的 `BEGIN IMMEDIATE` + 条件 UPDATE（`WHERE status='todo' AND assignee IS NULL`，`rowcount==0 → 认领失败`）。并发两个 claim 都会读到 `todo`、都过检查、都 UPDATE，破坏「先到先得排他」核心原则。
- **修法**：包在 `BEGIN IMMEDIATE` 事务里，用条件 UPDATE + rowcount 判定抢到者。M2 要上多角色并发，这条必须修。

### #2 单一共享 sqlite3 连接跨线程、无锁
- **位置**：`backend/anvil/db.py:188`（`check_same_thread=False`）、`backend/anvil/deps.py:17-18`（`get_db` 返回共享 `app.state.db`）
- **问题**：FastAPI 同步端点跑在 anyio 线程池，多线程并发共用**同一个** sqlite3 connection。SQLite 连接对象非线程安全，并发写会报 "recursive use of cursors" 甚至损坏。**design 的「asyncio 单进程编排天然无竞态」被同步线程池模型绕开了**。
- **修法**（二选一，@anvil-se 裁决）：① 每请求独立连接（每次 `connect` 或 `threading.local`）；② 全局一把写锁串行化所有写。

### #3 角色 update 只写 DB，profile.yaml 漂移（文件真相被破坏）
- **位置**：`backend/anvil/services/roles.py:70-77`（update 只改 `definition_json`）、`roles.py:44-58`（`_ensure_home` 仅 create 时写 profile.yaml）
- **问题**：`PUT /roles/{slug}` 后，DB 里的 definition 变了、文件真相 profile.yaml 没刷新，二者分离。违背「文件是真相，SQLite 是索引」。同源：`services/memory.py:82-90` `_append_to_home_file` 静默吞 OSError、DB 反成权威。
- **修法**：update 时同步重写 profile.yaml；memory append 失败应报错而非静默。

---

## P2（中 · 正确性 bug / 参数偏差）

### #4 父子依赖检查方向反了
- **位置**：`backend/anvil/services/tasks.py:176-179`
- **问题**：`_assert_deps_ready` 检查「本任务的 parent 是否 done」，但 design §5.1 是「**父**任务在所有**子**任务 done 前不可 submit」。应检查「parent_id==本任务的子任务是否都 done」，而非「parent 是否 done」。逻辑倒置。

### #5 租约时长 360s（6min）≠ design 30min
- **位置**：`backend/anvil/services/tasks.py:14`（`LEASE_SECONDS = 360`）
- **问题**：design §5.5「租期 30 分钟」= 1800s。当前 360s。注释自认占位，但值本身错。

### #6 diff 越界只查 scope_paths + 硬编码 deny，缺三交集
- **位置**：`backend/anvil/services/tasks.py:132-135, 182-190`
- **问题**：design §4.3 是「scope_paths ∩ 角色 access ∩ .anvilignore」三交集，现只有 scope_paths + 写死 `DENIED_PREFIXES`。M2 范围，但三交集未预留接口。

### #7 FTS 建了却不用（死代码）
- **位置**：`backend/anvil/services/memory.py:22-29`（search 用 LIKE）、`memory.py:93-97`（`_index_fts` 写 memory_fts）
- **问题**：`search_memory` 用 `LIKE` 查 `memories`，但 `_index_fts` 又往 `memory_fts` 写（永不查询）。FTS 索引维护了却从未用，误导。M4 才上 FTS 可接受，但应删 `_index_fts` 或明确注释。

---

## P3（低 · 简化/偏离，需 @anvil-se 背书或标注）

8. **同步 sqlite3 替代 design「SQLAlchemy 2.0 async」**（全库）——P1#2 的根源。简化合理，需 SE 明确接受同步模型。
9. **git 走 subprocess CLI，非 design 的 pygit2**（`services/workspace.py`）——合理简化，需 SE 背书。
10. **probe 只做 `--version`，未做 health.probe_prompt 最小任务健康检查**（`services/runtime.py:124-138`）——design §2.4 含健康检查；M1 简化需标注。
11. **token 估算 = 固定 500/工具**（`services/roles.py:13`）——design §2.4 是 `len(json)/3.5`（±10%）；固定值误差大，M3 上真估算，需标注。
12. **darwin 凭据文件回退存明文、无 ACL**（`platform/darwin.py:101-111`）——仅 dev shim 回退，但 design「密钥不走明文」；建议至少 `chmod 600`。
13. **鉴权仅「存在 session 头」即可，无真实校验**（`deps.py:21-32`）——M1 已知缺口（M2 上真校验）；当前任何进程都能调任意端点，需列 M2 安全待办。

---

## 架构亮点（供 @anvil-se 确认，无需改）

- **平台抽象层**（base/factory/darwin/windows）ABC 干净，Windows Job Object（KILL_ON_JOB_CLOSE）+ Credential Manager 语义正确，macOS shim 进程组 killpg + 僵尸判态到位。
- **api → services → db 三层分离清晰**，api 层薄、services 持业务逻辑，职责边界对得上 design §1.5 的 8 模块。
- **runtime §2.6 存储分层**（内置 + 用户目录 + DB 只存发现态）实现到位。
- **5 态状态机 + 退出码 0–4** 契约对齐（L2/L3 已验证 49 passed）。

---

## 建议处理顺序

1. P1 #1 + #2 一起改（并发模型是 M2 的基座，现在改最便宜）——@anvil-dev 实现、@anvil-se 定同步/异步取舍。
2. P1 #3 + P2 #4 是正确性 bug，M1 收尾前改。
3. P2 #5 顺手改数值；#6/#7 标 TODO 留 M2。
4. P3 逐条由 @anvil-se 背书或标 TODO。
