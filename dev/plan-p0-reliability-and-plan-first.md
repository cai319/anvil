# P0 可靠性地基 + P1-0 计划先行 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复续跑误杀/超时矛盾/停机孤儿三大可靠性缺陷，并落地「计划先行编排」（多角色任务先生成 workflow 草案、人工批准后按依赖执行）。

**Architecture:** P0 在 run 生命周期上加三个概念——run 级超时（spawn 时冻结自运行时 YAML）、`suspended` 状态（停机/挂起的可恢复态，区别于 failed）、失败分类（`failure_class`）；停机走 lifespan shutdown（终止子进程 + 转 suspended），启动走自愈（崩溃遗留 running → suspended）。P1-0 复用 `board_drafts`：planner 角色通过 `POST /tasks/{id}/plan` 以规划协议唤醒 → CLI `anvil board draft` 提交草案（`source_task` 关联）→ 人工 approve → 子任务树落在父任务下，依赖感知派发自动执行。

**Tech Stack:** Python 3.13 / FastAPI / SQLite / LangGraph / React+TS（Vite）。测试用 pytest + pytest-asyncio（httpx ASGITransport）与 graph conftest 的 FakePM。

**注意**：本仓库**不是 git 仓库**，计划中的「检查点」为运行测试套件，不含 git 提交。

**验证命令**（每个检查点）：
```bash
cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/ -q
# 基线：122 passed + 21 skipped（新测试会增加 passed 数）
```

---

## Part A · P0 可靠性地基

### Task 1: DB 迁移（新列）

**Files:**
- Modify: `dev/backend/anvil/db.py`（`init_schema` 末尾迁移区）

- [ ] **Step 1: 在 `init_schema` 的迁移区（`role_versions` 引导块之后、`conn.commit()` 之前）追加迁移**

```python
    # 迁移：P0 可靠性——run 级超时 / 失败分类；P1-0——任务规划字段 / 草案关联任务
    for col in ("timeout_seconds INTEGER", "failure_class TEXT"):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("plan_first INTEGER NOT NULL DEFAULT 0", "planner TEXT"):
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE board_drafts ADD COLUMN source_task TEXT")
    except sqlite3.OperationalError:
        pass
```

- [ ] **Step 2: 验证迁移不破坏现有测试**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/ -q`
Expected: `122 passed, 21 skipped`（与基线一致）

---

### Task 2: runs service —— mark_spawned / failure_class / suspend / heal

**Files:**
- Modify: `dev/backend/anvil/services/runs.py`
- Test: `dev/tests/test_runs_lifecycle.py`（新建）

- [ ] **Step 1: 写失败测试（新文件 `dev/tests/test_runs_lifecycle.py`）**

```python
"""P0 可靠性：mark_spawned 重置计时、failure_class、suspend_active、heal_stale_runs。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.db import connect
from anvil.services import runs as runs_service

pytest.importorskip("anvil.graph")  # 与其他测试同口径：无 langgraph 时跳过


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def _mk_run(conn: sqlite3.Connection, rid="R1", role="qa-role", task="T1",
            status="running", sid="pg:999", started="2026-01-01T00:00:00+00:00") -> None:
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at, native_session_id) "
        "VALUES (?,?,?,?,?,?)", (rid, role, task, status, started, sid),
    )
    conn.commit()


def test_mark_spawned_resets_started_at_and_stores_timeout(conn):
    _mk_run(conn, started="2026-01-01T00:00:00+00:00")
    runs_service.mark_spawned(conn, "R1", native_session_id="pg:4242", timeout_seconds=3600)
    r = runs_service.get_run(conn, "R1")
    assert r["native_session_id"] == "pg:4242"
    assert r["timeout_seconds"] == 3600
    assert r["started_at"] > "2026-01-01"  # 已重置为当前时间
    assert r["status"] == "running"


def test_set_status_records_failure_class(conn):
    _mk_run(conn, status="running")
    runs_service.set_status(conn, "R1", "failed", ended_at="2026-01-02T00:00:00+00:00",
                            failure_class="platform_timeout")
    r = runs_service.get_run(conn, "R1")
    assert r["status"] == "failed"
    assert r["failure_class"] == "platform_timeout"
    # 后续状态迁移不清空 failure_class
    runs_service.set_status(conn, "R1", "failed")
    assert runs_service.get_run(conn, "R1")["failure_class"] == "platform_timeout"


def test_suspend_active_marks_running_suspended(conn, monkeypatch):
    _mk_run(conn, rid="R1", status="running", sid="pg:111")
    _mk_run(conn, rid="R2", status="running", sid=None)      # 挂起等待中的 run
    _mk_run(conn, rid="R3", status="completed", sid="pg:333")  # 终态不动
    terminated: list[str] = []
    monkeypatch.setattr(
        "anvil.platform.factory.get_platform",
        lambda: (type("PM", (), {"terminate": staticmethod(lambda s: terminated.append(s)),
                                  "probe": staticmethod(lambda s: type("P", (), {"alive": True})())})(),),
    )
    n = runs_service.suspend_active(conn)
    assert n == 2
    assert terminated == ["pg:111"]  # 有进程的终止，无进程的只改状态
    assert runs_service.get_run(conn, "R1")["status"] == "suspended"
    assert runs_service.get_run(conn, "R2")["status"] == "suspended"
    assert runs_service.get_run(conn, "R3")["status"] == "completed"
    assert runs_service.get_run(conn, "R1")["ended_at"] is None  # 挂起不是结束


def test_heal_stale_runs_suspends_orphans(conn, monkeypatch):
    _mk_run(conn, rid="R1", status="running", sid="pg:111")  # 崩溃遗留（进程已死）
    _mk_run(conn, rid="R2", status="projecting", sid=None)
    monkeypatch.setattr(
        "anvil.platform.factory.get_platform",
        lambda: (type("PM", (), {"terminate": staticmethod(lambda s: None),
                                  "probe": staticmethod(lambda s: type("P", (), {"alive": False})())})(),),
    )
    out = runs_service.heal_stale_runs(conn)
    assert out["healed"] == 2
    assert runs_service.get_run(conn, "R1")["status"] == "suspended"
    assert runs_service.get_run(conn, "R2")["status"] == "suspended"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/test_runs_lifecycle.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'mark_spawned'`

- [ ] **Step 3: 实现——`runs.py` 中 `set_native_session` 之后追加三个函数**

```python
def mark_spawned(conn: sqlite3.Connection, run_id: str, native_session_id: str,
              timeout_seconds: float | None = None) -> None:
    """spawn 成功：写会话与 run 级超时，并重置 started_at（续跑重新计时，修 P0-1）。"""
    conn.execute(
        "UPDATE runs SET native_session_id=?, timeout_seconds=?, started_at=?, ended_at=NULL "
        "WHERE id=?",
        (native_session_id, timeout_seconds, now_iso(), run_id),
    )
    conn.commit()


def suspend_active(conn: sqlite3.Connection) -> int:
    """优雅停机：活跃 run（running/projecting）终止子进程并转 suspended（可 @mention 唤醒续跑）。

    suspended 不是失败：不写 ended_at、不写 failure_class；无进程的「挂起等待」run 同样归入。
    """
    from ..platform import factory  # 惰性导入，避免 services→platform 顶层循环

    pm = factory.get_platform()[0]
    n = 0
    for r in conn.execute(
        "SELECT id, native_session_id FROM runs WHERE status IN ('running','projecting')"
    ).fetchall():
        if r["native_session_id"]:
            try:
                pm.terminate(r["native_session_id"])
            except Exception:  # noqa: BLE001 —— 停机路径尽力而为
                pass
        set_status(conn, r["id"], "suspended")
        n += 1
    conn.commit()
    return n


def heal_stale_runs(conn: sqlite3.Connection) -> dict:
    """启动自愈：上一实例崩溃遗留的 running/projecting run 转 suspended（先终止孤儿进程）。"""
    from ..platform import factory

    pm = factory.get_platform()[0]
    healed = terminated = 0
    for r in conn.execute(
        "SELECT id, native_session_id FROM runs WHERE status IN ('running','projecting')"
    ).fetchall():
        sid = r["native_session_id"]
        if sid:
            try:
                if pm.probe(sid).alive:
                    pm.terminate(sid)  # 孤儿进程（其 CLI 回调已无服务端可寻）
                    terminated += 1
            except Exception:  # noqa: BLE001
                pass
        set_status(conn, r["id"], "suspended")
        healed += 1
    conn.commit()
    return {"healed": healed, "terminated": terminated}
```

同时修改 `set_status` 支持可选 `failure_class`（不清空语义：仅显式传入时更新）：

```python
def set_status(conn: sqlite3.Connection, run_id: str, status: str, ended_at: str | None = None,
               failure_class: str | None = None) -> None:
    """迁移 run 状态，并把迁移作为 activity 事件落库（供任务时间线/会话视图消费）。"""
    prev = conn.execute("SELECT status, task_id, role_slug FROM runs WHERE id=?", (run_id,)).fetchone()
    if failure_class is not None:
        conn.execute("UPDATE runs SET status=?, ended_at=?, failure_class=? WHERE id=?",
                     (status, ended_at, failure_class, run_id))
    else:
        conn.execute("UPDATE runs SET status=?, ended_at=? WHERE id=?", (status, ended_at, run_id))
    if prev and prev["status"] != status:
        conn.execute(
            "INSERT INTO activities (task_id, role_slug, run_id, kind, payload_json, created_at) VALUES (?,?,?,?,?,?)",
            (prev["task_id"], prev["role_slug"], run_id, "run", jdumps({"status": status, "prev": prev["status"]}), now_iso()),
        )
    conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/test_runs_lifecycle.py -q`
Expected: `4 passed`

---

### Task 3: spawn_node 重置计时 + run 级超时（P0-1/P0-2 核心）

**Files:**
- Modify: `dev/backend/anvil/graph/nodes.py`（`_render_invocation` 返回值、`spawn_node`）
- Test: `dev/tests/graph/test_spawn_timer.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""P0-1/P0-2：spawn 时重置 started_at 并冻结 run 级超时（来自运行时 YAML）。"""
from __future__ import annotations

