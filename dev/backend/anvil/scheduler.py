"""后台调度器（threading.Thread）：替代手动 `POST /control/tick` 的常驻循环。

每 `SCHEDULER_INTERVAL` 秒：
  1. `governance.tick` —— 回收过期租约 + 为 todo 任务建 run（status=created，不 spawn）。
  2. 派发 —— pump 每个 `created` run 的 `wake` 事件（走图 project→spawn 拉起子进程）。
  3. monitor —— probe 每个 `running` run 的 session，已退出则 pump `process_exit`。
  4. reconcile —— 对账任务态与 run 态，愈合「端点改任务→图 pump」之间的崩溃窗口。

仅 `server.py`（`scheduler_enabled=True`）启动；默认 `create_app` 不启线程（测试安全）。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from . import config
from .api.events import publish
from .db import open_conn
from .platform import factory
from .services import daemon as daemon_service
from .services import governance, runs as runs_service, workflow as workflow_service


def _elapsed_seconds(started_at: str | None) -> float:
    if not started_at:
        return 0.0
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)  # 无时区视为 UTC
        return (datetime.now(timezone.utc) - start).total_seconds()
    except (ValueError, TypeError):
        return 0.0


class Scheduler:
    def __init__(self, db_path: str, orchestrator, interval: float | None = None) -> None:
        self.db_path = db_path
        self.orchestrator = orchestrator
        self.interval = interval if interval is not None else config.SCHEDULER_INTERVAL
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="anvil-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 —— 单轮异常不终止循环，reconcile 自愈
                pass
            self._stop.wait(self.interval)

    def tick_once(self) -> None:
        conn = open_conn(self.db_path)
        try:
            governance.tick(conn, dispatch=True, plans_only=True)
            self._advance_workflows(conn)
            self._pump_created(conn)
            self._monitor(conn)
            self._reconcile(conn)
        finally:
            conn.close()
        publish("tick", {})  # 通知前端刷新运行/任务状态

    def _advance_workflows(self, conn) -> None:
        """推进所有 running workflow：依赖满足且角色空闲的步骤创建 run。"""
        for wf in conn.execute("SELECT id FROM workflows WHERE status='running'").fetchall():
            wid = wf["id"]

            def starter(role, task_id, prompt, step, _wid=wid):
                return runs_service.create_run(
                    conn, role, task_id, prompt=prompt,
                    step_id=step["step_id"], workflow_id=_wid)

            workflow_service.advance(conn, wid, starter=starter)
        # run 失败/失联 → 步骤失败并通知人类
        rows = conn.execute(
            """SELECT s.workflow_id, s.step_id, r.status AS rs, r.failure_class
                 FROM workflow_steps s JOIN runs r ON r.id = s.run_id
                WHERE s.status='running' AND r.status IN ('failed','abandoned')"""
        ).fetchall()
        for r in rows:
            workflow_service.fail_step(conn, r["workflow_id"], r["step_id"],
                                       r["failure_class"] or r["rs"])

    def _pump_created(self, conn) -> None:
        for r in conn.execute("SELECT id FROM runs WHERE status='created'").fetchall():
            self.orchestrator.pump(self.db_path, r["id"], "wake", {})

    def _monitor(self, conn) -> None:
        pm = factory.get_platform()[0]
        rows = conn.execute(
            "SELECT id, native_session_id, started_at, timeout_seconds FROM runs "
            "WHERE status='running' AND native_session_id IS NOT NULL"
        ).fetchall()
        for r in rows:
            if not r["native_session_id"]:
                continue
            timeout = r["timeout_seconds"] or config.RUN_TIMEOUT_SECONDS
            # 常驻会话（rs:<role>）：进程空闲等待是正常态，只有「忙」超时才回收。
            sid = r["native_session_id"]
            if sid.startswith("rs:"):
                role = sid[3:]
                mgr = daemon_service.get_manager()
                st = mgr.status(role) if mgr else {"alive": False}
                if not st.get("alive"):
                    self.orchestrator.pump(self.db_path, r["id"], "process_exit",
                                           {"exit_code": 1, "failure_class": "agent_exit_process"})
                    continue
                if st.get("busy_since") and _elapsed_seconds(st["busy_since"]) > timeout:
                    mgr.kill(role)  # type: ignore[union-attr]
                    self.orchestrator.pump(self.db_path, r["id"], "process_exit",
                                           {"exit_code": 1, "failure_class": "platform_timeout"})
                continue
            # 超时回收（P0-2）：run 级 timeout_seconds（spawn 时冻结自运行时 YAML）优先，
            # RUN_TIMEOUT_SECONDS 仅作未冻结时的兜底。续跑已重置 started_at（P0-1）。
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

    def _reconcile(self, conn) -> None:
        rows = conn.execute(
            """SELECT r.id AS run_id, r.status AS run_status, r.role_slug AS run_role,
                      t.id AS task_id, t.status AS task_status, t.assignee AS task_assignee
                 FROM runs r JOIN tasks t ON t.id = r.task_id
                WHERE r.status NOT IN ('completed','failed','abandoned')"""
        ).fetchall()
        for r in rows:
            rs, ts = r["run_status"], r["task_status"]
            # 只有任务负责人（assignee）的 run 才能代表任务进入评审/完成。
            # 同一任务可能绑定多个角色并行协作；非负责人 run 不能因为任务状态变化
            # 被误 pump submit（否则会出现「没提交却进入 reviewing」）。
            if r["task_assignee"] and r["run_role"] != r["task_assignee"]:
                continue
            if ts == "in_review" and rs in ("running", "submitted"):
                self.orchestrator.pump(self.db_path, r["run_id"], "submit", {})
            elif ts == "done" and rs == "reviewing":
                self.orchestrator.resume_review(self.db_path, r["run_id"], True)
            elif ts == "in_progress" and rs == "reviewing":
                self.orchestrator.resume_review(self.db_path, r["run_id"], False)
            # 注意：todo 任务有 run 是正常派发态（agent 认领前），不在此 abandon；由 monitor 收尾。
