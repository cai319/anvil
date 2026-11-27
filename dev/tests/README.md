# Anvil 测试说明

Anvil 的测试覆盖后端服务、FastAPI 集成、LangGraph/调度、CLI 契约与模型评测。
默认不依赖真实 Agent 模型；只有显式配置环境变量时才运行外部评测。

## 目录

```
tests/
├── conftest.py            共享 fixture（app/testclient/临时 SQLite/鉴权/种子数据）
├── api/                   L2：FastAPI 集成测试（httpx + ASGITransport）
├── graph/                 调度器、render、checkpoint、协议与步骤测试
├── cli/                   L3：anvil CLI 契约（子进程 + mock 后端）
└── test_*.py              顶层服务/单元测试（任务、角色、知识、RSI、问题板等）
```

## 运行

```bash
cd dev
source .venv/bin/activate
pytest tests/ -q
```

当前基线：**1265 passed / 63 skipped**。

跳过条件：

- `dev/.venv` 未安装 `langgraph` 时，依赖 LangGraph 的用例会在 `conftest` 中跳过；
- CLI 契约测试需要 `ANVIL_BIN=$(which anvil)`；
- 模型协作评测需要 `ANVIL_COLLAB_EVAL_CMD`。

只跑某个模块：

```bash
pytest tests/api/test_role_gate_api.py -q
pytest tests/api/test_inbox.py -q
pytest tests/api/test_clarification_api.py -q
```

## 契约假设

- **任务五态**：`todo/in_progress/in_review/done/closed`。`done` 是评审通过后的成功终态，
  `closed` 是人工终止，可重新打开回 `todo`。
- **runs 九态**：`created/projecting/running/suspended/submitted/reviewing/completed/failed/abandoned`。
  `suspended` 表示等待下一条消息，不是失败。
- **review 无 attempts 字段**：`review {approve|reject}`，approve→done、reject→in_progress。
- **submit 直达 in_review**：`submit` 是唯一 `in_progress→in_review` 动词。
- **鉴权**：CLI 走 `X-Anvil-Session` 头；Agent 只能操作绑定角色/任务；人类专属操作返回 403。
- **退出码**：`0` 成功 / `1` 通用失败 / `2` 参数错误 / `3` 状态冲突 / `4` 无权限。

## v1.51 覆盖

- `tests/test_clarification.py`、`tests/api/test_clarification_api.py`：任务简报、确认/跳过、
  stale、门禁与澄清 API。
- `tests/test_role_effort.py`、`tests/api/test_role_effort_api.py`：角色思考强度默认值与参数注入。
- `tests/api/test_role_gate_api.py`：pending/approved 角色建议门禁、authorize 唤醒与解锁。
- `tests/api/test_inbox.py`：按发送角色保留 @human、human-only 回应、澄清确认/终态抑制。
- `tests/api/test_feature_flags.py`：切回默认值清除覆盖。
- `tests/test_runtime_proposals.py`、`tests/api/test_tool_import.py`：运行时提案与工具导入契约。
