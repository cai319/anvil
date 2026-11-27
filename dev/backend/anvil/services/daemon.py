"""常驻角色运行时（DaemonManager）：由 runtime/queue/context/subagent 组合。"""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..db import open_conn
from ..util import now_iso
from .daemon_common import (  # noqa: F401
    CONTEXT_SEP, GLOBAL_TASK_KEY, _ALL_SLOTS, _CONTEXT_OVERFLOW_MARKERS, _DeliveryError, _MODEL_VARIANT_SUFFIX, _PAYLOAD_OVERFLOW_MARKERS, _Queued, _Role, _SESSION_ENV_KEYS, _SpawnSpec, _USAGE_CAMEL, _aggregate_model_usage, _attachment_chars, _clean_env, _hard_limit_for, _is_context_overflow, _is_payload_overflow, clear_session, clear_slice_sessions, context_key, context_parts, extract_turn_usage, extract_usage, frame_user_message, logger, slice_key_of, task_id_of, task_key,
)
from .context_engine import ContextEngineMixin
from .delivery_queue import DeliveryQueueMixin
from .runtime_process import RuntimeProcessMixin
from .subagent_tracker import SubagentTrackerMixin


class DaemonManager(DeliveryQueueMixin, ContextEngineMixin,
                    RuntimeProcessMixin, SubagentTrackerMixin):
    """角色进程与投递会话管理器。"""

    def __init__(self, db_path: str, enabled: bool = True,
                 idle_release_minutes: float | None = None) -> None:
        self.db_path = db_path
        self.enabled = enabled
        self.idle_release_minutes = (
            idle_release_minutes if idle_release_minutes is not None
            else config.ROLE_IDLE_RELEASE_MINUTES
        )
        # 回调(role, exit_code, current_run_id)：由 create_app 注入，只泵实际执行的 run。
        self.on_exit = None
        # 回调(role, run_id, reason)：上下文溢出被主动/被动终止后，由 create_app
        # 负责清 run 的会话标记并用 CIC 新会话重投；daemon 不直接依赖编排器。
        self.on_context_overflow = None
        # 回调(role, run_id, task_id, workspace_path)：实际投递前投影当前任务。
        # 由 create_app 注入，避免 daemon 反向依赖 graph/nodes。
        self.on_project = None
        # 回调(run_id) -> prompt：写 stdin 前按最新 DB 重算提示词（wake_note/未读/
        # continuation 头）。失败时退回入队快照，且不消费 wake_note。
        self.on_render = None
        self._roles: dict[str, _Role] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._shutting_down = False
        self._idle_thread: threading.Thread | None = None
        if enabled:
            self._heal_db()
            # 进程重启后不可能有存活子代理；把上次运行残留的 running 标 interrupted。
            self._heal_subagents()
            self._idle_thread = threading.Thread(
                target=self._idle_loop, daemon=True, name="anvil-role-idle")

    def _role(self, role: str) -> _Role:
        with self._guard:
            return self._roles.setdefault(role, _Role(role=role))

    def _db_upsert(self, conn, r: _Role, status: str, pid: int | None = -1,
                   error: str | None = None) -> None:
        if pid == -1:
            pid = r.proc.pid if r.proc else None
        # dead/idle 不得携带 current_*；即使调用方忘了先清内存，DB 也不能留下幽灵绑定。
        current_task = r.current_task_id if status not in ("dead", "idle") else None
        current_run = r.current_run_id if status not in ("dead", "idle") else None
        current_step = r.current_step_id if status not in ("dead", "idle") else None
        current_context = r.context_key if status not in ("dead", "idle") else None
        current_slice = slice_key_of(current_context) if current_context else None
        busy_since = (r.busy_since if status not in ("dead", "idle") else None)
        conn.execute(
            "INSERT INTO role_sessions (role_slug, session_id, token, model_id, pid, status, "
            "busy_since, last_active_at, last_context_tokens, last_error, "
            "current_task_id, current_run_id, current_step_id, current_context_key, "
            "current_slice_key, sandbox_instance_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_slug) DO UPDATE SET session_id=excluded.session_id, token=excluded.token, "
            "model_id=COALESCE(excluded.model_id, role_sessions.model_id), "
            "pid=excluded.pid, status=excluded.status, busy_since=excluded.busy_since, "
            "last_active_at=excluded.last_active_at, "
            "last_context_tokens=COALESCE(excluded.last_context_tokens, role_sessions.last_context_tokens), "
            "last_error=excluded.last_error, current_task_id=excluded.current_task_id, "
            "current_run_id=excluded.current_run_id, current_step_id=excluded.current_step_id, "
            "current_context_key=excluded.current_context_key, "
            "current_slice_key=excluded.current_slice_key, "
            "sandbox_instance_id=excluded.sandbox_instance_id, "
            "updated_at=excluded.updated_at",
            (r.role, r.session_id or "", r.token, r.model_id, pid, status,
             datetime.fromtimestamp(busy_since, tz=timezone.utc).isoformat() if busy_since else None,
             datetime.fromtimestamp(r.last_active, tz=timezone.utc).isoformat() if r.last_active else None,
             r.context_tokens or None, error,
             current_task, current_run, current_step, current_context, current_slice,
             r.sandbox_instance_id, now_iso()),
        )
        conn.commit()
        # 当前任务上下文槽同步（session/水位/状态）；没有加载槽时跳过。
        if r.context_key:
            try:
                self._slot_upsert_conn(conn, r, status=status)
                conn.commit()
            except Exception:  # noqa: BLE001 —— 槽同步失败不应阻断进程状态写入
                pass

    def _heal_db(self) -> None:
        """启动自愈：终止上一实例遗留的孤儿角色进程，session_id 保留供 resume。"""
        conn = open_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT role_slug, pid FROM role_sessions WHERE pid IS NOT NULL").fetchall()
            for r in rows:
                pid = r["pid"]
                if not pid:
                    continue
                try:
                    # 只终止确实是 claude 的进程，避免 PID 复用误杀
                    out = subprocess.run(
                        ["ps", "-o", "command=", "-p", str(pid)],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=3).stdout
                    if "claude" in out:
                        os.kill(int(pid), 15)
                        self._log(r["role_slug"],
                                  f"[anvil] 启动自愈：终止上一实例遗留进程 pid={pid}")
                except Exception:  # noqa: BLE001
                    pass
            conn.execute(
                "UPDATE role_sessions SET status='dead', pid=NULL, busy_since=NULL, "
                "current_task_id=NULL, current_run_id=NULL, current_step_id=NULL, "
                "current_context_key=NULL, current_slice_key=NULL, "
                "updated_at=? WHERE status IN ('starting','idle','busy')", (now_iso(),))
            conn.execute("UPDATE role_task_sessions SET status='dead', updated_at=? "
                         "WHERE status IN ('idle','busy')", (now_iso(),))
            try:
                from . import sandbox as sandbox_service
                sandbox_service.reconcile_startup(conn)
            except Exception:  # noqa: BLE001 —— 观测表问题不能阻断启动
                pass
            conn.commit()
        finally:
            conn.close()

    def _log(self, role: str, text: str) -> None:
        if not text:
            return
        try:
            p = Path(config.AGENTS_DIR) / role / "logs" / "role-session.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"[{now_iso()}] {text}\n")
        except OSError:
            pass


# ---- 全局单例（create_app 初始化；测试不启用） -------------------------------

_manager: DaemonManager | None = None


def init_manager(db_path: str, enabled: bool) -> DaemonManager:
    global _manager
    _manager = DaemonManager(db_path, enabled=enabled)
    return _manager


def get_manager() -> DaemonManager | None:
    return _manager
