"""常驻角色运行时（persistent role daemon）。

每个角色一个长期存活的 CLI 会话进程（claude stream-json 双向流）：
- 唤醒 = 往进程 stdin 投递一条用户消息（同一上下文，不重启进程）；
- 空闲 = 阻塞等待下一条消息（CPU 为 0）；
- 空闲超时（默认 15 分钟）= 关闭 stdin 让进程退出，保留 session_id；
- 下次唤醒用 `--resume <session_id>` 恢复同一上下文（上下文持久在磁盘，不常驻内存）；
- 忙时收到新消息 = 排队，当前轮 `result` 事件后合并投递（coalescing）。

设计变更（owner 2026-10-19）：取代 design.md §1.2「进程不持久」的 one-shot 模型；
one-shot 仍作为不支持流式协议的运行时的回退路径。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..db import open_conn
from ..util import decode_child_output, jdumps, jloads, now_iso

# 不泄漏给子进程的编排器会话变量（与 platform/darwin.py 保持一致）
_SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
_SESSION_ENV_KEYS = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED"}

# 模型别名的上下文变体后缀，例如 deepseek-flash[1m] / foo[200k]。
_MODEL_VARIANT_SUFFIX = re.compile(r"\[(?:1m|200k|128k)\]$", re.IGNORECASE)


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    merged = {
        k: v for k, v in os.environ.items()
        if not k.startswith(_SESSION_ENV_PREFIXES) and k not in _SESSION_ENV_KEYS
    }
    merged.update(extra)
    if os.name == "nt":
        # Windows 默认按系统 ANSI/OEM 代码页输出；强制 Python 子进程使用 UTF-8，
        # 避免角色/tool 输出的中文在 stdout 日志里变成乱码。
        merged.setdefault("PYTHONIOENCODING", "utf-8")
        merged.setdefault("PYTHONUTF8", "1")
        merged.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")
    return merged


def extract_usage(ev: dict) -> dict:
    """从 result 事件提取 usage；兼容 modelUsage（camelCase）聚合。"""
    usage = ev.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    mu = ev.get("modelUsage")
    if isinstance(mu, dict):
        mapping = {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_input_tokens": "cacheReadInputTokens",
            "cache_creation_input_tokens": "cacheCreationInputTokens",
        }
        agg = {}
        for snake, camel in mapping.items():
            agg[snake] = sum(
                int((v or {}).get(camel) or 0) for v in mu.values() if isinstance(v, dict))
        if any(agg.values()):
            return agg
    return {}


def frame_user_message(text: str) -> str:
    """stream-json 输入帧（claude --input-format stream-json）。"""
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    ) + "\n"


def clear_session(conn, role: str, note: str = "session reset") -> None:
    """清空角色常驻会话；下一次唤醒新建会话（不 --resume）。"""
    conn.execute(
        "UPDATE role_sessions SET session_id='', status='dead', pid=NULL, "
        "last_context_tokens=NULL, last_error=?, updated_at=? WHERE role_slug=?",
        (note, now_iso(), role),
    )
    conn.commit()


@dataclass
class _Role:
    role: str
    proc: subprocess.Popen | None = None
    session_id: str | None = None
    token: str | None = None
    model_id: str | None = None
    served_model_id: str | None = None
    model_pool: list[str] = field(default_factory=list)
    context_window_floor: int | None = None
    busy: bool = False
    busy_since: float | None = None
    last_active: float = 0.0
    queue: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    current_run_id: str | None = None
    current_task_id: str | None = None
    last_task_id: str | None = None
    last_step_id: str | None = None
    context_tokens: int = 0
    cic_pending: bool = False
    suppress_exit: bool = False
    current_log_path: str | None = None
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    stop: bool = False


class DaemonManager:
    """角色常驻进程管理器（单进程内全局单例；由 create_app 初始化）。"""

    def __init__(self, db_path: str, enabled: bool = True,
                 idle_release_minutes: float | None = None) -> None:
        self.db_path = db_path
        self.enabled = enabled
        self.idle_release_minutes = (
            idle_release_minutes if idle_release_minutes is not None
            else config.ROLE_IDLE_RELEASE_MINUTES
        )
        self.on_exit = None  # 回调(role, exit_code)：由 create_app 注入，泵 process_exit
        self._roles: dict[str, _Role] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._shutting_down = False
        self._idle_thread: threading.Thread | None = None
        if enabled:
            self._heal_db()
            self._idle_thread = threading.Thread(
                target=self._idle_loop, daemon=True, name="anvil-role-idle")

    # ---- 对外 API --------------------------------------------------------

    def ensure_and_deliver(self, conn, role: str, *, executable: str, args_new: list[str],
                           args_resume: list[str], ctx: dict, cwd: str, env: dict,
                           prompt: str, run_id: str | None = None,
                           task_id: str | None = None,
                           model_pool: list[str] | None = None,
                           context_window_floor: int | None = None) -> dict:
        """确保角色进程存活（必要时 --resume/新建）并投递一条消息。"""
        r = self._role(role)
        with r.lock:
            if model_pool is not None:
                r.model_pool = [str(m).strip() for m in model_pool if str(m).strip()]
            if context_window_floor is not None:
                try:
                    floor = int(context_window_floor)
                except (TypeError, ValueError):
                    floor = 0
                r.context_window_floor = floor if floor > 0 else None
            if r.proc is None or r.proc.poll() is not None:
                self._spawn(conn, r, executable, args_new, args_resume, ctx, cwd, env)
            self._deliver_locked(conn, r, prompt, run_id, task_id)
            return {"session_id": r.session_id, "pid": r.proc.pid, "alive": True}

    def deliver(self, conn, role: str, prompt: str, run_id: str | None = None,
                task_id: str | None = None) -> bool:
        """向已存在的常驻进程投递消息（无进程返回 False，由调用方走 spawn 路径）。"""
        r = self._roles.get(role)
        if r is None or r.proc is None or r.proc.poll() is not None:
            return False
        with r.lock:
            self._deliver_locked(conn, r, prompt, run_id, task_id)
            return True

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
                "hint_tokens": int(window * config.CONTEXT_HINT_RATIO),
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
            "queue_len": len(r.queue),
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
            "hint_tokens": self._hint_threshold(r),
        }

    def cancel_task(self, role: str, task_id: str) -> bool:
        """任务删除：停止该角色处理此任务的常驻进程。

        - 正忙于此任务：杀进程并清会话；
        - 正忙其它任务：不动；
        - 空闲且队列无其它任务消息：杀进程并清会话；
        - 丢弃队列中属于此任务的消息。
        返回是否终止了进程。
        """
        r = self._roles.get(role)
        if r is None:
            return False
        kill = False
        with r.lock:
            r.queue = [item for item in r.queue if item[2] != task_id]
            if r.busy:
                if r.current_task_id != task_id:
                    return False
            elif r.last_task_id != task_id or r.queue:
                return False
            kill = True
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)
            r.session_id = None
            r.context_tokens = 0
            r.cic_pending = False
            r.busy = False
            r.busy_since = None
            r.current_run_id = None
            r.current_task_id = None
            r.current_log_path = None
        if kill:
            conn = open_conn(self.db_path)
            try:
                clear_session(conn, role, "task deleted")
            finally:
                conn.close()
        return kill

    def kill(self, role: str) -> None:
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            # 主动终止不算 agent 失败，不触发 on_exit / 自动清会话。
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)

    def restart_process(self, role: str) -> None:
        """终止常驻进程但保留 session_id；下一次唤醒 --resume 同一上下文。"""
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            r.suppress_exit = True
            r.queue.clear()
            self._kill_locked(r, graceful=True)
            r.busy = False
            r.busy_since = None
            r.current_run_id = None
            r.current_task_id = None
            r.current_log_path = None

    def reset_session(self, role: str, cic: bool = False) -> None:
        """终止常驻进程并清空 session_id；下一次唤醒新建上下文。

        cic=True 时，下一次唤醒先注入 CIC（MEMORY 头 + 最近消息/动作），适合
        角色自主决定“换新上下文，但保留必要交接”的场景。
        """
        r = self._roles.get(role)
        if r is not None:
            with r.lock:
                r.suppress_exit = True
                r.queue.clear()
                self._kill_locked(r, graceful=True)
                r.session_id = None
                r.context_tokens = 0
                r.cic_pending = cic
                r.busy = False
                r.busy_since = None
                r.current_run_id = None
                r.current_task_id = None
                r.current_log_path = None
        conn = open_conn(self.db_path)
        try:
            clear_session(conn, role, "session reset")
        finally:
            conn.close()

    def shutdown(self) -> None:
        # 停机路径：不触发 on_exit（否则会把 run 标 failed/abandoned，与 suspend 语义冲突）
        self._shutting_down = True
        self._stop.set()
        for r in list(self._roles.values()):
            with r.lock:
                self._kill_locked(r, graceful=True)

    # ---- 内部 ------------------------------------------------------------

    def _role(self, role: str) -> _Role:
        with self._guard:
            return self._roles.setdefault(role, _Role(role=role))

    def _spawn(self, conn, r: _Role, executable: str, args_new: list[str],
               args_resume: list[str], ctx: dict, cwd: str, env: dict) -> None:
        from ..graph.render import render_template

        row = conn.execute(
            "SELECT session_id, token, last_context_tokens, model_id "
            "FROM role_sessions WHERE role_slug=?",
            (r.role,)).fetchone()
        if row and row["model_id"]:
            r.model_id = row["model_id"]
        resume = bool(row and row["session_id"])
        if resume and row["last_context_tokens"] and \
                int(row["last_context_tokens"]) >= self._cic_threshold(r):
            # 会话过大：不 resume，按 CIC 构造新会话首输入（见 _build_cic）
            resume = False
            r.cic_pending = True
        sid = (row["session_id"] if resume else str(uuid.uuid4()))
        token = (row["token"] if row else None) or self._issue_token(conn, r.role)

        tpl = args_resume if resume else args_new
        full_ctx = dict(ctx, session_id=sid, session_token=token)
        args = [render_template(str(a), full_ctx) for a in tpl]
        merged_env = _clean_env(dict(env, ANVIL_TOKEN=token, ANVIL_AGENT_SLUG=r.role))
        # ANVIL_TASK_ID 保留 spawn 时的任务 id（跨任务时以消息前缀为准），
        # 避免 agent 执行 `anvil inbox check` 等命令时 task_id 为空。

        # 二进制管道：由 _reader_loop 统一按 UTF-8/本地编码回退解码，
        # 避免 Windows text 模式按 GBK 解码或把 \n 改写成 \r\n。
        kwargs: dict = dict(
            cwd=cwd, env=merged_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            proc = subprocess.Popen([executable, *args], start_new_session=True, **kwargs)
        except TypeError:  # Windows 不支持 start_new_session
            proc = subprocess.Popen([executable, *args], **kwargs)
        r.proc = proc
        self._log(r.role, f"[anvil] spawn {executable} {' '.join(str(a) for a in args)}")
        r.session_id = sid
        r.token = token
        r.busy = False
        r.last_active = time.time()
        r.stop = False
        r.suppress_exit = False
        self._db_upsert(conn, r, status="starting")
        r.reader = threading.Thread(
            target=self._reader_loop, args=(r,), daemon=True, name=f"anvil-role-{r.role}")
        r.reader.start()
        if self._idle_thread is not None and not self._idle_thread.is_alive():
            self._idle_thread.start()

    def _issue_token(self, conn, role: str) -> str:
        token = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=config.ROLE_SESSION_TOKEN_DAYS)).isoformat()
        conn.execute("DELETE FROM sessions WHERE role_slug=? AND task_id IS NULL", (role,))
        conn.execute(
            "INSERT INTO sessions (token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)", (token, role, None, None, now_iso(), expires))
        conn.commit()
        return token

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
        return self._model_window(r.model_id)

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
        """冷唤醒/CIC 重置阈值：显式配置优先，否则按策略窗口比例。"""
        if config.CIC_THRESHOLD_TOKENS > 0:
            return int(config.CIC_THRESHOLD_TOKENS)
        return max(1, int(self._context_window(r) * config.CIC_RESET_RATIO))

    def _hint_threshold(self, r: _Role) -> int:
        return max(1, int(self._context_window(r) * config.CONTEXT_HINT_RATIO))

    def _context_hint(self, r: _Role, task_id: str | None) -> str:
        """给角色的会话决策提示：只在切换任务或上下文占用较高时出现。"""
        tokens = int(r.context_tokens or 0)
        active_window = self._active_context_window(r)
        window = self._context_window(r)
        threshold = self._cic_threshold(r)
        changed = bool(r.last_task_id and task_id and r.last_task_id != task_id)
        high = tokens >= self._hint_threshold(r)
        if not (changed or high):
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
        if changed:
            lines.append("- 任务已切换；若与本任务不相关，建议先重置会话。")
        if high:
            lines.append("- 上下文占用较高；若新任务与旧上下文关系不大，建议先重置会话。")
        cmd = (f"anvil self reset-context --task {task_id} --reason \"...\""
               if task_id else "anvil self reset-context --reason \"...\"")
        lines.append(
            f"- 重置方法：`{cmd}`。它会在新会话中重新投递当前任务/步骤；"
            "不要在有排队消息时调用。")
        return "\n".join(lines) + "\n\n"

    def _deliver_locked(self, conn, r: _Role, prompt: str,
                        run_id: str | None, task_id: str | None) -> None:
        prefix = f"当前任务 ID：{task_id}\n（环境变量 ANVIL_TASK_ID 可能已过期，请以此为准）\n\n" if task_id else ""
        if r.cic_pending:
            prefix = self._build_cic(conn, r.role, task_id) + "\n\n" + prefix
            r.cic_pending = False
        else:
            prefix = self._context_hint(r, task_id) + prefix
        text = prefix + prompt
        r.queue.append((text, run_id, task_id))
        if not r.busy:
            self._flush_next_locked(conn, r)

    def _flush_next_locked(self, conn, r: _Role) -> None:
        if not r.queue:
            r.busy = False
            r.busy_since = None
            r.current_run_id = None
            r.current_task_id = None
            r.current_log_path = None
            self._db_upsert(conn, r, status="idle")
            return
        text, run_id, task_id = r.queue.pop(0)
        r.last_task_id = task_id
        r.busy = True
        r.busy_since = time.time()
        r.current_run_id = run_id
        r.current_task_id = task_id
        row = conn.execute("SELECT log_path, step_id FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else None
        r.current_log_path = row["log_path"] if row else None
        r.last_step_id = row["step_id"] if row else None
        self._append_run_log(r, f"\n===== USER（上下文/提示词） @ {now_iso()} =====\n{text}\n")
        self._db_upsert(conn, r, status="busy")
        try:
            if r.proc and r.proc.stdin:
                r.proc.stdin.write(frame_user_message(text).encode("utf-8"))
                r.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._log(r.role, f"[anvil] stdin write failed: {exc}")
            r.last_active = time.time()
            self._db_upsert(conn, r, status="dead", error=str(exc))

    def _reader_loop(self, r: _Role) -> None:
        proc = r.proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = decode_child_output(raw).strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    self._log(r.role, line)  # claude 的警告行（如 unrecognized_model）
                    if "Prompt is too long" in line:
                        self._reset_for_cic(r)
                        break
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    self._log(r.role, line)
                    continue
                self._append_run_log(r, self._format_event(ev))
                etype = ev.get("type")
                if etype == "system" and ev.get("session_id"):
                    r.session_id = ev["session_id"]
                    model = ev.get("model") or ev.get("model_id")
                    if model:
                        r.model_id = str(model)
                elif etype == "result":
                    if "Prompt is too long" in line:
                        self._reset_for_cic(r)
                        break
                    self._on_turn_result(r, ev)
                elif etype == "assistant":
                    msg = ev.get("message") or {}
                    model = msg.get("model") or ev.get("model")
                    old_model = r.model_id
                    if model:
                        served = str(model)
                        r.served_model_id = served
                        if not old_model:
                            r.model_id = served
                        elif self._model_family(old_model) == self._model_family(served):
                            # init 别名可能更具体（deepseek-flash[1m] vs served deepseek-flash）；
                            # 同族时优先保留有明确窗口的变体，二者都明确则取较小窗口。
                            old_w, old_known = self._model_window_info(old_model)
                            new_w, new_known = self._model_window_info(served)
                            if new_known and (not old_known or new_w < old_w):
                                r.model_id = served
                        else:
                            r.model_id = served
                    self._log(r.role, self._assistant_text(ev))
                    usage = msg.get("usage") or {}
                    if usage:
                        try:
                            ctx = sum(int(usage.get(k) or 0) for k in (
                                "input_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens"))
                            if ctx:
                                r.context_tokens = ctx
                            self._record_usage(r, usage)
                        except (TypeError, ValueError):
                            pass
                    if old_model and r.model_id and r.model_id != old_model:
                        self._on_model_switch(r, old_model, r.model_id)
        except Exception:  # noqa: BLE001 —— 解码/管道异常不能静默杀死 reader
            self._log(r.role, "[anvil] reader crashed:\n" + traceback.format_exc())
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        # 进程退出。stdout EOF 时 poll() 可能还是 None，wait 一下拿真实退出码，
        # 否则非零退出会被误判成正常退出，失败后仍会 --resume 旧上下文。
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                code = None
        # 失败后默认丢弃上下文：非零退出时下一次唤醒新建 session，
        # 避免坏 context 被 --resume 反复复现（可用 ANVIL_RESET_SESSION_ON_FAILURE=0 关闭）。
        auto_reset = (
            code not in (None, 0)
            and not r.suppress_exit
            and not self._shutting_down
            and config.RESET_SESSION_ON_FAILURE
        )
        with r.lock:
            if r.proc is proc:
                r.proc = None
                r.busy = False
                r.busy_since = None
                if auto_reset:
                    r.session_id = None
                    r.context_tokens = 0
                    r.cic_pending = False
        conn = open_conn(self.db_path)
        try:
            note = f"auto reset after exit code {code}" if auto_reset else None
            self._db_upsert(conn, r, status="dead", pid=None, error=note)
            if auto_reset:
                clear_session(conn, r.role, note or "auto reset after failure")
        finally:
            conn.close()
        if self.on_exit and not self._shutting_down and not r.suppress_exit:
            try:
                self.on_exit(r.role, 0 if code is None else code)
            except Exception:  # noqa: BLE001
                pass

    def _append_run_log(self, r: _Role, text: str | None) -> None:
        if not text or not r.current_log_path:
            return
        try:
            with r.log_lock:
                with open(r.current_log_path, "a", encoding="utf-8") as f:
                    f.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    @staticmethod
    def _format_event(ev: dict) -> str | None:
        """把 stream-json 事件格式化为可读的「上下文/模型/工具」记录。"""
        t = ev.get("type")
        if t == "system":
            if ev.get("subtype") == "init":
                return (f"[system] init session={ev.get('session_id')} "
                        f"model={ev.get('model') or ev.get('model_id') or '-'}")
            return None
        if t in ("assistant", "user"):
            msg = ev.get("message") or {}
            blocks = msg.get("content")
            if not isinstance(blocks, list):
                return None
            out: list[str] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    out.append(f"[{t}] {b.get('text', '')}")
                elif bt == "tool_use":
                    out.append(f"[tool_use] {b.get('name')} {jdumps(b.get('input') or {})}")
                elif bt == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            x.get("text", "") for x in content if isinstance(x, dict))
                    out.append(f"[tool_result] {content}")
            return "\n".join(out) if out else None
        if t == "result":
            usage = ev.get("usage") or {}
            out = [f"[result] usage={jdumps(usage)} cost={ev.get('total_cost_usd')}"]
            if ev.get("result") is not None:
                out.append(str(ev.get("result")))
            return "\n".join(out)
        return None

    def _on_model_switch(self, r: _Role, old: str, new: str) -> None:
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
                conn.execute(
                    "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (r.role, r.current_task_id, "session.model_switch",
                     jdumps({"from": old, "to": new,
                             "served_model_id": r.served_model_id,
                             "context_tokens": tokens,
                             "active_window": active, "safe_window": safe,
                             "fit": fit, "safe_fit": safe_fit}), now_iso()),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 —— 审计失败不影响会话
            pass

    def _record_usage(self, r: _Role, usage: dict) -> None:
        try:
            from . import tokens as tokens_service
            conn = open_conn(self.db_path)
            try:
                tokens_service.record(conn, r.role, r.current_task_id, r.current_run_id, usage)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _reset_for_cic(self, r: _Role) -> None:
        """Prompt 过长：本会话已不可用，终止进程并在下次唤醒用 CIC 新会话。"""
        r.cic_pending = True
        r.suppress_exit = True
        try:
            r.context_tokens = max(int(r.context_tokens or 0),
                                   self._cic_threshold(r) + 1)
        except Exception:  # noqa: BLE001
            pass
        proc = r.proc
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._log(r.role, "[anvil] context too long → 将在下次唤醒用 CIC 新会话")

    def _on_turn_result(self, r: _Role, ev: dict) -> None:
        conn = open_conn(self.db_path)
        try:
            r.last_active = time.time()
            usage = extract_usage(ev)
            try:
                tokens = sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if tokens:
                    r.context_tokens = tokens

            except (TypeError, ValueError):
                pass
            cost = ev.get("total_cost_usd")
            if cost:
                try:
                    from . import governance
                    governance.report_cost(conn, r.current_task_id, r.role, float(cost), "reported")
                except Exception:  # noqa: BLE001
                    pass
            # 一轮结束仍未 submit → run 进入 suspended（等待下一条消息）
            if r.current_run_id:
                row = conn.execute("SELECT status FROM runs WHERE id=?", (r.current_run_id,)).fetchone()
                if row and row["status"] == "running":
                    from . import runs as runs_service
                    runs_service.set_status(conn, r.current_run_id, "suspended")
            with r.lock:
                self._flush_next_locked(conn, r)
        finally:
            conn.close()

    def _idle_loop(self) -> None:
        while not self._stop.wait(30):
            now = time.time()
            for r in list(self._roles.values()):
                if r.proc is None or r.busy:
                    continue
                idle_minutes = (now - (r.last_active or now)) / 60.0
                if idle_minutes >= self.idle_release_minutes:
                    with r.lock:
                        self._kill_locked(r, graceful=True)

    def _kill_locked(self, r: _Role, graceful: bool = True) -> None:
        proc = r.proc
        if proc is None:
            return
        try:
            if graceful and proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        r.proc = None
        r.busy = False
        r.busy_since = None
        conn = open_conn(self.db_path)
        try:
            self._db_upsert(conn, r, status="dead", pid=None)
        finally:
            conn.close()

    def _build_cic(self, conn, role: str, task_id: str | None) -> str:
        """Constructed Initial Context（简化 3 块）：MEMORY 头 / 最近消息 / 最近动作。

        三条负规则：不宣布重置、不列未决问题清单、超过 2 小时的内容只给指针。
        """
        blocks: list[str] = []
        mem_path = Path(config.AGENTS_DIR) / role / "MEMORY.md"
        try:
            mem = mem_path.read_text(encoding="utf-8")[:4000].strip()
        except OSError:
            mem = ""
        if mem:
            blocks.append("## 记忆（MEMORY.md 头部，无需重读全文）\n" + mem)
        if task_id:
            rows = conn.execute(
                "SELECT id, role_slug, kind, payload_json FROM activities "
                "WHERE task_id=? AND kind IN ('comment','mention','step') "
                "ORDER BY id DESC LIMIT 25", (task_id,)
            ).fetchall()
            lines = []
            for r in reversed(rows):
                p = jloads(r["payload_json"], {})
                if r["kind"] == "comment":
                    txt = str(p.get("body", ""))
                elif r["kind"] == "mention":
                    txt = f"@{p.get('target','')}: {p.get('message','')}"
                else:
                    txt = f"步骤 {p.get('step','')} → {p.get('status','')}"
                lines.append(f"- [{r['role_slug'] or 'system'}] {txt[:400]}")
            if lines:
                blocks.append("## 最近 25 条消息\n" + "\n".join(lines))
            acts = conn.execute(
                "SELECT role_slug, action FROM audit WHERE task_id=? ORDER BY id DESC LIMIT 12",
                (task_id,)
            ).fetchall()
            if acts:
                blocks.append("## 最近动作\n" + "\n".join(
                    f"- {a['role_slug'] or 'system'}: {a['action']}" for a in reversed(acts)))
        return "\n\n".join(blocks)

    def _db_upsert(self, conn, r: _Role, status: str, pid: int | None = -1,
                   error: str | None = None) -> None:
        if pid == -1:
            pid = r.proc.pid if r.proc else None
        conn.execute(
            "INSERT INTO role_sessions (role_slug, session_id, token, model_id, pid, status, busy_since, "
            "last_active_at, last_context_tokens, last_error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_slug) DO UPDATE SET session_id=excluded.session_id, token=excluded.token, "
            "model_id=COALESCE(excluded.model_id, role_sessions.model_id), "
            "pid=excluded.pid, status=excluded.status, busy_since=excluded.busy_since, "
            "last_active_at=excluded.last_active_at, "
            "last_context_tokens=COALESCE(excluded.last_context_tokens, role_sessions.last_context_tokens), "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (r.role, r.session_id or "", r.token, r.model_id, pid, status,
             datetime.fromtimestamp(r.busy_since, tz=timezone.utc).isoformat() if r.busy_since else None,
             datetime.fromtimestamp(r.last_active, tz=timezone.utc).isoformat() if r.last_active else None,
             r.context_tokens or None, error, now_iso()),
        )
        conn.commit()

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
            conn.execute("UPDATE role_sessions SET status='dead', pid=NULL, updated_at=? "
                         "WHERE status IN ('starting','idle','busy')", (now_iso(),))
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

    @staticmethod
    def _assistant_text(ev: dict) -> str:
        try:
            blocks = (ev.get("message") or {}).get("content") or []
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        except Exception:  # noqa: BLE001
            return ""


# ---- 全局单例（create_app 初始化；测试不启用） -------------------------------

_manager: DaemonManager | None = None


def init_manager(db_path: str, enabled: bool) -> DaemonManager:
    global _manager
    _manager = DaemonManager(db_path, enabled=enabled)
    return _manager


def get_manager() -> DaemonManager | None:
    return _manager
