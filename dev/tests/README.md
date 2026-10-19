# Anvil 测试骨架（L2 API 集成 + L3 CLI 契约）

任务 #4 交付物。锚定 `se-design-v1.0.md` §3/§4 契约 + design.md §5.1/§10/§7.2。

## 目录

```
tests/
├── README.md              本文件
├── conftest.py            共享 fixture（app/testclient/临时 SQLite/鉴权/种子数据）
├── api/                   L2：FastAPI 集成测试（httpx + ASGITransport）
│   ├── test_runtimes.py   运行时注册/发现/健康检查
│   ├── test_roles.py      角色 CRUD/home/工具预算/配置快照
│   ├── test_tasks.py      任务 CRUD + 5 态状态机 + claim/租约/心跳/submit/review/deps
│   ├── test_memory.py     记忆 append/search、note、lessons、proposals（M4 骨架）
│   └── test_m2_governance.py  M2 协作与治理（并发 claim 原子性/依赖两守卫/diff 三交集/成本+并发闸门/冻结/审计；M2 未落地前整文件 skip）
└── cli/                   L3：anvil CLI 契约测试（子进程 + mock 后端）
    └── test_cli_contract.py  命令↔端点映射 + 退出码 0–4
```

## 运行

本目录位于项目开发路径 `dev/tests/`，与后端源码同根（`dev/backend/`、`dev/agentcli/`）。

```bash
cd /Users/ikaruga/cctest/aliceAgent/dev
pytest tests/ -q
```

后端 M1 落地前，conftest 会 `importorskip` 后端 app → 用例跳过（不报错）。dev 只需在
`dev/backend/anvil/main.py` 暴露 `app`（FastAPI 实例）和 `create_app(config)` 工厂即可被骨架拾取。

## 契约假设（实现前须注意，均已由 @anvil-se 决议）

- **状态机 5 态**（design §5.1）：`todo/in_progress/in_review/done/closed`。I1 已决议；
  注意与 runs 运行生命周期（§6.2 的 8 态）区分。
- **review 无 attempts 字段**（I2 已决议）：`review {approve|reject}`，approve→done、reject→in_progress。
- **submit 直达 in_review**（I3 已决议）：无 request-review，`submit` 是唯一 in_progress→in_review 动词。
- **鉴权**：CLI 走 `X-Anvil-Session` 头（design §10 末段），agent 自写端点用 `/api/v1/me/*`。
- **退出码**（design §7.3）：`0` 成功 / `1` 通用失败 / `2` 参数错误 / `3` 状态冲突 / `4` 无权限。

## 用例编号对照

`S1..S9 / C1..C2 / D1..D3 / G1..G6` 对应 `acceptance-baseline-v1.0.md` §2/§3/§4 的用例表。
每个 test 的 docstring 首行写编号，便于回归追踪。