import time

import pytest

from anvil.db import connect

pytest.importorskip("anvil.graph")
from anvil.graph import build_graph, get_checkpointer  # noqa: E402
from anvil.graph.orchestrator import Orchestrator  # noqa: E402
from anvil.platform import factory  # noqa: E402
from anvil.services import roles as roles_service  # noqa: E402
from anvil.services import runs as runs_service  # noqa: E402


class FakePM:
    def __init__(self):
        self.spawned = []

    def spawn(self, executable, args, cwd, env, stdout=None, stderr=None):
        self.spawned.append({"executable": executable, "args": args})
        return type("R", (), {"pid": 4242, "native_session_id": "pg:4242"})()

    def probe(self, sid):
        return type("P", (), {"alive": True})()

    def terminate(self, sid):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config := __import__("anvil.config", fromlist=["x"]), "DATA_DIR", tmp_path)
    (tmp_path / "agents" / "qa-role").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(factory, "get_platform", lambda: (FakePM(),))
    conn = connect(str(tmp_path / "anvil.db"))
    roles_service.create(conn, slug="qa-role", display_name="QA",
                         definition={"runtime": {"id": "claude-code"}})
    runs_service.create_run(conn, "qa-role", task_id=None)
    run = runs_service.list_runs(conn)[0]
    old_started = run["started_at"]
    time.sleep(0.05)
    orch = Orchestrator(str(tmp_path / "anvil.db"),
                        build_graph(checkpointer=get_checkpointer(str(tmp_path / "graph.db"))))
    yield conn, orch, run["id"], old_started
    conn.close()


def test_spawn_resets_started_at_and_freezes_timeout(env):
    conn, orch, run_id, old_started = env
    orch.pump(conn.execute("SELECT 1").fetchone() and (lambda: None)() or "", run_id, "wake", {}) if False else None
    orch.pump("", run_id, "wake", {})
    r = runs_service.get_run(conn, run_id)
    assert r["status"] == "running"
    assert r["native_session_id"] == "pg:4242"
    assert r["started_at"] > old_started          # 续跑重置计时（P0-1）
    assert r["timeout_seconds"] == 3600           # 冻结自 claude-code.yaml（P0-2）
