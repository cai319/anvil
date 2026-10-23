"""后台调度器（threading.Thread）：替代手动 `POST /control/tick` 的常驻循环。

每 `SCHEDULER_INTERVAL` 秒：
  1. `governance.tick` —— 回收过期租约 + 为 todo 任务建 run（status=created，不 spawn）。
  2. 派发 —— pump 每个 `created` run 的 `wake` 事件（走图 project→spawn 拉起子进程）。
  3. monitor —— probe 每个 `running` run 的 session，已退出则 pump `process_exit`。
  4. reconcile —— 对账任务态与 run 态，愈合「端点改任务→图 pump」之间的崩溃窗口。

仅 `server.py`（`scheduler_enabled=True`）启动；默认 `create_app` 不启线程（测试安全）。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .api.events import publish
from .db import open_conn
from .platform import factory
from .services import daemon as daemon_service
from .services import governance, runs as runs_service, tasks as tasks_service
from .services import workflow as workflow_service


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
        self._backup_daily()
        conn = open_conn(self.db_path)
        try:
            governance.tick(conn, dispatch=True, plans_only=True)
            self._advance_workflows(conn)
            self._pump_created(conn)
            self._monitor(conn)
            self._reap_dead_runs(conn)
            self._reap_stalled_phases(conn)
            self._reconcile(conn)
        finally:
            conn.close()
        publish("tick", {})  # 通知前端刷新运行/任务状态

    def _backup_daily(self) -> None:
        """每天一次 SQLite 在线备份，并清理超过保留期的备份。"""
        if not config.BACKUP_ENABLED or self.db_path == ":memory:":
            return
        backups = Path(config.DATA_DIR) / "backups"
        try:
            backups.mkdir(parents=True, exist_ok=True)
            try:
                backups.chmod(0o700)
            except OSError:
                pass
            stamp = datetime.now().strftime("%Y%m%d")
            target = backups / f"anvil-{stamp}.db"
            if not target.exists():
                src = sqlite3.connect(self.db_path)
                try:
                    dst = sqlite3.connect(str(target))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
            cutoff = datetime.now().timestamp() - config.BACKUP_KEEP_DAYS * 86400
            for old in backups.glob("anvil-*.db"):
                try:
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 —— 备份失败不应阻断调度
            pass

    def _advance_workflows(self, conn) -> None:
        """推进所有 running workflow：依赖满足且角色空闲的步骤创建 run。"""
        for wf in conn.execute("SELECT id FROM workflows WHERE status='running'").fetchall():
            wid = wf["id"]

            def starter(role, task_id, prompt, step, _wid=wid):
                return runs_service.create_run(
                    conn, role, task_id, prompt=prompt,
                    step_id=step["step_id"], workflow_id=_wid)

            workflow_service.advance(conn, wid, starter=starter)
        # run 失败/失联 → 步骤进入自动恢复队列（指数退避重试，不 @human）
        rows = conn.execute(
            """SELECT s.workflow_id, s.step_id, r.status AS rs, r.failure_class
                 FROM workflow_steps s JOIN runs r ON r.id = s.run_id
                WHERE s.status='running' AND r.status IN ('failed','abandoned')"""
        ).fetchall()
        for r in rows:
            fc = r["failure_class"] or r["rs"]
            category = "environment" if fc in ("platform_timeout", "spawn_error") else "agent_error"
            workflow_service.fail_step(conn, r["workflow_id"], r["step_id"], fc, category)
        self._nudge_stalled(conn)

    @staticmethod
    def _waiting_for_human(conn, task_id: str, role: str) -> bool:
        """角色最后一条 @ 是 @human 且之后没有人类回复 → 合法 HITL 等待，不自动重试。"""
        timeline = tasks_service.timeline(conn, task_id)
        own = [i for i, e in enumerate(timeline)
               if e["kind"] == "mention" and e["role"] == role]
        if not own:
            return False
        last = own[-1]
        payload = timeline[last].get("payload") or {}
        if payload.get("target") != "human":
            return False
        return not any(e["role"] == "human" for e in timeline[last + 1:])

    @staticmethod
    def _waiting_for_busy_role(conn, task_id: str, role: str) -> bool:
        """角色最后一条 @ 指向另一个角色，且对方仍活跃 → 合法等待，不打断。

        对方有活跃 run（同任务或其它任务）或进程正忙/有排队消息，说明它还在处理；
        唤醒等待方只会重复 @、浪费一次 turn。对方空闲/失联时才唤醒等待方检查。
        """
        timeline = tasks_service.timeline(conn, task_id)
        own = [i for i, e in enumerate(timeline)
               if e["kind"] == "mention" and e["role"] == role]
        if not own:
            return False
        last = own[-1]
        target = (timeline[last].get("payload") or {}).get("target")
        if not target or target in ("human", role):
            return False
        # 对方已经 @ 回来 = 已响应
        if any(e["kind"] == "mention" and e["role"] == target
               and (e.get("payload") or {}).get("target") == role
               for e in timeline[last + 1:]):
            return False
        if conn.execute(
            "SELECT 1 FROM runs WHERE role_slug=? AND status IN (?,?,?) LIMIT 1",
            (target, *workflow_service.RUN_ACTIVE),
        ).fetchone():
            return True
        mgr = daemon_service.get_manager()
        if mgr is not None and getattr(mgr, "enabled", False):
            try:
                st = mgr.status(target)
            except Exception:  # noqa: BLE001
                st = {}
            if st.get("switching") or (
                    st.get("alive") and (st.get("status") == "busy"
                                         or int(st.get("queue_len") or 0) > 0)):
                return True
        return False

    def _nudge_stalled(self, conn) -> None:
        """步骤 run 挂起且长时间无活动 → 唤醒角色续跑；超过上限交给自动恢复（退避重排队）。"""
        now = datetime.now(timezone.utc)
        rows = conn.execute(
            """SELECT s.workflow_id, s.step_id, s.role_slug, s.run_id, s.last_nudge_at,
                      s.nudge_count, r.status AS run_status, w.task_id AS task_id,
                      (SELECT MAX(a.created_at) FROM activities a WHERE a.run_id=s.run_id) AS last_act,
                      s.started_at
                 FROM workflow_steps s
                 JOIN workflows w ON w.id = s.workflow_id
                 LEFT JOIN runs r ON r.id = s.run_id
                WHERE s.status='running'"""
        ).fetchall()
        for r in rows:
            rs = r["run_status"]
            # run 丢失/已结束但步骤仍 running：重置为 pending，让 advance 重新启动
            if rs is None or rs == "completed":
                conn.execute(
                    "UPDATE workflow_steps SET status='pending', run_id=NULL, started_at=NULL, "
                    "next_retry_at=NULL, updated_at=? WHERE workflow_id=? AND step_id=?",
                    (now.isoformat(), r["workflow_id"], r["step_id"]))
                conn.commit()
                continue
            if rs != "suspended":
                continue  # 仍在运行/投影中
            # 等待人类决策不是“卡住”：保持挂起，human 回复后由 mention 端点唤醒。
            if r["task_id"] and self._waiting_for_human(conn, r["task_id"], r["role_slug"]):
                continue
            # 等待另一个角色且对方仍在运行：同样不打断。
            if r["task_id"] and self._waiting_for_busy_role(conn, r["task_id"], r["role_slug"]):
                continue
            last = r["last_act"] or r["started_at"]
            if not last:
                continue
            try:
                parsed = datetime.fromisoformat(last)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                elapsed = (now - parsed).total_seconds()
            except (ValueError, TypeError):
                continue
            if elapsed < config.STEP_NUDGE_SECONDS:
                continue
            if r["last_nudge_at"]:
                try:
                    since = (now - datetime.fromisoformat(r["last_nudge_at"])).total_seconds()
                except (ValueError, TypeError):
                    since = config.STEP_NUDGE_SECONDS
                if since < config.STEP_NUDGE_SECONDS:
                    continue
            if int(r["nudge_count"] or 0) >= config.STEP_MAX_NUDGES:
                workflow_service.fail_step(
                    conn, r["workflow_id"], r["step_id"],
                    f"步骤停滞：{r['nudge_count']} 次唤醒后仍未提交", "environment")
                continue
            try:
                self.orchestrator.pump(self.db_path, r["run_id"], "resume", {})
            except Exception:  # noqa: BLE001
                continue
            conn.execute(
                "UPDATE workflow_steps SET last_nudge_at=?, nudge_count=nudge_count+1, updated_at=? "
                "WHERE workflow_id=? AND step_id=?",
                (now.isoformat(), now.isoformat(), r["workflow_id"], r["step_id"]))
            conn.execute(
                "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (r["role_slug"], r["task_id"], "step_nudge",
                 __import__("json").dumps({"workflow_id": r["workflow_id"],
                                           "step_id": r["step_id"]}, ensure_ascii=False),
                 now.isoformat()))
            conn.commit()

    def _pump_created(self, conn) -> None:
        for r in conn.execute("SELECT id FROM runs WHERE status='created'").fetchall():
            self.orchestrator.pump(self.db_path, r["id"], "wake", {})

    def _monitor(self, conn) -> None:
        pm = factory.get_platform()[0]
        rows = conn.execute(
            "SELECT id, task_id, native_session_id, started_at, timeout_seconds FROM runs "
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
                if st.get("switching"):
                    # 计划内上下文切换中：旧进程已退出、新进程尚未拉起，不是故障。
                    continue
                if not st.get("alive"):
                    self.orchestrator.pump(self.db_path, r["id"], "process_exit",
                                           {"exit_code": 1, "failure_class": "agent_exit_process"})
                    continue
                # 角色正忙在别的任务上（含 __global__ 的 reminder）：这个 run 只是
                # 排队，不能按 busy_since 判超时。
                if st.get("current_task_id") != r["task_id"]:
                    continue
                if st.get("busy_since") and _elapsed_seconds(st["busy_since"]) > timeout:
                    # 超时失败也按“新上下文重试”：终止常驻进程并清当前任务槽，
                    # 避免下一次唤醒继续 --resume 卡死的上下文；其它任务上下文保留。
                    # 换新上下文，但注入 CIC（记忆/工作流决策/知识快照），
                    # 避免自动恢复后的新会话对历史一无所知。
                    mgr.reset_session(role, cic=True, task_id=r["task_id"])  # type: ignore[union-attr]
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

    def _reap_dead_runs(self, conn) -> None:
        """常驻进程已死但 run 仍 running → 宽限期后转 suspended。

        process_exit 节点在「角色最后 @ 了别人」时会保留 running（等待对方），
        但如果进程实际已死（崩溃/被外部杀掉/上下文溢出回调未送达），这个 run
        会永久 running：workflow advance 不会再给该角色派步骤，看门狗也只处理
        suspended。这里做最后兜底，保证死进程不会永久占住角色。
        """
        mgr = daemon_service.get_manager()
        if mgr is None or not getattr(mgr, "enabled", False):
            return
        rows = conn.execute(
            "SELECT r.id, r.role_slug, r.task_id, rs.updated_at FROM runs r "
            "JOIN role_sessions rs ON rs.role_slug=r.role_slug "
            "WHERE r.status='running' AND r.native_session_id LIKE 'rs:%'"
        ).fetchall()
        now = datetime.now(timezone.utc)
        changed = False
        for row in rows:
            try:
                st = mgr.status(row["role_slug"])
            except Exception:  # noqa: BLE001
                continue
            alive = bool(st.get("alive"))
            # 上下文切换窗口内进程暂时不在，不能按死进程回收。
            if st.get("switching"):
                continue
            # 进程活着但正忙（或在处理排队消息）→ 这个 run 可能就在队列里，不能动。
            # 活着却空闲且队列为空 → 没有任何人在处理这个 running run，按死进程回收。
            if alive and (st.get("status") == "busy"
                          or int(st.get("queue_len") or 0) > 0):
                continue
            updated_at = row["updated_at"]
            if not updated_at:
                continue
            try:
                dead_since = datetime.fromisoformat(updated_at)
                if dead_since.tzinfo is None:
                    dead_since = dead_since.replace(tzinfo=timezone.utc)
                dead_for = (now - dead_since).total_seconds()
            except (ValueError, TypeError):
                continue
            if dead_for < config.RUN_DEAD_GRACE_SECONDS:
                continue
            if not alive:
                # 进程已死：下一次唤醒不要再 --resume 可能损坏的旧上下文。
                # 只清该 run 所属任务的槽，其它任务的上下文保持可恢复。
                try:
                    mgr.mark_needs_fresh_context(
                        row["role_slug"], "run reaped: dead role process",
                        task_id=row["task_id"])
                except Exception:  # noqa: BLE001
                    pass
            from .services import runs as runs_service
            runs_service.set_status(conn, row["id"], "suspended")
            changed = True
        if changed:
            conn.commit()

    def _reap_stalled_phases(self, conn) -> None:
        """run 卡在 projecting（编排器在 project→spawn 之间崩溃）→ 周期自愈。

        created run 每轮由 `_pump_created` 重投；这里只处理 projecting：
        - 已有 native_session_id：说明进程已拉起，只是状态没写回 → 修正为 running；
        - 没有会话：重新泵 wake，project/spawn 节点自身幂等。
        """
        rows = conn.execute(
            "SELECT id, native_session_id, started_at FROM runs WHERE status='projecting'"
        ).fetchall()
        for row in rows:
            if _elapsed_seconds(row["started_at"]) < config.RUN_PHASE_STALL_SECONDS:
                continue
            if row["native_session_id"]:
                from .services import runs as runs_service
                runs_service.set_status(conn, row["id"], "running")
                continue
            try:
                self.orchestrator.pump(self.db_path, row["id"], "wake", {})
            except Exception:  # noqa: BLE001 —— 下一轮 tick 继续尝试
                pass

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
