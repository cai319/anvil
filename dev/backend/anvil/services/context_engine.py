"""上下文引擎：会话槽、窗口阈值、CIC 构建与溢出/轮次处理。"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..db import open_conn
from ..util import jdumps, jloads, now_iso
from . import ledger
from .daemon_common import (
    _ALL_SLOTS, _MODEL_VARIANT_SUFFIX, _Role, _hard_limit_for, clear_session,
    clear_slice_sessions, context_key, context_parts, logger, slice_key_of,
    stage_of, task_id_of,
)


class ContextEngineMixin:
    def status(self, role: str) -> dict:
        r = self._roles.get(role)
        if r is None:
            window = int(config.DEFAULT_CONTEXT_WINDOW)
            return {
                "alive": False, "status": "dead", "busy_since": None, "pid": None,
                "context_tokens": 0, "model_id": None, "served_model_id": None,
                "model_pool": [], "context_window_floor": None,
                "active_context_window": window, "context_window": window,
                "context_fit": True, "context_safe_fit": True,
                "cic_threshold_tokens": (
                    int(config.CIC_THRESHOLD_TOKENS) if config.CIC_THRESHOLD_TOKENS > 0
                    else int(window * config.CIC_RESET_RATIO)),
                "hard_limit_tokens": _hard_limit_for(window),
                "hint_tokens": int(window * config.CONTEXT_HINT_RATIO),
                "payload_chars": 0,
                "payload_limit_chars": int(config.PAYLOAD_HARD_RESET_CHARS),
                "current_task_id": None,
                "current_run_id": None,
                "current_step_id": None,
                "current_slice_key": None,
                "context_task_id": None,
                "context_slice_key": None,
                "context_key": None,
                "queue_len": 0,
                "queue": [],
                "switching": False,
                "runtime_env_error": None,
                "active_subagents": [],
            }
        alive = r.proc is not None and r.proc.poll() is None
        tokens = int(r.context_tokens or 0)
        active_window = self._active_context_window(r)
        return {
            "alive": alive,
            "status": "busy" if (alive and r.busy) else ("idle" if alive else "dead"),
            "busy_since": datetime.fromtimestamp(r.busy_since, tz=timezone.utc).isoformat()
            if (alive and r.busy and r.busy_since) else None,
            "pid": r.proc.pid if r.proc else None,
            "session_id": r.session_id,
            "current_run_id": r.current_run_id,
            "current_task_id": r.current_task_id,
            "current_step_id": r.current_step_id,
            "current_slice_key": slice_key_of(r.context_key) if r.current_task_id else None,
            "context_task_id": task_id_of(r.context_key),
            "context_slice_key": slice_key_of(r.context_key),
            "context_key": r.context_key,
            "switching": bool(r.switching),
            "runtime_env_error": r.runtime_env_error,
            "queue_len": len(r.queue),
            "queue": [
                {"run_id": q.run_id, "task_id": q.task_id, "slice_key": q.slice_key}
                for q in r.queue
            ],
            "active_subagents": [
                dict(info) for info in r.active_subagents.values()
                if info.get("status") == "running"
            ],
            "context_tokens": tokens,
            "model_id": r.model_id,
            "served_model_id": r.served_model_id,
            "model_pool": list(r.model_pool),
            "context_window_floor": r.context_window_floor,
            "active_context_window": active_window,
            "context_window": self._context_window(r),
            "context_fit": tokens <= active_window,
            "context_safe_fit": tokens <= self._context_window(r),
            "model_pool_windows": [
                {"model": m, "context_window": self._model_window(m),
                 "fits": tokens <= self._model_window(m)}
                for m in r.model_pool
            ],
            "cic_threshold_tokens": self._cic_threshold(r),
            "hard_limit_tokens": self._hard_threshold(r),
            "hint_tokens": self._hint_threshold(r),
            "payload_chars": int(r.payload_chars or 0),
            "payload_limit_chars": int(config.PAYLOAD_HARD_RESET_CHARS),
        }

    def reset_session(self, role: str, cic: bool = False,
                      task_id=_ALL_SLOTS, slice_key: str | None = None,
                      all_slices: bool = False) -> None:
        """清空上下文槽并释放进程；下一次唤醒新建上下文。

        - `task_id` 省略：清空该角色的**所有 task/slice 槽**（角色级 reset）；
        - 指定 task_id + slice_key：只清该 slice；
        - 指定 task_id 且 slice_key=None：只清 task 级槽；
        - `all_slices=True`：清该 task 的全部 slice 槽（任务级重置）。
        daemon 只在被清槽正是当前加载槽时才杀进程；其它槽清了也不影响当前工作。

        cic=True 时，下一次唤醒先注入 CIC（MEMORY 头 + 最近消息/动作），适合
        角色自主决定“换新上下文，但保留必要交接”的场景。
        """
        clear_all = task_id is _ALL_SLOTS
        key = None if clear_all else context_key(task_id, slice_key)
        r = self._roles.get(role)
        if r is not None:
            with r.lock:
                # task 级 reset 只移除目标 context 的排队消息；角色级 reset 才清全部。
                if clear_all:
                    r.queue.clear()
                elif all_slices:
                    r.queue = [q for q in r.queue if q.task_id != task_id]
                else:
                    r.queue = [
                        q for q in r.queue
                        if context_key(q.task_id, q.slice_key) != key]
                active = clear_all or (
                    task_id_of(r.context_key) == task_id if all_slices
                    else r.context_key == key)
                if active:
                    r.suppress_exit = True
                    # 计划内重置同样有杀人窗口；monitor 不能误判为进程死亡。
                    r.switching = True
                    try:
                        self._slot_persist(r)
                        self._kill_locked(r, graceful=True)
                    finally:
                        r.suppress_exit = False
                        r.switching = False
                    self._slot_clear_memory(r)
                    r.session_id = None
                    r.context_tokens = 0
                    r.slot_seen_activity = 0
                    r.payload_chars = 0
                    r.cic_pending = cic
                    self._clear_current_locked(r)
                    conn = open_conn(self.db_path)
                    try:
                        self._db_upsert(conn, r, status="dead")
                        if r.queue:
                            self._flush_next_locked(conn, r)
                    finally:
                        conn.close()
        conn = open_conn(self.db_path)
        try:
            clear_session(
                conn, role, "session reset",
                task_key_value=_ALL_SLOTS if clear_all else key,
                all_slices=all_slices)
        finally:
            conn.close()

    def reset_task_slices(self, role: str, task_id: str,
                          note: str = "workflow restarted") -> None:
        """清掉某 task 的全部 slice 槽，保留 task 级槽；新 execution 从新会话开始。"""
        r = self._roles.get(role)
        if r is not None:
            with r.lock:
                r.queue = [
                    q for q in r.queue
                    if not (q.task_id == task_id and q.slice_key)]
                active = (
                    task_id_of(r.context_key) == task_id
                    and slice_key_of(r.context_key) is not None)
                if active:
                    r.suppress_exit = True
                    r.switching = True
                    try:
                        self._kill_locked(r, graceful=True)
                    finally:
                        r.suppress_exit = False
                        r.switching = False
                    self._slot_clear_memory(r)
                    self._clear_current_locked(r)
                    conn = open_conn(self.db_path)
                    try:
                        self._db_upsert(conn, r, status="dead")
                        if r.queue:
                            self._flush_next_locked(conn, r)
                    finally:
                        conn.close()
        conn = open_conn(self.db_path)
        try:
            clear_slice_sessions(conn, role, task_id, note)
        finally:
            conn.close()

    def mark_needs_fresh_context(self, role: str, note: str = "fresh context required",
                                 task_id=_ALL_SLOTS,
                                 slice_key: str | None = None,
                                 all_slices: bool = False) -> None:
        """标记上下文槽下次唤醒使用全新 CIC 会话（不杀进程、不清排队消息）。

        task_id 省略时作用于当前加载槽；指定 task_id + slice 时只作用于该 slice；
        指定 task_id 且 slice=None 时只作用于 task 级槽；`all_slices=True`
        才作用于该 task 的所有 slice。
        """
        r = self._role(role)
        with r.lock:
            if task_id is _ALL_SLOTS:
                key = r.context_key
                active = key is not None and r.context_key == key
            elif all_slices:
                key = None
                active = task_id_of(r.context_key) == task_id
            else:
                key = context_key(task_id, slice_key)
                active = r.context_key == key
            if active:
                r.session_id = None
                r.cic_pending = True
                r.context_tokens = 0
                r.payload_chars = 0
                r.loaded_spec = None
                r.context_key = None if task_id is _ALL_SLOTS else r.context_key
        conn = open_conn(self.db_path)
        try:
            if key is not None:
                conn.execute(
                    "UPDATE role_task_sessions SET session_id='', context_tokens=NULL, "
                    "last_error=?, updated_at=? WHERE role_slug=? AND task_key=?",
                    (note, now_iso(), role, str(key)))
            elif task_id is not _ALL_SLOTS:
                conn.execute(
                    "UPDATE role_task_sessions SET session_id='', context_tokens=NULL, "
                    "last_error=?, updated_at=? WHERE role_slug=? AND task_id=?",
                    (note, now_iso(), role, task_id))
            if active:
                conn.execute(
                    "UPDATE role_sessions SET session_id='', last_context_tokens=NULL, "
                    "current_context_key=NULL, current_slice_key=NULL, current_stage=NULL, "
                    "last_error=?, updated_at=? WHERE role_slug=?",
                    (note, now_iso(), role))
            conn.commit()
        finally:
            conn.close()

    def _slot_persist(self, r: _Role, status: str = "idle", conn=None) -> None:
        """把当前加载槽的内存状态写回 DB；没有加载槽时安全返回。"""
        if not r.context_key:
            return
        own = conn is None
        c = conn or open_conn(self.db_path)
        try:
            self._slot_upsert_conn(c, r, status=status)
            c.commit()
        finally:
            if own:
                c.close()

    def _slot_upsert_conn(self, conn, r: _Role, status: str = "idle") -> None:
        key = r.context_key
        if not key:
            return
        spec = r.loaded_spec
        now = now_iso()
        slot_status = "dead" if status == "dead" else (
            "busy" if status in ("busy", "starting") else "idle")
        last_active = (
            datetime.fromtimestamp(r.last_active, tz=timezone.utc).isoformat()
            if r.last_active else now)
        task_id, slice_key = context_parts(key)
        stage = stage_of(key)
        conn.execute(
            """INSERT INTO role_task_sessions
                   (role_slug, task_key, task_id, slice_key, stage, session_id, runtime_id,
                    cwd, workspace_path, model_id, status, context_tokens,
                    last_seen_activity_id, last_active_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(role_slug, task_key) DO UPDATE SET
                   task_id=excluded.task_id,
                   slice_key=excluded.slice_key,
                   stage=excluded.stage,
                   session_id=excluded.session_id,
                   runtime_id=COALESCE(excluded.runtime_id, role_task_sessions.runtime_id),
                   cwd=COALESCE(excluded.cwd, role_task_sessions.cwd),
                   workspace_path=COALESCE(excluded.workspace_path,
                                           role_task_sessions.workspace_path),
                   model_id=COALESCE(excluded.model_id, role_task_sessions.model_id),
                   status=excluded.status,
                   context_tokens=excluded.context_tokens,
                   last_seen_activity_id=CASE
                       WHEN excluded.last_seen_activity_id>0
                       THEN excluded.last_seen_activity_id
                       ELSE role_task_sessions.last_seen_activity_id END,
                   last_active_at=excluded.last_active_at,
                   updated_at=excluded.updated_at""",
            (r.role, key, task_id, slice_key, stage, r.session_id or "",
             spec.runtime_id if spec else None,
             spec.cwd if spec else None, spec.workspace_path if spec else None,
             r.model_id, slot_status, int(r.context_tokens or 0),
             int(r.slot_seen_activity or 0), last_active, now, now),
        )
        # 同步角色级镜像：当前加载槽的 session / 水位。
        # idle/dead 槽不能把 current_* 写回角色镜像；只有 starting/busy 才代表执行绑定。
        if status in ("busy", "starting"):
            current_task, current_run, current_step = (
                r.current_task_id, r.current_run_id, r.current_step_id)
        else:
            current_task = current_run = current_step = None
        conn.execute(
            "UPDATE role_sessions SET session_id=?, model_id=COALESCE(?, model_id), "
            "last_context_tokens=?, current_task_id=?, current_run_id=?, "
            "current_step_id=?, current_context_key=?, current_slice_key=?, "
            "current_stage=?, updated_at=? WHERE role_slug=?",
            (r.session_id or "", r.model_id, int(r.context_tokens or 0),
             current_task, current_run, current_step,
             r.context_key if current_task else None,
             slice_key if current_task else None,
             stage if current_task else None, now, r.role),
        )

    def _slot_load(self, conn, role: str, key: str):
        return conn.execute(
            "SELECT * FROM role_task_sessions WHERE role_slug=? AND task_key=?",
            (role, key),
        ).fetchone()

    def _slot_seen_activity(self, conn, role: str, task_id: str | None,
                            slice_key: str | None = None) -> int:
        """当前上下文已见的最大活动 id；__global__ 槽不跟踪任务时间线。"""
        if not task_id:
            return 0
        clause, params = self._activity_scope(task_id, slice_key)
        row = conn.execute(
            f"SELECT COALESCE(MAX(id),0) AS m FROM activities WHERE {clause}",
            params,
        ).fetchone()
        return int(row["m"]) if row else 0

    @staticmethod
    def _activity_scope(task_id: str, slice_key: str | None) -> tuple[str, tuple]:
        """Task-wide comments are visible to every slice; other slices are not."""
        if slice_key:
            return ("task_id=? AND (slice_key=? OR slice_key IS NULL)",
                    (task_id, slice_key))
        return ("task_id=? AND slice_key IS NULL", (task_id,))

    def _slot_handoff(self, conn, role: str, task_id: str | None,
                      slice_key: str | None, since_id: int) -> str:
        """恢复会话时注入「离开期间的新进展」，避免角色用过时状态继续工作。"""
        if not task_id:
            return ""
        clause, params = self._activity_scope(task_id, slice_key)
        rows = conn.execute(
            f"""SELECT id, role_slug, kind, payload_json, created_at
                 FROM activities
                WHERE {clause} AND id>? AND (role_slug IS NULL OR role_slug<>?)
                ORDER BY id LIMIT 40""",
            (*params, int(since_id or 0), role),
        ).fetchall()
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            p = jloads(row["payload_json"], {})
            kind = row["kind"]
            if kind == "comment":
                text = str(p.get("body", ""))
            elif kind == "mention":
                text = f"@{p.get('target', '')}: {p.get('message', '')}"
            elif kind == "step":
                text = (f"步骤 {p.get('step', '')} → {p.get('status', '')}"
                        + (f"：{p.get('summary')}" if p.get("summary") else ""))
            elif kind == "review":
                text = "评审通过" if p.get("approve") else "评审驳回"
            elif kind == "submit":
                text = "提交：" + str(p.get("summary", ""))
            else:
                continue
            lines.append(f"- [{row['role_slug'] or 'system'}] {text[:400]}")
        if not lines:
            return ""
        return ("## 你离开任务期间的新进展\n" + "\n".join(lines[-25:])
                + f"\n（完整内容用 `anvil inbox check --task {task_id}` 拉取）\n\n")

    def _slot_clear_memory(self, r: _Role) -> None:
        r.context_key = None
        r.last_task_id = None
        r.last_slice_key = None
        r.loaded_spec = None
        r.session_id = None
        r.context_tokens = 0
        r.slot_seen_activity = 0
        r.payload_chars = 0
        r.handoff_pending = ""

    @staticmethod
    def _model_family(model_id: str | None) -> str:
        """去掉 [1m]/[200k]/[128k] 变体后缀，用于判断 init 别名与 served 名称是否同族。"""
        return _MODEL_VARIANT_SUFFIX.sub("", (model_id or "").strip().lower())

    def _model_window_info(self, model_id: str | None) -> tuple[int, bool]:
        """返回 (窗口, 是否来自显式配置/名称推断)；默认值不算 known。"""
        model = (model_id or "").strip()
        mapped = config.MODEL_CONTEXT_WINDOWS.get(model)
        if mapped:
            return int(mapped), True
        low = model.lower()
        if "1m" in low:
            return 1_000_000, True
        if "200k" in low:
            return 200_000, True
        if "128k" in low:
            return 128_000, True
        return int(config.DEFAULT_CONTEXT_WINDOW), False

    def _model_window(self, model_id: str | None) -> int:
        """解析单个模型 id 的上下文窗口（配置映射/名称推断/默认）。"""
        return self._model_window_info(model_id)[0]

    def _active_context_window(self, r: _Role) -> int:
        window = self._model_window(r.model_id)
        if r.adaptive_window:
            window = min(window, int(r.adaptive_window))
        return window

    def _context_window(self, r: _Role) -> int:
        """策略窗口：取「当前模型、角色下限、全局下限、模型池」中的最小值。

        角色可能从 pro 切到 flash；若只按当前模型窗口算阈值，大上下文会超出
        小模型的窗口。这里用保守最小值，保证任何候选模型都放得下。
        """
        windows = [self._active_context_window(r)]
        if r.context_window_floor and int(r.context_window_floor) > 0:
            windows.append(int(r.context_window_floor))
        if config.CONTEXT_WINDOW_FLOOR > 0:
            windows.append(int(config.CONTEXT_WINDOW_FLOOR))
        for model in r.model_pool:
            windows.append(self._model_window(model))
        return max(1, min(windows))

    def _cic_threshold(self, r: _Role) -> int:
        """冷唤醒/CIC 重置阈值：显式配置优先，否则按策略窗口比例。

        Phase 3：`ANVIL_TASK_CONTEXT_BUDGET_TOKENS > 0` 时把单任务预算作为上限，
        避免 1M 窗口下单个任务积累几十万 token 的历史。
        """
        if config.CIC_THRESHOLD_TOKENS > 0:
            base = int(config.CIC_THRESHOLD_TOKENS)
        else:
            base = max(1, int(self._context_window(r) * config.CIC_RESET_RATIO))
        budget = int(getattr(config, "TASK_CONTEXT_BUDGET_TOKENS", 0) or 0)
        return min(base, budget) if budget > 0 else base

    def _hint_threshold(self, r: _Role) -> int:
        return max(1, int(self._context_window(r) * config.CONTEXT_HINT_RATIO))

    def _hard_threshold(self, r: _Role) -> int:
        """单轮内主动换会话的硬阈值。"""
        return _hard_limit_for(self._context_window(r))

    def _context_hint(self, r: _Role, task_id: str | None,
                      slice_key: str | None = None) -> str:
        """给角色的会话决策提示：只在切换任务/切片或上下文占用较高时出现。"""
        tokens = int(r.context_tokens or 0)
        active_window = self._active_context_window(r)
        window = self._context_window(r)
        threshold = self._cic_threshold(r)
        last_context = context_key(r.last_task_id, r.last_slice_key)
        current_context = context_key(task_id, slice_key)
        changed = bool(r.last_task_id and task_id and last_context != current_context)
        high = tokens >= self._hint_threshold(r)
        payload = int(r.payload_chars or 0)
        payload_hint = int(config.PAYLOAD_HINT_CHARS)
        payload_high = payload_hint > 0 and payload >= payload_hint
        if not (changed or high or payload_high):
            return ""

        def fmt(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}m".replace(".0m", "m")
            return f"{n / 1000:.0f}k" if n >= 1000 else str(n)

        lines = ["## 会话上下文（由你决定是否重置）"]
        model = f"（{r.model_id}）" if r.model_id else ""
        if window < active_window:
            lines.append(
                f"- 当前会话已用上下文约 {fmt(tokens)} tokens；"
                f"当前模型窗口约 {fmt(active_window)}{model}；"
                f"按模型池/下限保守预算 {fmt(window)}，Anvil 冷唤醒阈值 {fmt(threshold)} tokens。")
        else:
            lines.append(
                f"- 当前会话已用上下文约 {fmt(tokens)} tokens；"
                f"模型窗口约 {fmt(window)}{model}，Anvil 冷唤醒阈值 {fmt(threshold)} tokens。")
        if r.last_task_id:
            last = r.last_task_id
            if r.last_step_id:
                last += f" / 步骤 {r.last_step_id}"
            lines.append(f"- 上一次处理：{last}")
        if task_id:
            lines.append(f"- 当前任务：{task_id}")
            if slice_key:
                lines.append(f"- 当前切片：{slice_key}")
        if changed:
            lines.append("- 任务已切换；若与本切片不相关，建议先重置会话。")
        if high:
            lines.append("- 上下文占用较高；若新任务与旧上下文关系不大，建议先重置会话。")
            lines.append(
                "- 重置前先用 `anvil memory append \"关键结论/口径/证据路径\"` 写入长期记忆；"
                "Anvil 在换会话前也会自动保存一份任务知识快照。")
        if payload_high:
            lines.append(
                f"- 本会话已累计约 {fmt(payload)} 字符的图片/附件"
                f"（预算 {fmt(int(config.PAYLOAD_HARD_RESET_CHARS))}）；"
                "请勿再读取大图/大文件，优先用片段读取（head/tail/grep/offset+limit）"
                "或引用文件路径；先把关键结论写入 memory/notes，"
                "接近预算时 Anvil 会自动换新会话。")
        cmd = (f"anvil self reset-context --task {task_id} --reason \"...\""
               if task_id else "anvil self reset-context --reason \"...\"")
        lines.append(
            f"- 重置方法：`{cmd}`。它会在新会话中重新投递当前任务/步骤；"
            "不要在有排队消息时调用。")
        return "\n".join(lines) + "\n\n"

    def _on_model_switch(self, r: _Role, old: str, new: str,
                         task_id: str | None = None) -> None:
        """模型切换后的上下文适配检查（运行时自己切模型，Anvil 只能观测和提示）。"""
        tokens = int(r.context_tokens or 0)
        active = self._active_context_window(r)
        safe = self._context_window(r)
        cic = self._cic_threshold(r)
        fit = tokens <= active
        safe_fit = tokens <= safe
        self._log(
            r.role,
            f"[anvil] model switched: {old} -> {new}; context={tokens}, "
            f"active_window={active}, safe_window={safe}, fit={fit}, safe_fit={safe_fit}")
        if not fit:
            self._log(
                r.role,
                "[anvil] WARNING: context exceeds the new model window; "
                "reset/compact before the next turn")
        elif tokens >= cic:
            self._log(
                r.role,
                f"[anvil] context {tokens} >= CIC threshold {cic} for model pool; "
                "next cold wake will use a fresh CIC session")
        try:
            conn = open_conn(self.db_path)
            try:
                ledger.emit_audit(
                    conn, role_slug=r.role,
                    task_id=task_id,
                    action="session.model_switch",
                    payload=jdumps({"from": old, "to": new,
                             "served_model_id": r.served_model_id,
                             "context_tokens": tokens,
                             "active_window": active, "safe_window": safe,
                             "fit": fit, "safe_fit": safe_fit}),
                    now=now_iso())
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 —— 审计失败不影响会话
            pass

    def _record_usage(self, r: _Role, usage: dict,
                      task_id: str | None = None, run_id: str | None = None) -> None:
        try:
            from . import tokens as tokens_service
            conn = open_conn(self.db_path)
            try:
                tokens_service.record(conn, r.role, task_id, run_id, usage)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _begin_context_overflow(self, r: _Role, reason: str,
                                proc: subprocess.Popen | None = None) -> None:
        """上下文超限（主动阈值或 API 报错）：终止本会话，交给回调换 CIC 新会话。

        幂等：同一轮内只处理一次。等待 reader 线程走正常退出路径，由它调用
        `on_context_overflow(role, run_id, reason)`，避免在 reader 线程里重入编排器。
        `proc` 是发现溢出的进程；旧 reader 不得杀掉已经切换后的新进程。
        """
        with r.lock:
            if proc is not None and r.proc is not proc:
                return
            if r.context_overflow:
                return
            owner = proc or r.proc
            r.context_overflow = True
            r.context_overflow_reason = reason
            r.context_overflow_proc = owner
            if reason == "api_error":
                # API 已明确拒绝：说明真实窗口可能小于模型名推断值。按“最后一次成功
                # 请求的 95%”收紧，后续会话在更低水位轮换，避免反复撞同一堵墙。
                measured = int(r.context_tokens or 0)
                if measured > 0:
                    cap = max(64_000, int(measured * 0.95))
                    r.adaptive_window = min(int(r.adaptive_window or cap), cap)
                    self._log(
                        r.role,
                        f"[anvil] adaptive context window → {r.adaptive_window} "
                        f"(last accepted context {measured})")
            # 先把任务决策/进度固化成 notes 快照，再丢掉对话上下文。
            if r.current_task_id:
                conn = open_conn(self.db_path)
                try:
                    self._consolidate_context(conn, r, reason)
                finally:
                    conn.close()
            r.cic_pending = True
            r.suppress_exit = True
            # 强制下一次 _spawn 新建会话：payload 溢出时 token 可能不高，
            # 不能依赖 token 阈值决定是否 --resume（否则旧附件会被原样带回）。
            r.session_id = None
            try:
                r.context_tokens = max(int(r.context_tokens or 0),
                                       self._hard_threshold(r) + 1)
            except Exception:  # noqa: BLE001
                pass
            active_proc = r.proc
            if active_proc is not None and (proc is None or active_proc is proc):
                try:
                    if active_proc.stdin:
                        active_proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    active_proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                # terminate 被忽略时兜底 kill，避免残留进程变成同一角色的第二个实例。
                try:
                    active_proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    try:
                        active_proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            self._log(
                r.role,
                f"[anvil] context/payload overflow ({reason}; "
                f"context≈{r.context_tokens}, payload≈{r.payload_chars} chars) "
                "→ 将用 CIC 新会话重投当前 run")

    def _consolidate_context(self, conn, r: _Role, trigger: str) -> None:
        """上下文清理前固化任务知识；失败只记日志，不阻断换会话。"""
        try:
            from . import memory as memory_service
            snap = memory_service.consolidate_context(
                conn, r.role, r.current_task_id, trigger, r.current_run_id,
                slice_key=slice_key_of(r.context_key))
            if snap:
                self._log(r.role,
                          f"[anvil] knowledge snapshot saved: {snap.get('path')}")
        except Exception:  # noqa: BLE001
            self._log(r.role, "[anvil] knowledge snapshot failed:\n"
                              + traceback.format_exc())

    def _notify_context_overflow(self, role: str, run_id: str, reason: str) -> None:
        try:
            self.on_context_overflow(role, run_id, reason)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 —— 回调失败不能杀死 daemon
            self._log(role, "[anvil] on_context_overflow failed:\n" + traceback.format_exc())

    def _on_turn_result(self, r: _Role, ev: dict, proc=None) -> None:
        # 计划内切换后，旧 reader 仍可能带着已缓冲的 result 走到这里；先用捕获的
        # run/task 归因，再在锁内确认自己仍是当前进程，避免改到新任务的状态。
        with r.lock:
            if proc is not None and r.proc is not proc:
                return
            run_id = r.current_run_id
            task_id = r.current_task_id
        conn = open_conn(self.db_path)
        try:
            r.last_active = time.time()
            # 注意：result.usage 是会话累计消耗（含历史所有 API 调用的 cache read），
            # 不是当前上下文大小。上下文只认 assistant 事件的单次请求 usage；
            # 用 result 覆盖会让 whoami/CIC 阈值全部失真（实测被放大到 1.5M–3M）。
            # 一轮结束仍未 submit → run 进入 suspended（等待下一条消息）
            reminder_row = None
            if run_id:
                self._finish_subagents(conn, r, run_id)
                reminder_row = conn.execute(
                    "SELECT was_alive FROM reminder_wakes WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                from . import runs as runs_service
                if reminder_row is not None:
                    # 知识管理/定时提醒是一次性工作：本轮结束即完成，不再作为
                    # suspended 活跃会话等待下一条消息。CAS 防止覆盖并发 failed。
                    runs_service.set_status(
                        conn, run_id, "completed", ended_at=now_iso(),
                        from_statuses=("running",))
                else:
                    # 分类挂起：等人回复（不自动恢复）与其它空闲/系统原因分开。
                    kind = runs_service.SUSPEND_IDLE
                    if task_id:
                        try:
                            from . import tasks as tasks_service
                            if tasks_service.waiting_for_human(
                                    conn, task_id, r.role,
                                    slice_key_of(r.context_key)):
                                kind = runs_service.SUSPEND_WAITING_HUMAN
                        except Exception:  # noqa: BLE001 —— 分类失败退回 idle
                            pass
                    runs_service.set_status(
                        conn, run_id, "suspended", from_statuses=("running",),
                        suspend_kind=kind)
            with r.lock:
                if proc is not None and r.proc is not proc:
                    return
                # turn 边界先把当前任务槽固化（session/水位/最后活跃时间）。
                self._slot_persist(r, status="idle", conn=conn)
                # 轮次边界是唯一安全的“换会话”时机：上下文已过高水位且没有排队消息时，
                # 释放进程，下一次唤醒由 _spawn 用 CIC 新建会话。否则一条大上下文会被
                # 后续消息继续 --resume，直到单轮内撞上 API 硬上限。
                payload_high = (
                    config.PAYLOAD_HARD_RESET_CHARS > 0
                    and int(r.payload_chars or 0) >= config.PAYLOAD_HARD_RESET_CHARS)
                if (not r.queue and r.proc is not None
                        and (int(r.context_tokens or 0) >= self._cic_threshold(r)
                             or payload_high)):
                    if run_id:
                        rotation = runs_service.note_context_rotation(
                            conn, run_id,
                            max_rotations=config.CONTEXT_ROTATION_MAX_CONSECUTIVE)
                        if rotation == "failed":
                            self._log(
                                r.role,
                                "[anvil] consecutive context rotations exhausted; "
                                "run marked failed")
                    self._consolidate_context(
                        conn, r, "payload-high" if payload_high else "context-high")
                    r.cic_pending = True
                    r.suppress_exit = True
                    key = r.context_key
                    self._log(
                        r.role,
                        f"[anvil] context {r.context_tokens} >= CIC threshold "
                        f"{self._cic_threshold(r)} at turn end → release session")
                    self._kill_locked(r, graceful=True)
                    r.suppress_exit = False
                    # 只清当前任务的槽；其它任务上下文保持可恢复。
                    if key:
                        clear_session(conn, r.role, "context high at turn end",
                                      task_key_value=key)
                    r.context_key = None
                    r.loaded_spec = None
                    r.session_id = None
                    r.current_run_id = None
                    r.current_task_id = None
                    r.current_step_id = None
                    r.current_log_path = None
                    self._db_upsert(conn, r, status="idle")
                else:
                    # 正常结束且未触发轮换：连续计数归零。
                    if run_id:
                        conn.execute(
                            "UPDATE runs SET context_rotation_count=0 WHERE id=? "
                            "AND status NOT IN ('completed','failed','abandoned')",
                            (run_id,))
                        conn.commit()
                    self._flush_next_locked(conn, r)
                self._release_reminder_process(conn, r, run_id, reminder_row)
        finally:
            conn.close()

    def _build_cic(self, conn, role: str, task_id: str | None,
                   run_id: str | None = None,
                   slice_key: str | None = None) -> str:
        """Constructed Initial Context：任务知识 + 可复用知识 + 最近消息/动作。

        三条负规则：不宣布重置、不列未决问题清单、超过 2 小时的内容只给指针。
        task-scoped 内容只允许当前 task_id；role-scoped 可复用知识可跨任务。
        slice 上下文只注入本切片（及 task-wide）的原始消息；其他切片只允许
        以蒸馏后的知识/决策进入。
        """
        from . import knowledge as knowledge_service  # 惰性导入避免循环
        from . import agent_inbox
        from . import knowledge_binding as knowledge_binding_service
        from . import knowledge_repos as knowledge_repos_service
        from . import memory as memory_service
        from . import repo_knowledge as repo_knowledge_service
        from . import task_knowledge as task_knowledge_service
        from . import workspace_knowledge as workspace_knowledge_service

        task = None
        if task_id:
            task = conn.execute(
                "SELECT id,title,status,tags_json,repo,scope_paths_json,workspace_id "
                "FROM tasks WHERE id=?", (task_id,)).fetchone()
        tags = [str(t) for t in jloads(task["tags_json"], [])] if task else []
        repos = knowledge_service.scope_repos(
            conn, task_id, task["repo"] if task else None)
        blocks: list[dict] = []
        try:
            from . import roles as roles_service
            capabilities = roles_service.capability_summary(conn, role)
            if capabilities:
                blocks.append({
                    "priority": 6, "order": 8, "cap": 900, "min": 120,
                    "text": "## 可用能力\n" + capabilities,
                    "name": "capabilities", "layer": "L0", "kind": "capabilities",
                    "scope": "role", "source_ids": [role],
                })
        except Exception:  # noqa: BLE001 —— 能力清单失败不阻断 CIC
            pass

        if task is not None:
            header = (f"## 当前任务\n- {task['title']}（{task_id}）\n"
                      f"- 状态：{task['status']}")
            if tags:
                header += "\n- 标签：" + "、".join(tags)
            if task["repo"]:
                header += f"\n- 仓库：{task['repo']}"
            if repos:
                header += "\n- 知识仓库：" + "、".join(repos)
            if slice_key:
                slice_row = conn.execute(
                    "SELECT ws.title FROM workflow_steps ws "
                    "JOIN workflows w ON w.id=ws.workflow_id "
                    "WHERE w.task_id=? AND ws.slice_key=? LIMIT 1",
                    (task_id, slice_key)).fetchone()
                header += "\n- 当前切片：" + slice_key
                if slice_row and slice_row["title"]:
                    header += f"（{slice_row['title']}）"
                header += "\n- 本切片的对话上下文与其他切片隔离；不要引用其他切片的原始对话。"
            blocks.append({
                "priority": 5, "order": 5, "cap": 500, "min": 120, "text": header,
                "name": "current_task", "layer": "L1", "kind": "task_header",
                "scope": "task", "source_ids": [task_id] if task_id else [],
            })

            # 本切片出发的派遣子任务：回切后让角色知道出去做过什么。
            try:
                dclause = "d.source_task_id=?"
                dparams: list = [task_id]
                if slice_key:
                    dclause += " AND (d.source_slice_key=? OR d.source_slice_key IS NULL)"
                    dparams.append(slice_key)
                drows = conn.execute(
                    "SELECT d.target_task_id, d.target_role, d.status, d.returned_at, "
                    "t.title, t.status AS target_status FROM task_dispatches d "
                    "LEFT JOIN tasks t ON t.id=d.target_task_id "
                    f"WHERE {dclause} ORDER BY d.created_at DESC LIMIT 5",
                    dparams).fetchall()
                if drows:
                    lines = []
                    for d in drows:
                        line = (f"- {d['target_task_id']}「{d['title'] or '已删除任务'}」"
                                f"→ {d['target_role'] or '—'}；{d['status']}")
                        if d["target_status"]:
                            line += f"（任务 {d['target_status']}）"
                        lines.append(line)
                    blocks.append({
                        "priority": 48, "order": 55, "cap": 1200, "min": 150,
                        "text": "## 本切片出发的派遣子任务\n" + "\n".join(lines),
                        "name": "dispatches", "layer": "L1", "kind": "dispatch",
                        "scope": "task", "source_ids": [d["target_task_id"] for d in drows],
                    })
            except Exception:  # noqa: BLE001 —— 派遣信息失败不阻断 CIC
                pass

            channel_knowledge = workspace_knowledge_service.render_for_task(
                conn, dict(task), budget_chars=config.CHANNEL_KNOWLEDGE_CIC_CHARS)
            if channel_knowledge:
                blocks.append({
                    "priority": 45, "order": 35, "cap": config.CHANNEL_KNOWLEDGE_CIC_CHARS,
                    "min": 200, "text": channel_knowledge,
                    "name": "channel_knowledge", "layer": "L1", "kind": "channel_knowledge",
                })
            task_shared = task_knowledge_service.render_for_task(conn, task_id, budget_chars=800)
            if task_shared:
                blocks.append({
                    "priority": 42, "order": 36, "cap": 800, "min": 150, "text": task_shared,
                    "name": "task_knowledge", "layer": "L1", "kind": "task_knowledge",
                    "scope": "task", "source_ids": [task_id] if task_id else [],
                })
            # 主题知识库：任务绑定的共享 repo 已批准卡片（只读，不复制文件）。
            try:
                scope = knowledge_binding_service.retrieval_scope(conn, dict(task))
                repo_ids = []
                for name in scope.get("repos") or []:
                    if not name or name == "shared":
                        continue
                    repo_row = knowledge_repos_service.get(conn, name)
                    if repo_row is not None:
                        repo_ids.append(repo_row["id"])
                topic_text = repo_knowledge_service.render_for_task(
                    conn, dict(task), repo_ids, budget_chars=2000)
                if topic_text:
                    blocks.append({
                        "priority": 35, "order": 50, "cap": 2500, "min": 200,
                        "text": topic_text,
                        "name": "topic_knowledge", "layer": "L2",
                        "kind": "topic_knowledge", "scope": "topic",
                        "source_ids": repo_ids,
                    })
            except Exception:  # noqa: BLE001 —— 主题知识失败不阻断 CIC
                pass

            snap = knowledge_service.latest_task_snapshot(
                conn, role, task_id, slice_key=slice_key)
            if snap:
                blocks.append({
                    "priority": 20, "order": 30, "cap": 4000, "min": 600,
                    "text": "## 最近知识快照（自动整理）\n" + str(snap["content"])[:3900],
                    "name": "task_snapshot", "layer": "L1", "kind": "task_snapshot",
                    "scope": "task", "source_ids": [snap.get("id") or task_id],
                })

            query = " ".join([str(task["title"] or ""), *tags,
                              str(task["repo"] or "")]).strip()
            hits = knowledge_service.search_knowledge(
                conn, role, query, task_id=task_id, scope="auto",
                limit=5, snippet_chars=600, repo_scope="bound_or_shared",
                repos=repos, min_score=config.KNOWLEDGE_MIN_SCORE,
                allow_empty=False) if query else []
            if hits:
                knowledge_service.record_hits(conn, [hit["id"] for hit in hits])
            hit_text = knowledge_service.format_hits(
                hits, max_item_chars=600, max_total_chars=4000)
            if hit_text:
                blocks.append({
                    "priority": 40, "order": 40, "cap": 4000, "min": 400,
                    "text": "## 相关记忆（按任务检索）\n" + hit_text,
                    "name": "related_knowledge", "layer": "L2", "kind": "knowledge",
                    "scope": "role_or_task",
                    "source_ids": [hit.get("id") for hit in hits if hit.get("id")],
                })

            wf = conn.execute(
                "SELECT id FROM workflows WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone()
            if wf is not None:
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM workflow_steps "
                    "WHERE workflow_id=? GROUP BY status", (wf["id"],)).fetchall()
                step_clause = ""
                step_params: list = [wf["id"]]
                if slice_key:
                    # 只展示 task 级步骤与本切片；其他切片的执行细节不得串入。
                    step_clause = (" AND (slice_key=? OR slice_key IS NULL "
                                   "OR slice_key='' OR kind='plan_slices')")
                    step_params.append(slice_key)
                steps = conn.execute(
                    "SELECT step_id, title, role_slug, status, summary, feedback "
                    "FROM workflow_steps WHERE workflow_id=?" + step_clause +
                    " ORDER BY updated_at DESC, step_id DESC LIMIT 30",
                    step_params).fetchall()
                if steps:
                    lines = []
                    if counts:
                        lines.append("- 总览：" + "，".join(
                            f"{c['status']} {c['n']}" for c in counts))
                    for st in reversed(steps):
                        line = (f"- [{st['status']}] {st['step_id']} {st['title']}"
                                f"（{st['role_slug']}）")
                        if st["summary"]:
                            line += f"：{str(st['summary'])[:160]}"
                        if st["feedback"]:
                            line += f"；反馈：{str(st['feedback'])[:160]}"
                        lines.append(line)
                    blocks.append({
                        "priority": 50, "order": 50, "cap": 4000, "min": 500,
                        "text": "## 工作流进度与决策\n" + "\n".join(lines),
                        "name": "workflow", "layer": "L1", "kind": "workflow",
                        "scope": "task", "source_ids": [wf["id"]],
                    })
        else:
            # 无任务（reminder/直聊）：只取 role-scoped 的最近知识，不碰 task scope。
            hits = knowledge_service.search_knowledge(
                conn, role, "", task_id=None, scope="role",
                limit=5, snippet_chars=300, repo_scope="bound_or_shared",
                repos=repos, min_score=0.0, allow_empty=True)
            if hits:
                knowledge_service.record_hits(conn, [hit["id"] for hit in hits])
            hit_text = knowledge_service.format_hits(
                hits, max_item_chars=400, max_total_chars=1800)
            if hit_text:
                blocks.append({
                    "priority": 40, "order": 40, "cap": 1800, "min": 200,
                    "text": "## 相关记忆（角色知识库）\n" + hit_text,
                    "name": "role_knowledge", "layer": "L3", "kind": "knowledge",
                    "scope": "role",
                    "source_ids": [hit.get("id") for hit in hits if hit.get("id")],
                })

        lessons = memory_service.shared_lessons(
            conn, tags, limit=8, repo=(task["repo"] if task else None),
            repos=repos)
        if lessons:
            memory_service.record_lesson_hits(conn, [x["id"] for x in lessons])
            lesson_lines = [f"- {str(x.get('content') or '')[:500]}" for x in lessons]
            blocks.append({
                "priority": 60, "order": 60, "cap": 1500, "min": 200,
                "text": "## 共享经验（已批准）\n" + "\n".join(lesson_lines),
                "name": "shared_lessons", "layer": "L4", "kind": "lesson",
                "scope": "shared", "source_ids": [x["id"] for x in lessons],
            })

        # 耐久准则：小预算、始终注入；完整 MEMORY.md 不再进入上下文。
        principles = memory_service.read_principles(role).strip()
        if principles:
            blocks.append({
                "priority": 8, "order": 10, "cap": 2048, "min": 400,
                "text": "## 角色准则（长期）\n" + principles,
                "name": "role_principles", "layer": "L3", "kind": "principles",
                "scope": "role", "source_ids": [role],
            })

        if task is not None:
            try:
                unread = agent_inbox.unread_count(
                    conn, role, task_id, slice_key=slice_key)
            except Exception:  # noqa: BLE001 —— 游标异常不阻断 CIC
                unread = 0
            if unread:
                blocks.append({
                    "priority": 25, "order": 65, "cap": 800, "min": 200,
                    "text": (
                        "## 未读消息（拉模式）\n"
                        f"- 当前上下文有 {unread} 条未读；冷唤醒只给摘要，"
                        "完整内容按需拉取，避免重复阅读。\n"
                        f"- 查看：`anvil inbox check --task {task_id} --limit 30`\n"
                        f"- 确认：`anvil inbox commit --task {task_id} "
                        "--up-to <最大消息 id>`"),
                    "name": "unread_inbox", "layer": "L1", "kind": "inbox",
                    "scope": "task", "source_ids": [task_id],
                })
            clause, params = self._activity_scope(task_id, slice_key)
            rows = conn.execute(
                "SELECT id, role_slug, kind, payload_json FROM activities "
                f"WHERE {clause} AND kind IN ('comment','mention','step') "
                "ORDER BY id DESC LIMIT 25", params).fetchall()
            lines = []
            for r in rows:
                p = jloads(r["payload_json"], {})
                if r["kind"] == "comment":
                    txt = str(p.get("body", ""))
                elif r["kind"] == "mention":
                    txt = f"@{p.get('target','')}: {p.get('message','')}"
                else:
                    txt = f"步骤 {p.get('step','')} → {p.get('status','')}"
                lines.append(f"- [{r['role_slug'] or 'system'}] {txt[:200]}")
            if lines:
                # 从最新往回取，保证预算截断时保留最近消息。
                picked: list[str] = []
                used = 0
                for line in lines:
                    if used + len(line) + 1 > 4000:
                        break
                    picked.append(line)
                    used += len(line) + 1
                blocks.append({
                    "priority": 30, "order": 70, "cap": 4000, "min": 500,
                    "truncate": "tail",
                    "text": "## 最近 25 条消息\n" + "\n".join(reversed(picked)),
                    "name": "recent_messages", "layer": "L1", "kind": "messages",
                    "scope": "task", "source_ids": [r["id"] for r in rows],
                })
            # audit 没有 slice 维度；切片上下文不注入，避免把其他切片的动作带进来。
            acts = [] if slice_key else conn.execute(
                "SELECT role_slug, action FROM audit WHERE task_id=? ORDER BY id DESC LIMIT 12",
                (task_id,)).fetchall()
            if acts:
                blocks.append({
                    "priority": 80, "order": 80, "cap": 1000, "min": 100,
                    "text": "## 最近动作\n" + "\n".join(
                        f"- {a['role_slug'] or 'system'}: {a['action']}"
                        for a in reversed(acts)),
                    "name": "recent_actions", "layer": "L1", "kind": "actions",
                    "scope": "task",
                })

        idx = knowledge_service.knowledge_index(conn, role, task_id)
        if idx["total_role"] or idx["total_task"]:
            labels = {"vulnerability_pattern": "漏洞模式",
                      "failure_scenario": "故障场景", "module_knowledge": "模块知识",
                      "decision": "决策", "note": "笔记",
                      "personal_lesson": "个人经验"}
            role_counts = " / ".join(
                f"{labels.get(k, k)} {v}" for k, v in sorted(idx["role_counts"].items()) if v)
            task_counts = " / ".join(
                f"{labels.get(k, k)} {v}" for k, v in sorted(idx["task_counts"].items()) if v)
            index_lines = ["## 知识库索引（按需检索）"]
            if role_counts:
                index_lines.append(f"- 角色知识库：{idx['total_role']} 条（{role_counts}）")
            if task_counts:
                index_lines.append(f"- 当前任务：{idx['total_task']} 条（{task_counts}）")
            recent = "、".join(
                f"{x['kind']}:{Path(str(x['path'])).name}"
                for x in idx["recent"][:8])
            if recent:
                index_lines.append(f"- 最近：{recent}")
            index_lines.append(
                '- 检索：`anvil knowledge search "<关键词>" --task $ANVIL_TASK_ID --limit 5`；'
                '详情：`anvil knowledge show <id>`')
            blocks.append({
                "priority": 70, "order": 20, "cap": 1200, "min": 300,
                "text": "\n".join(index_lines),
                "name": "knowledge_index", "layer": "L5", "kind": "index",
                "scope": "role_or_task",
            })

        budget_chars = max(1000, int(config.CIC_COLD_BUDGET_CHARS or 32000))
        text, manifest = knowledge_service.pack_blocks_with_manifest(
            blocks, budget_chars)
        try:
            from . import context_manifest as manifest_service
            manifest_service.record(
                conn, run_id=run_id, role_slug=role, task_id=task_id,
                slice_key=slice_key,
                kind="cic", budget_chars=budget_chars, total_chars=len(text),
                blocks=manifest)
            conn.commit()
        except Exception:  # noqa: BLE001 —— manifest 失败不影响 CIC
            pass
        return text