```

（注：`orch.pump` 第一参数为 db_path 字符串；上面 `if False` 行是占位错误写法，实现时直接删除该行，只保留 `orch.pump("", run_id, "wake", {})` 前先看 orchestrator.pump 签名——见 Step 3 的修正说明。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/graph/test_spawn_timer.py -q`
Expected: FAIL（timeout_seconds 为 None / started_at 未重置）

- [ ] **Step 3: 实现**

`nodes.py` 的 `_render_invocation`（当前返回 `executable, args, cwd, env`）改为返回五元组（新增 `timeout`）：

```python
    inv = definition.get("invocation") or {}
    timeout = float(inv.get("timeout_seconds") or 3600)
    args = [render.render_template(str(a), ctx) for a in inv.get("args", [])]
    cwd = render.render_template(str(inv.get("cwd") or workspace_path), ctx)
    env = {k: render.render_template(str(v), ctx) for k, v in (inv.get("env") or {}).items()}
    return executable, args, cwd, env, timeout
```

`spawn_node` 中对应改为：

```python
        try:
            executable, args, cwd, env, timeout = _render_invocation(
                conn, run, task, effective, role, workspace_path)
            pm = factory.get_platform()[0]
            log_path = run.get("log_path")
            if log_path:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "ab") as logf:
                    result = pm.spawn(executable, args, cwd, env, stdout=logf, stderr=subprocess.STDOUT)
            else:
                result = pm.spawn(executable, args, cwd, env)
        except Exception:  # noqa: BLE001 —— 无法拉起子进程即失败，不做静默降级
            runs_service.set_status(conn, run_id, "failed", ended_at=now_iso(),
                                    failure_class="spawn_error")
            return {}
        runs_service.mark_spawned(conn, run_id, result.native_session_id, timeout_seconds=timeout)
```

（`mark_spawned` 不改 status；`spawn_node` 在其调用后保留原 `runs_service.set_status(conn, run_id, "running")` 一行。注意：runs.py 已有一个遗留别名 `start_run(conn, role_slug, task_id)`——新函数刻意命名 `mark_spawned` 避免同名冲突，勿动遗留别名。）

测试修正：`Orchestrator.pump` 的真实签名以 `anvil/graph/orchestrator.py` 为准（第一个参数是 db_path）。测试 fixture 里 `orch = Orchestrator(db_path, graph)`，调用写 `orch.pump(str(tmp_path / "anvil.db"), run_id, "wake", {})`。写测试时打开该文件核对签名后落笔，不要保留 Step 1 中的占位行。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/graph/test_spawn_timer.py tests/graph/ -q`
Expected: 全部 PASS（新增 1 passed）

---

### Task 4: process_exit_node —— 失败分类 + suspended 守卫

**Files:**
- Modify: `dev/backend/anvil/graph/nodes.py`（`process_exit_node`）
- Test: `dev/tests/graph/test_process_exit_class.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""P0-4：process_exit 的失败分类；suspended run 不被 process_exit 覆盖。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import runs as runs_service

pytest.importorskip("anvil.graph")
from anvil.graph.orchestrator import Orchestrator  # noqa: E402
from anvil.graph import build_graph, get_checkpointer  # noqa: E402


@pytest.fixture
def orch(tmp_path):
    db_path = str(tmp_path / "anvil.db")
    conn = connect(db_path)
    yield conn, Orchestrator(db_path, build_graph(checkpointer=get_checkpointer(str(tmp_path / "g.db"))))
    conn.close()


