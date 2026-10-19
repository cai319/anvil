# Anvil M1 首轮测试问题清单（可复现）

**作者**：anvil-qa · **日期**：2026-09-16 · **对象**：dev commit `6a61c95` + `d0c076c`（M1 骨架）
**测试结果**：L2（API 集成）27 passed + L3（CLI 契约）21 passed = 48 passed，0 失败。
**结论**：契约主体一致（CLI↔REST 全表 + 退出码 0–4 全对齐），但发现 4 个 M1 相关缺口，按严重度如下。

---

## [高] #1 `anvil self show` 依赖 `GET /api/v1/me`，后端无此路由

- **复现**：`anvil self show`（ANVIL_ENDPOINT 指向后端）→ CLI 请求 `GET /api/v1/me` → 后端 404 → CLI 退出码 1。
- **证据**：`GET /api/v1/me` → 404（httpx 实测）；后端路由只有 runtimes/roles/tasks/memory/events，无 `/me`。
- **影响**：M1 出口「self show/whoami 返回身份」跑不通。
- **修法**：① @anvil-dev 补 `GET /api/v1/me`（返回自身 role + 并发额度 + 已用成本，SE §3.3）；或 ② @anvil-se 定 self show 改走 `GET /api/v1/roles/{slug}`（role profile 快照），CLI 改路径。二者取一。

## [中] #2 `anvil ws status/diff` → workspaces 端点 404

- **复现**：`anvil ws status` → `GET /api/v1/workspaces/0/tree` → 404。
- **证据**：后端无 workspaces 路由（workspace/Git 服务未实现）。
- **影响**：task #3 范围含「共享仓库 Git」，但 CLI ws 命令会 404。
- **修法**：@anvil-dev 确认 M1 补 workspace 服务（tree/diff），或降级 M2、CLI 命令暂标 TODO。

## [中] #3 CLI 默认端点 `127.0.0.1:8000` 与 design §10 的 `127.0.0.1:7317` 不一致

- **复现**：不设 `ANVIL_ENDPOINT` 时 CLI 默认连 8000，而后端按 design 应跑 7317。
- **证据**：`agentcli/anvil_cli/main.py` `ENDPOINT = os.environ.get("ANVIL_ENDPOINT", "http://127.0.0.1:8000")`。
- **影响**：默认配置下 CLI 连不上后端。
- **修法**：默认值改 `http://127.0.0.1:7317`。

## [低] #4 提前实现的 CLI 命令其后端未就位（M2/M4 范围）

- `anvil reminder list/add` → `GET/POST /api/v1/reminders` → 404（reminder 属 M4）。
- `anvil task create` 只有 body→title，缺 `--type` / `--parent` / `--title` 旗标（design §7.2）。
- `anvil task claim` 无 `--paths`（M2 认领路径声明）。
- **影响**：命令存在但调用报错或功能不完整；M1 不影响，但建议在 CLI 侧对未就位命令标 TODO 或返回明确「未实现」而非 404。

---

## 附：测试侧自身修正

- L3 `test_cli_contract.py`：mock 后端原记录 `self.path` 含 query string，导致 `memory search`（→ `/me/memory/search?q=x`）断言过严误报。已改为 `urlsplit(path).path` 只比对路径段。修正后 21 passed。
- 术语统一（runs/depends_on/instruction.md）：对 L2/L3 无影响——L2 已用 `depends_on`、未测 `/sessions`。