def _mk_run(conn, rid, status):
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at) VALUES (?,?,?,?,?)",
        (rid, "qa-role", None, status, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()


def test_nonzero_exit_classified(orch):
    conn, o = orch
    _mk_run(conn, "R1", "running")
    o.pump(conn.execute("SELECT 1").fetchone() is None and "" or "", "R1", "process_exit",
           {"exit_code": 2})
    r = runs_service.get_run(conn, "R1")
    assert r["status"] == "failed"
    assert r["failure_class"] == "agent_exit_2"


def test_platform_timeout_passthrough(orch):
    conn, o = orch
    _mk_run(conn, "R2", "running")
    o.pump("", "R2", "process_exit", {"exit_code": 1, "failure_class": "platform_timeout"})
    assert runs_service.get_run(conn, "R2")["failure_class"] == "platform_timeout"


def test_suspended_not_overwritten(orch):
    conn, o = orch
    _mk_run(conn, "R3", "suspended")
    o.pump("", "R3", "process_exit", {"exit_code": 1})
    assert runs_service.get_run(conn, "R3")["status"] == "suspended"  # 守卫：不动
```

（`o.pump` 第一参数同 Task 3 Step 3 的说明：写测试时按 orchestrator 真实签名传 db_path。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/graph/test_process_exit_class.py -q`
Expected: FAIL（无 failure_class / suspended 被覆盖）

- [ ] **Step 3: 实现——替换 `process_exit_node` 的守卫与分类段**

```python
def process_exit_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run:
            return {}
        # 依据 run 自身状态：已终态/已提交/挂起（suspended）则不动。
        if run["status"] in ("completed", "failed", "abandoned", "submitted", "reviewing", "suspended"):
            return {}
        # 挂起：若该角色最后 @提及了某个角色（等待响应），则保持 running，等 @回来再 resume。
        if _waiting_for_reply(conn, run.get("task_id"), run["role_slug"]):
            return {}
        payload = state.get("payload") or {}
        exit_code = payload.get("exit_code")
        # §7.4：退出但未 submit 永不 completed；非零→failed，否则 abandoned。
        # 失败分类（P0-4）：优先用泵入的分类（如 platform_timeout），否则按退出码归因。
        if exit_code not in (None, 0):
            failure_class = payload.get("failure_class") or f"agent_exit_{exit_code}"
            runs_service.set_status(conn, run_id, "failed", ended_at=now_iso(),
                                    failure_class=failure_class)
        else:
            runs_service.set_status(conn, run_id, "abandoned", ended_at=now_iso())
        return {}
    finally:
        conn.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/graph/ -q`
Expected: 全部 PASS

---

### Task 5: scheduler monitor —— run 级超时 + platform_timeout

**Files:**
- Modify: `dev/backend/anvil/scheduler.py`（`_monitor`）
- Test: `dev/tests/test_scheduler_timeout.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""P0-2：monitor 按 run 级 timeout_seconds 判超时（YAML 冻结值优先，RUN_TIMEOUT 兜底）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anvil.db import connect
from anvil.scheduler import Scheduler
from anvil.services import runs as runs_service

pytest.importorskip("anvil.graph")


class _NoOrch:
    def __init__(self):
        self.pumped = []

    def pump(self, db_path, run_id, event, payload):
        self.pumped.append((run_id, event, payload))


@pytest.fixture
def sched(tmp_path, monkeypatch):
    db_path = str(tmp_path / "anvil.db")
    conn = connect(db_path)
    monkeypatch.setattr("anvil.scheduler.config.SCHEDULER_INTERVAL", 999)
    s = Scheduler(db_path, _NoOrch())
    yield conn, s
    conn.close()


def _mk(conn, rid, started_delta_s, timeout_s):
    started = (datetime.now(timezone.utc) - timedelta(seconds=started_delta_s)).isoformat()
    conn.execute(
        "INSERT INTO runs (id, role_slug, task_id, status, started_at, native_session_id, timeout_seconds) "
        "VALUES (?,?,?,?,?,?,?)",
        (rid, "qa-role", None, "running", started, "pg:1", timeout_s),
    )
    conn.commit()


def test_run_level_timeout_wins(sched, monkeypatch):
    conn, s = sched
    _mk(conn, "R1", started_delta_s=700, timeout_s=3600)   # 超过旧 600s 默认，但 run 级 3600s 未到
    monkeypatch.setattr("anvil.platform.factory.get_platform",
                        lambda: (type("PM", (), {"terminate": staticmethod(lambda s: None),
                                                  "probe": staticmethod(lambda s: type("P", (), {"alive": True})())})(),))
    s.tick_once()
    assert runs_service.get_run(conn, "R1")["status"] == "running"  # 不误杀（P0-1 场景）


def test_run_level_timeout_expires(sched, monkeypatch):
    conn, s = sched
    _mk(conn, "R2", started_delta_s=100, timeout_s=50)     # run 级超时已过
    monkeypatch.setattr("anvil.platform.factory.get_platform",
                        lambda: (type("PM", (), {"terminate": staticmethod(lambda s: None),
                                                  "probe": staticmethod(lambda s: type("P", (), {"alive": True})())})(),))
    s.tick_once()
    assert s.orchestrator.pumped and s.orchestrator.pumped[0][1] == "process_exit"
    assert s.orchestrator.pumped[0][2].get("failure_class") == "platform_timeout"
```

（注意 `tick_once` 会跑 `governance.tick` → 需要 `tasks` 等表存在（connect 已建）。`_NoOrch.pump` 不真正驱动图，测试只断言泵入事件。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/test_scheduler_timeout.py -q`
Expected: FAIL（R1 被 600s 默认误杀 / 无 failure_class）

- [ ] **Step 3: 实现——`_monitor` 超时段替换**

```python
    def _monitor(self, conn) -> None:
        pm = factory.get_platform()[0]
        rows = conn.execute(
            "SELECT id, native_session_id, started_at, timeout_seconds FROM runs "
            "WHERE status='running' AND native_session_id IS NOT NULL"
        ).fetchall()
        for r in rows:
            if not r["native_session_id"]:
                continue
            # 超时回收（P0-2）：run 级 timeout_seconds（spawn 时冻结自运行时 YAML）优先，
            # RUN_TIMEOUT_SECONDS 仅作未冻结时的兜底。续跑已重置 started_at（P0-1）。
            timeout = r["timeout_seconds"] or config.RUN_TIMEOUT_SECONDS
            if _elapsed_seconds(r["started_at"]) > timeout:
                try:
                    pm.terminate(r["native_session_id"])
                except Exception:  # noqa: BLE001
                    pass
                self.orchestrator.pump(self.db_path, r["id"], "process_exit",
                                       {"exit_code": 1, "failure_class": "platform_timeout"})
                continue
            try:
                probe = pm.probe(r["native_session_id"])
            except Exception:  # noqa: BLE001
                continue
            if not probe.alive:
                self.orchestrator.pump(self.db_path, r["id"], "process_exit", {"exit_code": probe.exit_code})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/test_scheduler_timeout.py tests/test_scheduler.py -q`
Expected: 全部 PASS

---

### Task 6: lifespan —— 优雅停机 + 启动自愈

**Files:**
- Modify: `dev/backend/anvil/main.py`（lifespan、imports）

- [ ] **Step 1: 实现（lifespan 无法在单测里端到端验证，靠 Task 9 的手工验收）**

`main.py` 顶部 import 区追加：

```python
from .db import open_conn
from .services import runs as runs_service
```

（若 `create_app` 内已有 `path` 变量——Scheduler 构造处使用——直接复用。）

lifespan 替换为：

```python
    async def lifespan(app: FastAPI):
        # 启动自愈（P0-3）：上一实例崩溃遗留的 running/projecting run → suspended（可 @mention 续跑）
        conn = open_conn(path)
        try:
            runs_service.heal_stale_runs(conn)
        finally:
            conn.close()
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            scheduler.stop()
        # 优雅停机（P0-3）：终止活跃子进程（防孤儿烧钱），run 转 suspended（非 failed，可续跑）
        conn = open_conn(path)
        try:
            runs_service.suspend_active(conn)
        finally:
            conn.close()
```

- [ ] **Step 2: 检查点——现有测试不破坏**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/ -q`
Expected: 全部 PASS（lifespan 在测试 app 中也执行，空库时 heal 为 no-op）

---

### Task 7: API 暴露新字段

**Files:**
- Modify: `dev/backend/anvil/schemas.py`（`RunOut`、`TaskOut`）
- Modify: `dev/backend/anvil/services/tasks.py`（`create` 加 `plan_first`/`planner`；`get`/`list` 输出）
- Modify: `dev/backend/anvil/api/tasks.py`（create 透传）
- Test: `dev/tests/api/test_plan_first_api.py`（新建，Task 10 会扩展它）

- [ ] **Step 1: 先写 plan_first 字段的失败测试**

```python
"""P1-0 字段：任务可声明 plan_first + planner；RunOut 暴露 timeout/failure_class/suspended。"""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import auth_headers, role_payload, runtime_payload, task_payload  # noqa: E402


@pytest.fixture
async def seeded(client):
    await client.post("/api/v1/runtimes", json=runtime_payload(), headers=auth_headers())
    for slug in ("pm", "designer", "developer"):
        await client.post("/api/v1/roles", json=role_payload(slug), headers=auth_headers())


async def test_task_plan_first_roundtrip(client, seeded):
    r = await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF1", assignees=["pm", "developer"], plan_first=True, planner="pm"), headers=auth_headers())
    assert r.status_code == 201
    body = r.json()
    assert body["plan_first"] is True
    assert body["planner"] == "pm"
    got = await client.get("/api/v1/tasks/T-PF1", headers=auth_headers())
    assert got.json()["planner"] == "pm"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/api/test_plan_first_api.py -q`
Expected: FAIL（TaskIn 不接受 plan_first → 422 或字段缺失）

- [ ] **Step 3: 实现**

`schemas.py` `TaskIn` 加字段（`assignees` 行后）：

```python
    plan_first: bool = False
    planner: str | None = None
```

`TaskOut` 加：

```python
    plan_first: bool = False
    planner: str | None = None
```

`RunOut` 加：

```python
    timeout_seconds: float | None = None
    failure_class: str | None = None
```

`services/tasks.py` `create()` 签名加 `plan_first: bool = False, planner: str | None = None`，INSERT 的 tasks 列清单与 VALUES 增加两列（`plan_first` 存 `int(plan_first)`）。

`get()` 输出 dict 补 `"plan_first": bool(row["plan_first"]), "planner": row["planner"]`（`_row`/序列化处——打开 `get()` 找到 dict 组装点，把两个字段加进返回 dict）。

`api/tasks.py` `create_task` 透传：`plan_first=body.plan_first, planner=body.planner`。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/api/test_plan_first_api.py -q`
Expected: PASS

---

### Task 8: 前端 —— suspended 态与失败分类展示

**Files:**
- Modify: `dev/frontend/src/api/client.ts`（RunStatus 联合类型、Task 接口加 plan_first/planner）
- Modify: `dev/frontend/src/pages/RunSession.tsx`、`TaskDetail.tsx`、`AgentSessions.tsx`（各自的 RUN_LABEL/状态徽章映射加 `suspended`；run 详情显示 failure_class）

- [ ] **Step 1: client.ts**——`RunStatus` 联合加 `"suspended"`；`Task` 接口加 `plan_first: boolean; planner: string | null;`；`Run` 接口加 `failure_class: string | null; timeout_seconds: number | null;`

- [ ] **Step 2: 状态标签**——三个页面中形如 `const RUN_LABEL: Record<string, string> = {...}` 的映射统一补一行：

```ts
  suspended: "已挂起",
```

- [ ] **Step 3: RunSession.tsx 运行信息区**——在展示 status 的行旁追加（找到渲染 run 元信息的 JSX 段）：

```tsx
{run.failure_class && <span className="badge badge-in_progress" title="失败分类">{run.failure_class}</span>}
```

- [ ] **Step 4: 构建检查点**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev/frontend && npm run build`
Expected: tsc + vite build 成功

---

## Part B · P1-0 计划先行编排

### Task 9: board service —— agent 草案 + 挂父任务落板

**Files:**
- Modify: `dev/backend/anvil/services/board.py`
- Test: `dev/tests/test_board_plan_first.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""P1-0：agent 创建带 source_task 的草案；approve 落在父任务下并带 assignees/deps/验收。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import board as board_service

pytest.importorskip("anvil.graph")


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "b.db"))
    c.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES ('T1','五子棋','todo',?,?)",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    c.commit()
    yield c
    c.close()


TREE = {
    "id": "root", "title": "五子棋", "type": "epic", "leader": "pm",
    "feedback_rules": ["实现阻塞时 @pm", "需求歧义时 @pm，pm 决定是否升级 human"],
    "children": [
        {"id": "S1", "title": "设计 — 五子棋", "type": "design", "role": "designer",
         "acceptance": ["输出 docs/design.md"], "scope_paths": ["docs/**"]},
        {"id": "S2", "title": "实现 — 五子棋", "type": "implementation", "role": "developer",
         "depends_on": ["S1"], "acceptance": ["engine 测试通过"],
         "scope_paths": ["src/**", "tests/**"]},
    ],
}


def test_agent_draft_and_approve(conn):
    d = board_service.create_draft(conn, goal="五子棋 Web", tree=TREE,
                                   created_by="agent:pm", source_task="T1")
    assert d["source_task"] == "T1"
    assert d["status"] == "pending"
    out = board_service.approve(conn, [d["id"]])
    assert out["landed"] == [d["id"]]
    s2 = conn.execute("SELECT * FROM tasks WHERE id='S2'").fetchone()
    assert s2["parent_id"] == "T1"                      # 落在父任务下
    deps = [r["depends_on"] for r in conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id='S2'")]
    assert deps == ["S1"]
    roles = [r["role_slug"] for r in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id='S2'")]
    assert roles == ["developer"]
    acc = s2["acceptance_json"]
    assert "engine" in acc
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/test_board_plan_first.py -q`
Expected: FAIL（无 `create_draft` 函数）

- [ ] **Step 3: 实现——`board.py` 追加 `create_draft`，改造 `approve`/`_land_tree`**

```python
def create_draft(conn: sqlite3.Connection, goal: str, tree: dict,
                 created_by: str = "orchestrator", workspace_id: str | None = None,
                 source_task: str | None = None) -> dict:
    """agent（planner）或人创建草案。tree 节点字段：id/title/type/role/depends_on/
    acceptance/scope_paths/description；树级 leader 与 feedback_rules 落板时并入子任务描述。"""
    did = new_id()
    conn.execute(
        "INSERT INTO board_drafts (id, workspace_id, goal, tree_json, status, created_by, created_at, source_task) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (did, workspace_id, goal, jdumps(tree), "pending", created_by, now_iso(), source_task),
    )
    conn.commit()
    return get_draft(conn, did)  # type: ignore[return-value]
```

`approve` 中 `_land_tree` 调用改为传 `source_task`（当 draft 有 source_task 时，子任务的 `parent_id` 指向它）：

```python
def approve(conn: sqlite3.Connection, draft_ids: list[str], by: str = "human") -> dict:
    landed: list[str] = []
    for did in draft_ids:
        draft = get_draft(conn, did)
        if draft is None or draft["status"] != "pending":
            continue
        _land_tree(conn, draft["tree"], parent_id=draft.get("source_task"),
                   workspace_id=draft.get("workspace_id"))
        conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?", (now_iso(), did))
        landed.append(did)
    conn.commit()
    return {"landed": landed}
```

`_land_tree` 改为（支持 acceptance/scope_paths/description，feedback_rules 并入描述）：

```python
def _land_tree(conn: sqlite3.Connection, node: dict, parent_id: str | None,
               workspace_id: str | None, feedback_rules: list[str] | None = None) -> None:
    now = now_iso()
    feedback_rules = feedback_rules if feedback_rules is not None else (node.get("feedback_rules") or [])
    children = node.get("children") or []
    for child in (children if children else [node]):
        tid = child.get("id") or new_id()
        desc = child.get("description") or ""
        if feedback_rules:
            desc = (desc + "\n\n" if desc else "") + "反馈路径：\n" + "\n".join(f"- {r}" for r in feedback_rules)
        conn.execute(
            "INSERT INTO tasks (id, title, type, status, parent_id, workspace_id, description, "
            "scope_paths_json, acceptance_json, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, child.get("title", ""), child.get("type"), "todo", parent_id, workspace_id,
             desc or None, jdumps(child.get("scope_paths") or []), jdumps(child.get("acceptance") or []),
             "orchestrator", now, now),
        )
        for dep in child.get("depends_on", []):
            conn.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)", (tid, dep))
        if child.get("role"):
            conn.execute(
                "INSERT OR IGNORE INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
                (tid, child["role"], now),
            )
        for grandchild in child.get("children", []):
            _land_tree(conn, grandchild, parent_id=tid, workspace_id=workspace_id,
                       feedback_rules=feedback_rules)
```

- [ ] **Step 4: 运行测试（含既有 board/decompose 测试）**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/test_board_plan_first.py tests/ -q`
Expected: 全部 PASS（若旧 decompose 测试断言 parent_id=None 语义，按新语义更新该断言——approve 无 source_task 时 parent_id 仍为 None，不应有破坏）

---

### Task 10: 规划协议 + `POST /tasks/{tid}/plan` 端点

**Files:**
- Modify: `dev/backend/anvil/graph/render.py`（`PLANNING_PROTOCOL`）
- Modify: `dev/backend/anvil/graph/nodes.py`（`_prompt` 支持 extra 始终注入）
- Modify: `dev/backend/anvil/api/tasks.py`（新端点）
- Test: `dev/tests/api/test_plan_first_api.py`（扩展）

- [ ] **Step 1: 扩展失败测试**

在 `test_plan_first_api.py` 追加：

```python
async def test_plan_endpoint_creates_planner_run(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF2", assignees=["pm", "developer"], plan_first=True, planner="pm"),
        headers=auth_headers())
    r = await client.post("/api/v1/tasks/T-PF2/plan", headers=auth_headers())
    assert r.status_code == 201
    run = r.json()
    assert run["role_slug"] == "pm"
    assert "工作流规划者" in (run.get("prompt") or "")
    # 只拉起 planner，其他角色不动
    runs = await client.get("/api/v1/runs", headers=auth_headers())
    assert [x["role_slug"] for x in runs.json() if x.get("task_id") == "T-PF2"] == ["pm"]


async def test_plan_endpoint_rejects_non_assignee(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF3", assignees=["pm"], plan_first=True, planner="developer"),
        headers=auth_headers())
    r = await client.post("/api/v1/tasks/T-PF3/plan", headers=auth_headers())
    assert r.status_code == 409
```

（`RunOut` 若不含 `prompt` 字段：在 `RunOut` 加 `prompt: str | None = None`。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/api/test_plan_first_api.py -q`
Expected: FAIL（404 无 /plan 端点）

- [ ] **Step 3: 实现**

`render.py` `PROTOCOL` 之后追加：

```python
# P1-0 计划先行：规划者（leader）的专用协议。规划 → 草案 → @human → 挂起；不亲自实现。
PLANNING_PROTOCOL = (
    "你是本任务的工作流规划者（leader）。先规划、后协调，不亲自实现具体产出。\n"
    "第一步：执行 `anvil task claim $ANVIL_TASK_ID` 认领，`anvil task show $ANVIL_TASK_ID` 读取任务全文。\n"
    "第二步：把任务分解为子任务树，为每个子任务明确：标题、类型（design/implementation/verification/research/bugfix）、"
    "负责角色（role，必须是本任务参与角色之一）、依赖顺序（depends_on，引用其它子任务的 id）、验收标准（acceptance）、"
    "文件范围（scope_paths）。树级给出 leader 与 feedback_rules（出问题 @谁、何时升级 human）。\n"
    "第三步：把草案写入一个 JSON 文件并执行 `anvil board draft --task $ANVIL_TASK_ID --goal \"<目标一句话>\" --tree-file <tree.json>` 提交。"
    "tree.json 格式：{\"leader\":..., \"feedback_rules\":[...], \"children\":[{\"id\":\"S1\",\"title\":...,"
    "\"type\":..., \"role\":..., \"depends_on\":[], \"acceptance\":[...], \"scope_paths\":[...]}]}。\n"
    "第四步：执行 `anvil task mention $ANVIL_TASK_ID --to human \"工作流草案已生成，请审阅批准\"`，"
    "然后直接结束进程挂起等待。人工批准后系统会自动把子任务落板并按依赖派发，届时你被 @唤醒汇总。\n"
    "禁止：不要实现子任务内容；不要 submit 主任务；草案被驳回时按人工意见修订后重新提交。"
)
```

`nodes.py` `_prompt` 改为（extra 始终注入，规划指令不再被任务正文挤掉）：

```python
def _prompt(task: dict | None, transcript: str = "", extra: str = "") -> str:
    head = "\n".join(p for p in (task.get("title"), task.get("description")) if p) if task else ""
    parts = [p for p in (head, extra, transcript, render.PROTOCOL) if p]
    return "\n\n".join(parts)
```

（`_render_invocation` 里调用处已是 `_prompt(task, ..., run.get("prompt") or "")`，无需改动——prompt 作为 extra 注入。）

`api/tasks.py` 在 mention 端点之后追加：

```python
@router.post("/{tid}/plan", status_code=201, response_model=RunOut)
def plan_task(tid: str, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """P1-0 计划先行：唤醒 planner 角色生成工作流草案（只拉起 planner，其余角色等批准后自动派发）。"""
    _assert_session_task(auth, tid)
    t = services.tasks.get(conn, tid)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner = t.get("planner")
    if not planner:
        raise HTTPException(status_code=409, detail="task has no planner; set planner first")
    assignees = [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=?", (tid,))]
    if planner not in assignees:
        raise HTTPException(status_code=409, detail=f"planner {planner} is not a task assignee")
    from ..graph.render import PLANNING_PROTOCOL
    roster = "\n".join(
        f"- {slug}（@提及语法：anvil task mention {tid} --to {slug} \"消息\"）" for slug in assignees)
    prompt = PLANNING_PROTOCOL + "\n\n## 团队（Roster）\n" + roster
    run = services.runs.create_run(conn, planner, tid, prompt=prompt)
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    events.publish("task", {"id": tid, "kind": "plan", "planner": planner})
    return run
```

（`schemas.RunOut` 需含 `prompt`、`role_slug`、`task_id` 字段——若缺则补。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/api/test_plan_first_api.py -q`
Expected: PASS

---

### Task 11: board API + CLI `anvil board draft`

**Files:**
- Modify: `dev/backend/anvil/api/board.py`（POST /board/drafts、GET 带 source_task 过滤）
- Modify: `dev/agentcli/anvil_cli/main.py`（board 命令组）
- Test: `dev/tests/api/test_plan_first_api.py`（扩展）

- [ ] **Step 1: 扩展失败测试**

```python
async def test_agent_can_submit_draft(client, seeded):
    await client.post("/api/v1/tasks", json=task_payload(
        id="T-PF4", assignees=["pm", "developer"], plan_first=True, planner="pm"),
        headers=auth_headers())
    tree = {"leader": "pm", "feedback_rules": ["阻塞 @pm"],
            "children": [{"id": "S1", "title": "设计", "type": "design", "role": "designer"}]}
    r = await client.post("/api/v1/board/drafts", headers=auth_headers("pm"), json={
        "goal": "五子棋", "tree": tree, "source_task": "T-PF4", "created_by": "agent:pm"})
    assert r.status_code == 201
    did = r.json()["id"]
    # 人工批准（human session）
    r2 = await client.post("/api/v1/board/drafts/approve", headers=auth_headers(), json={"draft_ids": [did]})
    assert r2.status_code == 200 and r2.json()["landed"] == [did]
    s1 = await client.get("/api/v1/tasks/S1", headers=auth_headers())
    assert s1.status_code == 200
    assert s1.json()["parent_id"] == "T-PF4"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/api/test_plan_first_api.py -q`
Expected: FAIL（404 无 POST /board/drafts）

- [ ] **Step 3: 实现**

`api/board.py` 追加（imports 处补 `events`、`Auth/get_auth`、`Request`、`HTTPException` 已有）：

```python
from ..deps import get_auth, Auth  # 与其他 api 模块一致；若路径不同以 tasks.py 顶部 import 为准
```

```python
@router.get("/drafts")
def list_drafts(workspace_id: str | None = None, source_task: str | None = None,
                conn=Depends(get_db)):
    return services.board.list_drafts(conn, workspace_id, source_task=source_task)


@router.post("/drafts", status_code=201)
def create_draft(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """agent（planner）或人提交工作流草案。source_task 关联发起任务，approve 后子任务落其下。"""
    goal = body.get("goal")
    tree = body.get("tree")
    if not goal or not isinstance(tree, dict):
        raise HTTPException(status_code=422, detail="goal and tree are required")
    created_by = body.get("created_by") or (f"agent:{auth.role}" if auth.role else "human")
    if auth.role and body.get("source_task"):
        # agent 会话只能为绑定任务创建草案（防跨任务写入）
        if auth.task_id and body["source_task"] != auth.task_id:
            raise HTTPException(status_code=403, detail="session not bound to this task")
    return services.board.create_draft(
        conn, goal=goal, tree=tree, created_by=created_by,
        workspace_id=body.get("workspace_id"), source_task=body.get("source_task"),
    )
```

（`services.board.list_drafts` 签名加 `source_task: str | None = None` 过滤参数。`get_auth`/`Auth` 的 import 路径以 `api/tasks.py` 顶部为准，保持一致。）

`agentcli/anvil_cli/main.py` 追加命令组（`ws` 命令之后；沿用现有 `_post`/`_get` 辅助函数与 TOKEN/env 约定——打开文件头部确认辅助函数名后照其风格写）：

```python
@app.command("board")
def board(ctx: typer.Context):
    """工作流草案（P1-0 计划先行）。"""


@board.command("draft")
def board_draft(
    task: str = typer.Option(..., "--task", help="关联的主任务 id（ANVIL_TASK_ID）"),
    goal: str = typer.Option(..., "--goal", help="目标一句话"),
    tree_file: str = typer.Option(..., "--tree-file", help="子任务树 JSON 文件路径"),
):
    """planner 提交工作流草案（子任务树 + 分工 + 依赖 + 反馈路径）。"""
    tree = json.loads(Path(tree_file).read_text(encoding="utf-8"))
    out = _post("/board/drafts", {
        "goal": goal, "tree": tree, "source_task": task, "created_by": f"agent:{ROLE}",
    })
    console.print(f"草案已创建：{out['id']}（pending，等待人工批准）")


@board.command("drafts")
def board_drafts(source_task: str = typer.Option(None, "--task", help="按任务过滤")):
    """列出工作流草案。"""
    q = f"?source_task={source_task}" if source_task else ""
    for d in _get(f"/board/drafts{q}"):
        console.print(f"{d['id']}  {d['status']:8s}  {d['goal']}  by={d['created_by']}")
```

（`ROLE`/`json`/`Path`/`console` 等符号以文件内既有命名为准——`whoami`/`task` 命令已用同套辅助；不一致处以现有代码为准调整。）

- [ ] **Step 4: 运行测试确认通过 + CLI 冒烟**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/api/test_plan_first_api.py -q`
Expected: PASS

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev/agentcli && ANVIL_BIN=1 python3 -c "from anvil_cli.main import app; app(['board','drafts'])" 2>&1 | head -3`（或按仓库 CLI 契约测试方式冒烟；无服务端时允许连接错误，验证命令注册成功即可）

---

### Task 12: 前端 —— 计划先行 UI

**Files:**
- Modify: `dev/frontend/src/pages/Lobby.tsx`（创建任务表单：计划先行开关 + planner 下拉）
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`（「生成工作流」按钮 + 草案卡片 + 批准/驳回）
- Modify: `dev/frontend/src/api/client.ts`（`createTask` 支持选项、`planTask`、`listDrafts`、`approveDraft`）

- [ ] **Step 1: client.ts**——`createTask` 改签名：

```ts
  createTask: (title: string, opts?: { assignees?: string[]; plan_first?: boolean; planner?: string }): Promise<Task> =>
    req("POST", "/tasks", { title, ...opts }).then((r) => r.json()),
  planTask: (id: string): Promise<Run> =>
    req("POST", `/tasks/${id}/plan`).then((r) => r.json()),
  listDrafts: (sourceTask?: string): Promise<unknown[]> =>
    req("GET", `/board/drafts${sourceTask ? `?source_task=${sourceTask}` : ""}`).then((r) => r.json()),
  approveDraft: (draftIds: string[]): Promise<{ landed: string[] }> =>
    req("POST", "/board/drafts/approve", { draft_ids: draftIds }).then((r) => r.json()),
```

（`req` 为文件内既有请求辅助——以实际命名为准。）

- [ ] **Step 2: Lobby.tsx**——toolbar 增加状态与控件（`selectedRoles` 旁）：

```tsx
<label className="role-check">
  <input type="checkbox" checked={planFirst}
    onChange={(e) => setPlanFirst(e.target.checked)} />
  计划先行
</label>
{planFirst && selectedRoles.length > 0 && (
  <select value={planner} onChange={(e) => setPlanner(e.target.value)}>
    <option value="">选择规划者…</option>
    {selectedRoles.map((s) => <option key={s} value={s}>{s}</option>)}
  </select>
)}
```

state：`const [planFirst, setPlanFirst] = useState(false); const [planner, setPlanner] = useState("");`
`create()` 内 `api.createTask(title.trim(), planFirst && planner ? { assignees: selectedRoles, plan_first: true, planner } : undefined)`。

- [ ] **Step 3: TaskDetail.tsx**——任务卡操作区（「派发全部参与角色」按钮旁）追加：

```tsx
{task.plan_first && task.status === "todo" && (
  <button className="primary" disabled={busy}
    onClick={() => act(() => api.planTask(task.id))}>生成工作流</button>
)}
```

页面加载与 SSE 刷新时拉取关联草案（`useEffect` 内、refresh 里追加）：

```tsx
api.listDrafts(task?.id).then(setDrafts).catch(() => {});
```

`const [drafts, setDrafts] = useState<any[]>([]);`，渲染（任务卡下方）：

```tsx
{drafts.filter((d) => d.status === "pending").map((d) => (
  <div key={d.id} className="card">
    <strong>工作流草案 · {d.goal}</strong>
    <div className="muted">by {d.created_by}</div>
    <div className="card-actions">
      <button className="small" onClick={() => act(async () => { await api.approveDraft([d.id]); })}
        disabled={busy}>批准并落板</button>
    </div>
  </div>
))}
```

- [ ] **Step 4: 构建检查点**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev/frontend && npm run build`
Expected: 构建成功

---

### Task 13: 全量回归 + 文档 + 手工验收

- [ ] **Step 1: 后端全量**

Run: `cd /Users/ikaruga/cctest/aliceAgent/dev && python3 -m pytest tests/ -q`
Expected: 全部 PASS（基线 122 + 新增 ~10）

- [ ] **Step 2: 更新 `dev/implementation-status.md`**——在「本轮已补齐」区追加 P0/P1-0 小节（续跑重置计时、run 级超时、suspended、失败分类、停机自愈、计划先行），更新测试计数。

- [ ] **Step 3: 手工验收（由 owner 在 UI 操作，脚本不得代劳评审）**

```bash
cd /Users/ikaruga/cctest/aliceAgent/dev/backend && python3 run_server.py
```

验收清单（对照 optimization-roadmap.md 的 P0/P1-0 验收标准）：
1. 五子棋任务（4 角色、计划先行、planner=pm）→「生成工作流」→ PM 产出草案 → 人工批准 → 子任务按依赖自动派发
2. 子任务运行 >15 分钟不被误杀（可用 `ANVIL_RUN_TIMEOUT` 调小验证超时路径：超时的 run `failure_class=platform_timeout`）
3. 运行中重启服务（Ctrl-C 再启动）→ run 全部转「已挂起」、无孤儿 claude 进程（`ps aux | grep "claude --print"`）→ @mention 任一挂起角色能唤醒续跑
4. 任务详情/运行详情显示「已挂起」与失败分类徽章

---

## Self-Review 记录

- 覆盖检查：P0-1（Task 2/3）、P0-2（Task 3/5）、P0-3（Task 2/6）、P0-4（Task 2/4/7/8）、P1-0 全链路（Task 7/9/10/11/12）——roadmap P0/P1-0 全部有对应任务。P1-1/1-2（共享仓库/scope）不在本计划，后续独立计划。
- 类型一致性：`mark_spawned(conn, run_id, native_session_id, timeout_seconds)`、`set_status(..., failure_class=None)`、`create_draft(conn, goal, tree, created_by, workspace_id, source_task)`、`approve(conn, draft_ids, by)` 在各任务间签名一致。
- 已知风险点：`Orchestrator.pump` 签名与 `RunOut` 字段集以仓库实际代码为准（计划内已标注核对步骤）；Lobby「指派并执行」对 plan_first 任务应只拉 planner——本计划通过「生成工作流」按钮引导，未硬禁旧路径（避免过度耦合，P1-0 验收观察后再定）。
