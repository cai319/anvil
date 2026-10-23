"""常驻角色运行时（persistent role daemon）。

每个角色最多一个长期存活的 CLI 进程（claude stream-json 双向流），但**上下文按
任务分槽**（role_task_sessions）：
- 唤醒 = 往进程 stdin 投递一条用户消息；同一任务内复用进程/会话；
- 下一条消息属于另一个任务时，在 turn 边界保存当前槽 → 杀进程 → 恢复目标槽：
  有 session 且 cwd 一致则 `--resume`，否则新建会话 + 该任务的 CIC；
- 空闲 = 阻塞等待下一条消息（CPU 为 0）；
- 空闲超时（默认 15 分钟）= 关闭 stdin 让进程退出，保留**所有任务槽**的 session_id；
- 忙时收到新消息 = 按 task 入队，当前轮 `result` 事件后按 FIFO 处理（不抢占）；
- 任务级 reset/溢出/崩溃只清当前任务槽，其它任务上下文保持可恢复；
- reminder/直聊等无任务工作使用 `__global__` 槽。

设计变更（owner 2026-10-19）：取代 design.md §1.2「进程不持久」的 one-shot 模型；
one-shot 仍作为不支持流式协议的运行时的回退路径。
多任务上下文槽设计（owner 2026-10-23）见
`docs/superpowers/specs/2026-10-23-role-task-contexts-design.md`。
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

# 不泄漏给子进程的编排器会话变量（与 platform/darwin.py 保持一致）。
# 注意：不能按 `CLAUDE_CODE_` 前缀整片剥离——该命名空间里还有自动压缩窗口、
# effort、子代理模型等配置项；剥离后角色进程会比交互会话更早撞到上下文上限。
_SESSION_ENV_KEYS = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_ENTRYPOINT",
}

# 模型别名的上下文变体后缀，例如 deepseek-flash[1m] / foo[200k]。
_MODEL_VARIANT_SUFFIX = re.compile(r"\[(?:1m|200k|128k)\]$", re.IGNORECASE)


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    merged = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV_KEYS}
    merged.update(extra)
    if os.name == "nt":
        # Windows 默认按系统 ANSI/OEM 代码页输出；强制 Python 子进程使用 UTF-8，
        # 避免角色/tool 输出的中文在 stdout 日志里变成乱码。
        merged.setdefault("PYTHONIOENCODING", "utf-8")
        merged.setdefault("PYTHONUTF8", "1")
        merged.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")
    return merged


def _hard_limit_for(window: int) -> int:
    """给定窗口的硬阈值；0 比例表示关闭（返回窗口+1）。"""
    ratio = float(config.CONTEXT_HARD_RESET_RATIO)
    if ratio <= 0:
        return window + 1
    reserve = max(0, int(config.CONTEXT_OUTPUT_RESERVE_TOKENS))
    # API 上限按 input+max_tokens 计：既要留出响应额度，也要留出比例余量。
    return max(1, min(int(window * min(ratio, 1.0)), max(1, window - reserve)))


# 只匹配明确的超限措辞；不能用「context + window」这类宽泛组合，否则
# Claude Code 的 unrecognized_model 提示（"the context window it assumes"）
# 或角色自己讨论该错误的输出都会误触发换会话。
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context limit reached",
    "context window exceeded",
    "exceeds the context window",
    "reached its context window limit",
    "context_length_exceeded",
    "input length and max_tokens exceed",
)


def _is_context_overflow(text: str) -> bool:
    """识别各 CLI/API 形态的上下文超限错误（大小写不敏感）。"""
    low = (text or "").lower()
    return any(marker in low for marker in _CONTEXT_OVERFLOW_MARKERS)


# 请求体超限（图片/附件累计）与 token 超限是两类错误；前者换新会话即可清除附件。
_PAYLOAD_OVERFLOW_MARKERS = (
    "request too large",
    "max 32mb",
    "accumulated images and attachments",
    "pushed the request over the limit",
    "remove older images",
    "payload too large",
    "request_too_large",
)


def _is_payload_overflow(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _PAYLOAD_OVERFLOW_MARKERS)


def _attachment_chars(ev: dict) -> int:
    """估算事件中图片/附件的 base64 字符数（用于请求体大小保护）。"""
    def block_chars(block) -> int:
        if not isinstance(block, dict):
            return 0
        kind = block.get("type")
        if kind == "image":
            return len(str((block.get("source") or {}).get("data") or ""))
        if kind == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                return sum(block_chars(x) for x in content)
            return 0
        if kind in ("attachment", "file"):
            return len(str(block.get("data") or block.get("content") or ""))
        return 0

    content = (ev.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    return sum(block_chars(b) for b in content)


_USAGE_CAMEL = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
}


def _aggregate_model_usage(models: dict) -> dict:
    agg = {}
    for snake, camel in _USAGE_CAMEL.items():
        agg[snake] = sum(
            int((v or {}).get(camel) or 0)
            for v in models.values() if isinstance(v, dict))
    return agg


def extract_usage(ev: dict) -> dict:
    """从 result 事件提取 usage；兼容 modelUsage（camelCase）聚合。"""
    usage = ev.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    mu = ev.get("modelUsage")
    if isinstance(mu, dict):
        agg = _aggregate_model_usage(mu)
        if any(agg.values()):
            return agg
    return {}


def extract_turn_usage(ev: dict) -> dict:
    """取「本轮」token 用量。

    Claude Code 的 assistant 事件 usage.output_tokens 恒为 0，且同一轮多个
    assistant 事件会重复携带完整 input；只有 result 事件带真实输出量。
    优先用 model_usage_since_last_result（明确标注为 since last result），
    再退回 result.usage / modelUsage。
    """
    since = ev.get("model_usage_since_last_result")
    if isinstance(since, dict):
        agg = _aggregate_model_usage(since)
        if any(agg.values()):
            return agg
    usage = ev.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    return extract_usage(ev)


def frame_user_message(text: str) -> str:
    """stream-json 输入帧（claude --input-format stream-json）。"""
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    ) + "\n"


GLOBAL_TASK_KEY = "__global__"
_ALL_SLOTS = object()


def task_key(task_id: str | None) -> str:
    """任务上下文槽 key：角色级工作（reminder/直聊）使用 __global__。"""
    return str(task_id) if task_id else GLOBAL_TASK_KEY


def task_id_of(key: str | None) -> str | None:
    return None if key in (None, GLOBAL_TASK_KEY) else key


def clear_session(conn, role: str, note: str = "session reset",
                  task_key_value=_ALL_SLOTS) -> None:
    """清空角色会话槽；下一次唤醒新建会话（不 --resume）。

    task_key_value 默认清空该角色的**所有任务槽**；传入具体 key 时只清一个任务。
    role_sessions 是当前活跃进程的镜像，只有清到当前槽（或全部）时才清空镜像。
    """
    if task_key_value is _ALL_SLOTS:
        conn.execute("DELETE FROM role_task_sessions WHERE role_slug=?", (role,))
        conn.execute(
            "UPDATE role_sessions SET session_id='', status='dead', pid=NULL, "
            "last_context_tokens=NULL, current_task_id=NULL, current_run_id=NULL, "
            "current_step_id=NULL, last_error=?, updated_at=? WHERE role_slug=?",
            (note, now_iso(), role),
        )
    else:
        key = str(task_key_value)
        conn.execute(
            "DELETE FROM role_task_sessions WHERE role_slug=? AND task_key=?",
            (role, key),
        )
        row = conn.execute(
            "SELECT current_task_id FROM role_sessions WHERE role_slug=?", (role,)
        ).fetchone()
        active_key = task_key(row["current_task_id"]) if row else None
        if active_key == key:
            conn.execute(
                "UPDATE role_sessions SET session_id='', last_context_tokens=NULL, "
                "current_task_id=NULL, current_run_id=NULL, current_step_id=NULL, "
                "last_error=?, updated_at=? WHERE role_slug=?",
                (note, now_iso(), role),
            )
        else:
            conn.execute(
                "UPDATE role_sessions SET last_error=?, updated_at=? WHERE role_slug=?",
                (note, now_iso(), role),
            )
    conn.commit()


@dataclass
class _SpawnSpec:
    """拉起/恢复一个角色进程所需的参数；随队列消息保存，跨任务切换时使用。"""
    executable: str
    args_new: list[str]
    args_resume: list[str]
    ctx: dict
    cwd: str
    env: dict
    model_pool: list[str] = field(default_factory=list)
    context_window_floor: int | None = None
    runtime_id: str | None = None
    workspace_path: str | None = None


@dataclass
class _Queued:
    """一条待投递消息；task_id 决定它属于哪个上下文槽。"""
    text: str
    run_id: str | None
    task_id: str | None
    spec: _SpawnSpec


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
    queue: list[_Queued] = field(default_factory=list)
    current_run_id: str | None = None
    current_task_id: str | None = None
    current_step_id: str | None = None
    last_task_id: str | None = None
    last_step_id: str | None = None
    # 当前进程加载的上下文槽（task_key 或 __global__）；None = 尚未加载。
    context_key: str | None = None
    loaded_spec: _SpawnSpec | None = None
    # 正在执行计划内上下文切换（杀旧进程 → 拉新进程）；调度器/看门狗据此跳过。
    switching: bool = False
    # 该槽上次投递时已见过的任务活动 id，用于恢复时生成增量交接。
    slot_seen_activity: int = 0
    handoff_pending: str = ""
    context_tokens: int = 0
    # 当前会话累计的图片/附件 base64 字符数（约等于请求体大小）。
    payload_chars: int = 0
    # API 报错后自适应收紧的窗口（只调小不调大）；用于修正模型名推断不准的情况。
    adaptive_window: int | None = None
    cic_pending: bool = False
    context_overflow: bool = False
    context_overflow_reason: str | None = None
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
        # 回调(role, run_id, reason)：上下文溢出被主动/被动终止后，由 create_app
        # 负责清 run 的会话标记并用 CIC 新会话重投；daemon 不直接依赖编排器。
        self.on_context_overflow = None
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
                           context_window_floor: int | None = None,
                           runtime_id: str | None = None,
                           workspace_path: str | None = None) -> dict:
        """确保角色进程存活（必要时 --resume/新建）并投递一条消息。

        消息按 task_id 进入角色队列；如果角色正忙（或前面排着别的任务），
        返回 `queued=True`，由调用方决定 run 的展示状态。真正投递时才决定
        是否切换上下文槽。
        """
        spec = _SpawnSpec(
            executable=executable, args_new=list(args_new), args_resume=list(args_resume),
            ctx=dict(ctx or {}), cwd=str(cwd), env=dict(env or {}),
            model_pool=[str(m).strip() for m in (model_pool or []) if str(m).strip()],
            context_window_floor=(int(context_window_floor)
                                  if context_window_floor not in (None, "", 0) else None),
            runtime_id=runtime_id, workspace_path=workspace_path,
        )
        r = self._role(role)
        with r.lock:
            self._deliver_locked(conn, r, _Queued(
                text=prompt, run_id=run_id, task_id=task_id, spec=spec))
            queued = any(item.run_id == run_id for item in r.queue) if run_id else bool(r.queue)
            return {
                "session_id": r.session_id,
                "pid": r.proc.pid if r.proc else None,
                "alive": bool(r.proc),
                "queued": queued,
                "queue_len": len(r.queue),
                "task_id": task_id,
            }

    def deliver(self, conn, role: str, prompt: str, run_id: str | None = None,
                task_id: str | None = None) -> bool:
        """向已存在的常驻进程投递消息（无进程返回 False，由调用方走 spawn 路径）。"""
        r = self._roles.get(role)
        if r is None or r.proc is None or r.proc.poll() is not None:
            return False
        with r.lock:
            spec = r.loaded_spec or _SpawnSpec(
                executable="", args_new=[], args_resume=[], ctx={}, cwd="", env={})
            self._deliver_locked(conn, r, _Queued(
                text=prompt, run_id=run_id, task_id=task_id, spec=spec))
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
                "hard_limit_tokens": _hard_limit_for(window),
                "hint_tokens": int(window * config.CONTEXT_HINT_RATIO),
                "payload_chars": 0,
                "payload_limit_chars": int(config.PAYLOAD_HARD_RESET_CHARS),
                "current_task_id": None,
                "current_run_id": None,
                "current_step_id": None,
                "context_task_id": None,
                "queue_len": 0,
                "queue": [],
                "switching": False,
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
            "context_task_id": task_id_of(r.context_key),
            "context_key": r.context_key,
            "switching": bool(r.switching),
            "queue_len": len(r.queue),
            "queue": [
                {"run_id": q.run_id, "task_id": q.task_id} for q in r.queue
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

    def cancel_task(self, role: str, task_id: str) -> bool:
        """任务删除：丢弃该任务的消息、删除其上下文槽。

        - 当前加载/正忙于此任务：杀进程、清内存上下文，然后处理剩余队列；
        - 加载/正忙其它任务：不动进程，只删该任务槽与队列项。
        返回是否终止了进程。
        """
        key = task_key(task_id)
        r = self._roles.get(role)
        conn = open_conn(self.db_path)
        try:
            kill = False
            if r is not None:
                with r.lock:
                    r.queue = [item for item in r.queue if item.task_id != task_id]
                    if r.context_key == key:
                        r.suppress_exit = True
                        self._slot_persist(r, status="idle", conn=conn)
                        r.current_run_id = None
                        r.current_task_id = None
                        r.current_step_id = None
                        r.current_log_path = None
                        self._kill_locked(r, graceful=True)
                        r.suppress_exit = False
                        self._slot_clear_memory(r)
                        r.cic_pending = False
                        r.busy = False
                        r.busy_since = None
                        kill = True
                        self._db_upsert(conn, r, status="idle")
                        if r.queue:
                            # 当前槽已删除；把队列里属于其它任务的消息继续处理。
                            self._flush_next_locked(conn, r)
            clear_session(conn, role, "task deleted", task_key_value=key)
            return kill
        finally:
            conn.close()

    def kill(self, role: str) -> None:
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            # 主动终止不算 agent 失败，不触发 on_exit / 自动清会话。
            r.suppress_exit = True
            self._slot_persist(r)
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False

    def restart_process(self, role: str) -> None:
        """终止常驻进程但保留所有任务上下文槽；下一次唤醒恢复对应任务。"""
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            r.suppress_exit = True
            self._slot_persist(r)
            r.queue.clear()
            r.busy = False
            r.busy_since = None
            r.current_run_id = None
            r.current_task_id = None
            r.current_step_id = None
            r.current_log_path = None
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False
            r.context_key = None
            r.loaded_spec = None
            r.session_id = None

    def reset_session(self, role: str, cic: bool = False,
                      task_id=_ALL_SLOTS) -> None:
        """清空上下文槽并释放进程；下一次唤醒新建上下文。

        - `task_id` 省略：清空该角色的**所有任务槽**（角色级 reset）；
        - 指定 task_id（含 None 表示 __global__）：只清该任务的槽。
          daemon 只在被清槽正是当前加载槽时才杀进程；其它任务槽清了也不影响当前工作。

        cic=True 时，下一次唤醒先注入 CIC（MEMORY 头 + 最近消息/动作），适合
        角色自主决定“换新上下文，但保留必要交接”的场景。
        """
        clear_all = task_id is _ALL_SLOTS
        key = None if clear_all else task_key(task_id)
        r = self._roles.get(role)
        if r is not None:
            with r.lock:
                active = clear_all or r.context_key == key
                if active:
                    r.suppress_exit = True
                    self._slot_persist(r)
                    r.queue.clear()
                    self._kill_locked(r, graceful=True)
                    r.suppress_exit = False
                    r.context_key = None
                    r.loaded_spec = None
                    r.session_id = None
                    r.context_tokens = 0
                    r.slot_seen_activity = 0
                    r.payload_chars = 0
                    r.cic_pending = cic
                    r.busy = False
                    r.busy_since = None
                    r.current_run_id = None
                    r.current_task_id = None
                    r.current_step_id = None
                    r.current_log_path = None
        conn = open_conn(self.db_path)
        try:
            clear_session(
                conn, role, "session reset",
                task_key_value=_ALL_SLOTS if clear_all else key)
        finally:
            conn.close()

    def mark_needs_fresh_context(self, role: str, note: str = "fresh context required",
                                 task_id=_ALL_SLOTS) -> None:
        """标记某个任务槽下次唤醒使用全新 CIC 会话（不杀进程、不清排队消息）。

        `task_id` 省略时作用于当前加载槽；进程已死/会话损坏的健康检查应传入
        具体 run 的任务，避免误清其它任务的上下文。
        """
        r = self._role(role)
        with r.lock:
            key = r.context_key if task_id is _ALL_SLOTS else task_key(task_id)
            active = key is not None and r.context_key == key
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
            if key is None or active:
                conn.execute(
                    "UPDATE role_sessions SET session_id='', last_context_tokens=NULL, "
                    "last_error=?, updated_at=? WHERE role_slug=?",
                    (note, now_iso(), role))
            conn.commit()
        finally:
            conn.close()

    def shutdown(self) -> None:
        # 停机路径：不触发 on_exit（否则会把 run 标 failed/abandoned，与 suspend 语义冲突）
        self._shutting_down = True
        self._stop.set()
        for r in list(self._roles.values()):
            with r.lock:
                self._slot_persist(r)
                self._kill_locked(r, graceful=True)

    # ---- 内部 ------------------------------------------------------------

    def _role(self, role: str) -> _Role:
        with self._guard:
            return self._roles.setdefault(role, _Role(role=role))

    # ---- 任务上下文槽 ----------------------------------------------------

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
        conn.execute(
            """INSERT INTO role_task_sessions
                   (role_slug, task_key, session_id, runtime_id, cwd, workspace_path,
                    model_id, status, context_tokens, last_seen_activity_id,
                    last_active_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(role_slug, task_key) DO UPDATE SET
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
            (r.role, key, r.session_id or "", spec.runtime_id if spec else None,
             spec.cwd if spec else None, spec.workspace_path if spec else None,
             r.model_id, slot_status, int(r.context_tokens or 0),
             int(r.slot_seen_activity or 0), last_active, now, now),
        )
        # 同步角色级镜像：当前加载槽的 session / 水位。
        conn.execute(
            "UPDATE role_sessions SET session_id=?, model_id=COALESCE(?, model_id), "
            "last_context_tokens=?, current_task_id=?, current_run_id=?, "
            "current_step_id=?, updated_at=? WHERE role_slug=?",
            (r.session_id or "", r.model_id, int(r.context_tokens or 0),
             r.current_task_id, r.current_run_id, r.current_step_id, now, r.role),
        )

    def _slot_load(self, conn, role: str, key: str):
        return conn.execute(
            "SELECT * FROM role_task_sessions WHERE role_slug=? AND task_key=?",
            (role, key),
        ).fetchone()

    def _slot_seen_activity(self, conn, role: str, task_key_value: str) -> int:
        """任务当前最大活动 id；__global__ 槽不跟踪任务时间线。"""
        if task_key_value == GLOBAL_TASK_KEY:
            return 0
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM activities WHERE task_id=?",
            (task_key_value,),
        ).fetchone()
        return int(row["m"]) if row else 0

    def _slot_handoff(self, conn, role: str, key: str, since_id: int) -> str:
        """恢复会话时注入「离开期间的新进展」，避免角色用过时状态继续工作。"""
        if key == GLOBAL_TASK_KEY:
            return ""
        rows = conn.execute(
            """SELECT id, role_slug, kind, payload_json, created_at
                 FROM activities
                WHERE task_id=? AND id>? AND (role_slug IS NULL OR role_slug<>?)
                ORDER BY id LIMIT 40""",
            (key, int(since_id or 0), role),
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
                + f"\n（完整内容用 `anvil inbox check {key}` 拉取）\n\n")

    def _slot_clear_memory(self, r: _Role) -> None:
        r.context_key = None
        r.loaded_spec = None
        r.session_id = None
        r.context_tokens = 0
        r.slot_seen_activity = 0
        r.payload_chars = 0
        r.handoff_pending = ""

    def _spawn(self, conn, r: _Role, spec: _SpawnSpec) -> None:
        from ..graph.render import render_template

        row = conn.execute(
            "SELECT token FROM role_sessions WHERE role_slug=?", (r.role,)
        ).fetchone()
        resume = bool(r.session_id)
        if resume and int(r.context_tokens or 0) >= self._cic_threshold(r):
            # 会话过大：不 resume，按 CIC 构造新会话首输入（见 _build_cic）
            resume = False
            r.cic_pending = True
        sid = r.session_id if resume else str(uuid.uuid4())
        if not resume:
            r.payload_chars = 0  # 新会话不再携带旧图片/附件
        token = (row["token"] if row else None) or self._issue_token(conn, r.role)

        tpl = spec.args_resume if resume else spec.args_new
        full_ctx = dict(spec.ctx, session_id=sid, session_token=token)
        args = [render_template(str(a), full_ctx) for a in tpl]
        merged_env = _clean_env(dict(spec.env, ANVIL_TOKEN=token, ANVIL_AGENT_SLUG=r.role))
        # ANVIL_TASK_ID 保留 spawn 时的任务 id（跨任务时以消息前缀为准），
        # 避免 agent 执行 `anvil inbox check` 等命令时 task_id 为空。

        # 二进制管道：由 _reader_loop 统一按 UTF-8/本地编码回退解码，
        # 避免 Windows text 模式按 GBK 解码或把 \n 改写成 \r\n。
        kwargs: dict = dict(
            cwd=spec.cwd, env=merged_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            proc = subprocess.Popen(
                [spec.executable, *args], start_new_session=True, **kwargs)
        except TypeError:  # Windows 不支持 start_new_session
            proc = subprocess.Popen([spec.executable, *args], **kwargs)
        r.proc = proc
        self._log(r.role,
                  f"[anvil] spawn {spec.executable} {' '.join(str(a) for a in args)}")
        r.session_id = sid
        r.token = token
        r.loaded_spec = spec
        r.model_pool = list(spec.model_pool or [])
        r.context_window_floor = spec.context_window_floor
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
        """冷唤醒/CIC 重置阈值：显式配置优先，否则按策略窗口比例。"""
        if config.CIC_THRESHOLD_TOKENS > 0:
            return int(config.CIC_THRESHOLD_TOKENS)
        return max(1, int(self._context_window(r) * config.CIC_RESET_RATIO))

    def _hint_threshold(self, r: _Role) -> int:
        return max(1, int(self._context_window(r) * config.CONTEXT_HINT_RATIO))

    def _hard_threshold(self, r: _Role) -> int:
        """单轮内主动换会话的硬阈值。"""
        return _hard_limit_for(self._context_window(r))

    def _context_hint(self, r: _Role, task_id: str | None) -> str:
        """给角色的会话决策提示：只在切换任务或上下文占用较高时出现。"""
        tokens = int(r.context_tokens or 0)
        active_window = self._active_context_window(r)
        window = self._context_window(r)
        threshold = self._cic_threshold(r)
        changed = bool(r.last_task_id and task_id and r.last_task_id != task_id)
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
        if changed:
            lines.append("- 任务已切换；若与本任务不相关，建议先重置会话。")
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

    def _deliver_locked(self, conn, r: _Role, item: _Queued) -> None:
        r.queue.append(item)
        if not r.busy:
            self._flush_next_locked(conn, r)

    def _mark_idle_locked(self, conn, r: _Role) -> None:
        r.busy = False
        r.busy_since = None
        r.current_run_id = None
        r.current_task_id = None
        r.current_step_id = None
        r.current_log_path = None
        if r.context_key:
            self._slot_persist(r, status="idle", conn=conn)
        self._db_upsert(conn, r, status="idle")

    def _flush_next_locked(self, conn, r: _Role) -> None:
        if not r.queue:
            self._mark_idle_locked(conn, r)
            return
        item = r.queue.pop(0)
        target_key = task_key(item.task_id)
        same_context = (
            r.proc is not None
            and r.proc.poll() is None
            and r.context_key == target_key
        )
        if same_context:
            self._send_locked(conn, r, item)
        else:
            self._switch_context_locked(conn, r, item)

    def _switch_context_locked(self, conn, r: _Role, item: _Queued) -> None:
        """在 turn 边界切换任务上下文：保存当前槽 → 重开进程 → 恢复目标槽。"""
        target_key = task_key(item.task_id)
        # 1) 保存当前上下文
        if r.context_key:
            self._slot_persist(r, status="idle", conn=conn)
        # 2) 计划内切换：旧进程退出不能触发 on_exit / 自动判失败。
        #    switching=True 让调度器在「杀旧进程 → 拉新进程」窗口内不要把它当死进程回收。
        r.switching = True
        try:
            self._switch_context_inner(conn, r, item, target_key)
        finally:
            r.switching = False

    def _switch_context_inner(self, conn, r: _Role, item: _Queued,
                              target_key: str) -> None:
        if r.proc is not None and r.proc.poll() is None:
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False
            # 旧槽只是释放进程，session 仍可恢复：槽状态回到 idle。
            if r.context_key:
                self._slot_persist(r, status="idle", conn=conn)
        # 3) 加载目标槽
        row = self._slot_load(conn, r.role, target_key)
        r.context_key = target_key
        r.loaded_spec = item.spec
        r.slot_seen_activity = int(row["last_seen_activity_id"] or 0) if row else 0
        r.model_id = (row["model_id"] if row else None)
        r.served_model_id = None
        r.context_tokens = int(row["context_tokens"] or 0) if row else 0
        r.payload_chars = 0
        r.handoff_pending = ""
        resume = bool(row and row["session_id"])
        rotate_reason = None
        if resume:
            old_cwd = str(row["cwd"] or "")
            new_cwd = str(item.spec.cwd or "")
            if old_cwd and new_cwd and old_cwd != new_cwd:
                # Claude Code 的会话按 cwd 分目录；cwd 变了就不能 --resume。
                self._audit_session(
                    conn, r.role, item.task_id, "session.cwd_changed_rotate",
                    {"from": old_cwd, "to": new_cwd})
                rotate_reason = "cwd-changed"
                resume = False
            elif int(row["context_tokens"] or 0) >= self._cic_threshold(r):
                rotate_reason = "context-high-on-resume"
                resume = False
        if rotate_reason:
            # 换新上下文前先固化该任务的决策/进度，CIC 才能接得上。
            try:
                from . import memory as memory_service
                memory_service.consolidate_context(
                    conn, r.role, item.task_id, rotate_reason, None)
            except Exception:  # noqa: BLE001 —— 快照失败不阻断恢复
                self._log(r.role, f"[anvil] knowledge snapshot failed ({rotate_reason})")
        r.session_id = row["session_id"] if resume else None
        r.cic_pending = not resume
        if resume:
            r.handoff_pending = self._slot_handoff(
                conn, r.role, target_key, r.slot_seen_activity)
        self._log(
            r.role,
            f"[anvil] context switch → {target_key} "
            f"({'resume ' + str(r.session_id) if resume else 'new session + CIC'})")
        # 4) 拉起目标槽并投递
        self._spawn(conn, r, item.spec)
        self._send_locked(conn, r, item)

    def _send_locked(self, conn, r: _Role, item: _Queued) -> None:
        spec = item.spec
        r.loaded_spec = spec
        if spec.model_pool:
            r.model_pool = list(spec.model_pool)
        if spec.context_window_floor:
            r.context_window_floor = spec.context_window_floor
        # 上下文已按任务切换；_context_hint 不应再提示“任务已切换，建议重置”。
        r.last_task_id = item.task_id
        task_prefix = (
            f"当前任务 ID：{item.task_id}\n"
            "（环境变量 ANVIL_TASK_ID 可能已过期，请以此为准）\n\n"
            if item.task_id else "")
        if r.cic_pending:
            prefix = self._build_cic(conn, r.role, item.task_id) + "\n\n" + task_prefix
            r.cic_pending = False
        else:
            prefix = (r.handoff_pending
                      + self._context_hint(r, item.task_id)
                      + task_prefix)
        r.handoff_pending = ""
        text = prefix + item.text

        r.busy = True
        r.busy_since = time.time()
        r.current_run_id = item.run_id
        r.current_task_id = item.task_id
        row = (conn.execute("SELECT log_path, step_id FROM runs WHERE id=?",
                            (item.run_id,)).fetchone() if item.run_id else None)
        r.current_log_path = row["log_path"] if row else None
        r.current_step_id = row["step_id"] if row else None
        r.last_step_id = r.current_step_id
        # 投递成功后才推进「已看到」游标；失败时保留旧游标，下次恢复能补上未送达事件。
        seen = max(
            int(r.slot_seen_activity or 0),
            self._slot_seen_activity(conn, r.role, task_key(item.task_id)))
        self._slot_persist(r, status="busy", conn=conn)
        self._append_run_log(r, f"\n===== USER（上下文/提示词） @ {now_iso()} =====\n{text}\n")
        self._db_upsert(conn, r, status="busy")
        # 排队期间被标记 suspended 的 run，真正投递时恢复 running。
        if item.run_id:
            conn.execute(
                "UPDATE runs SET status='running', "
                "started_at=COALESCE(started_at, ?) "
                "WHERE id=? AND status IN ('created','projecting','suspended')",
                (now_iso(), item.run_id),
            )
            conn.commit()
        try:
            if r.proc and r.proc.stdin:
                r.proc.stdin.write(frame_user_message(text).encode("utf-8"))
                r.proc.stdin.flush()
                r.slot_seen_activity = seen
                self._slot_persist(r, status="busy", conn=conn)
        except (BrokenPipeError, OSError) as exc:
            self._log(r.role, f"[anvil] stdin write failed: {exc}")
            r.last_active = time.time()
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False
            self._db_upsert(conn, r, status="dead", error=str(exc))

    def _audit_session(self, conn, role: str, task_id: str | None, action: str,
                       payload: dict) -> None:
        try:
            conn.execute(
                "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (role, task_id, action, jdumps(payload), now_iso()))
            conn.commit()
        except Exception:  # noqa: BLE001 —— 审计失败不阻断运行
            pass

    def _reader_loop(self, r: _Role) -> None:
        proc = r.proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                # 进程已被计划内上下文切换替换：不再处理旧进程的缓冲事件。
                if r.proc is not proc:
                    return
                line = decode_child_output(raw).strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    self._log(r.role, line)  # claude 的警告行（如 unrecognized_model）
                    if _is_payload_overflow(line):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "payload")
                        break
                    if _is_context_overflow(line):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "api_error")
                        break
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    self._log(r.role, line)
                    continue
                if r.proc is not proc:
                    return
                self._append_run_log(r, self._format_event(ev))
                # 图片/附件不计 token，但会计入请求体；累计到阈值时主动换新会话，
                # 避免撞上 “request too large (max 32mb)”。
                added_chars = _attachment_chars(ev)
                payload_hit = False
                if added_chars:
                    with r.lock:
                        if r.proc is not proc:
                            return
                        r.payload_chars = int(r.payload_chars or 0) + added_chars
                        payload_hit = bool(
                            r.current_run_id and config.PAYLOAD_HARD_RESET_CHARS > 0
                            and r.payload_chars >= config.PAYLOAD_HARD_RESET_CHARS)
                    if payload_hit:
                        self._begin_context_overflow(r, "payload")
                        break
                etype = ev.get("type")
                if etype == "system" and ev.get("session_id"):
                    with r.lock:
                        if r.proc is not proc:
                            return
                        r.session_id = ev["session_id"]
                        model = ev.get("model") or ev.get("model_id")
                        if model:
                            r.model_id = str(model)
                elif etype == "result":
                    # Token 记账以 result 事件为准：assistant 事件没有 output_tokens，
                    # 且同一轮会重复带 input。每轮只记一次。
                    turn_usage = extract_turn_usage(ev)
                    if turn_usage:
                        with r.lock:
                            usage_task_id = r.current_task_id
                            usage_run_id = r.current_run_id
                        self._record_usage(r, turn_usage, usage_task_id, usage_run_id)
                    # 只认错误结果里的文本；正常 result 是 agent 的最终回答，
                    # 里面可能出现 “prompt is too long” 等字样（例如正在分析本 bug）。
                    result_text = str(ev.get("result") or ev.get("error") or "")
                    if ev.get("is_error") and _is_payload_overflow(result_text):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "payload")
                        break
                    if ev.get("is_error") and _is_context_overflow(result_text):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "api_error")
                        break
                    if r.proc is not proc:
                        return
                    self._on_turn_result(r, ev, proc=proc)
                elif etype == "assistant":
                    msg = ev.get("message") or {}
                    model = msg.get("model") or ev.get("model")
                    old_model = None
                    usage: dict = {}
                    switched = False
                    hard = False
                    with r.lock:
                        if r.proc is not proc:
                            return
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
                        usage = msg.get("usage") or {}
                        if usage:
                            try:
                                ctx = sum(int(usage.get(k) or 0) for k in (
                                    "input_tokens", "cache_read_input_tokens",
                                    "cache_creation_input_tokens"))
                                if ctx:
                                    r.context_tokens = ctx
                            except (TypeError, ValueError):
                                pass
                        task_id, run_id = r.current_task_id, r.current_run_id
                        switched = bool(
                            old_model and r.model_id and r.model_id != old_model)
                        hard = bool(
                            r.current_run_id
                            and int(r.context_tokens or 0) >= self._hard_threshold(r))
                    self._log(r.role, self._assistant_text(ev))
                    if switched:
                        self._on_model_switch(r, old_model, r.model_id, task_id)
                    # 结构化 API 错误（synthetic assistant 消息）也要识别，避免只依赖
                    # 原始文本；同时检查单轮硬阈值——一次唤醒可能连续跑几百次工具调用，
                    # CLI 的自动压缩来不及介入。
                    if ev.get("isApiErrorMessage") or msg.get("model") == "<synthetic>":
                        blocks = msg.get("content") or []
                        err_text = "".join(
                            b.get("text", "") for b in blocks if isinstance(b, dict))
                        if _is_payload_overflow(err_text):
                            if r.proc is proc:
                                self._begin_context_overflow(r, "payload")
                            break
                        if _is_context_overflow(err_text):
                            if r.proc is proc:
                                self._begin_context_overflow(r, "api_error")
                            break
                    if hard:
                        if r.proc is not proc:
                            return
                        self._begin_context_overflow(r, "threshold")
                        break
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
        overflow_run_id: str | None = None
        overflow_reason: str | None = None
        was_current = False
        suppress = False
        reset_key: str | None = None
        with r.lock:
            # 进程可能已被一次计划内上下文切换替换：旧 reader 只负责安静退出。
            if r.proc is not proc:
                return
            was_current = True
            suppress = bool(r.suppress_exit)
            auto_reset = (
                code not in (None, 0)
                and not suppress
                and not self._shutting_down
                and config.RESET_SESSION_ON_FAILURE
            )
            r.proc = None
            r.busy = False
            r.busy_since = None
            r.current_step_id = None
            if auto_reset:
                reset_key = r.context_key
                r.session_id = None
                r.context_tokens = 0
                # 崩溃后的重试用 CIC：保留 MEMORY/工作流决策/知识快照，
                # 不从一张白纸重新开始。
                r.cic_pending = True
            if r.context_overflow:
                overflow_run_id = r.current_run_id
                overflow_reason = r.context_overflow_reason or "threshold"
                reset_key = r.context_key
                r.context_overflow = False
                r.context_overflow_reason = None
        conn = open_conn(self.db_path)
        try:
            note = f"auto reset after exit code {code}" if auto_reset else None
            if overflow_run_id:
                note = f"context overflow ({overflow_reason})"
            self._db_upsert(conn, r, status="dead", pid=None, error=note)
            if reset_key:
                # 只清当前任务的槽；其它任务的上下文不受影响。
                clear_session(conn, r.role, note or "auto reset after failure",
                              task_key_value=reset_key)
        finally:
            conn.close()
        if not was_current:
            return
        if overflow_run_id and not self._shutting_down and self.on_context_overflow:
            # 不在 reader 线程里同步跑编排器：投递/拉起新进程会重新获取 r.lock。
            threading.Thread(
                target=self._notify_context_overflow,
                args=(r.role, overflow_run_id, overflow_reason or "threshold"),
                daemon=True, name=f"anvil-overflow-{r.role}").start()
        elif self.on_exit and not self._shutting_down and not suppress:
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
            out = [f"[result] usage={jdumps(usage)}"]
            if ev.get("result") is not None:
                out.append(str(ev.get("result")))
            return "\n".join(out)
        return None

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
                conn.execute(
                    "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (r.role, task_id, "session.model_switch",
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

    def _begin_context_overflow(self, r: _Role, reason: str) -> None:
        """上下文超限（主动阈值或 API 报错）：终止本会话，交给回调换 CIC 新会话。

        幂等：同一轮内只处理一次。等待 reader 线程走正常退出路径，由它调用
        `on_context_overflow(role, run_id, reason)`，避免在 reader 线程里重入编排器。
        """
        if r.context_overflow:
            return
        r.context_overflow = True
        r.context_overflow_reason = reason
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
            # terminate 被忽略时兜底 kill，避免残留进程变成同一角色的第二个实例。
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
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
                conn, r.role, r.current_task_id, trigger, r.current_run_id)
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
            if run_id:
                row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                if row and row["status"] == "running":
                    from . import runs as runs_service
                    runs_service.set_status(conn, run_id, "suspended")
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
                    self._flush_next_locked(conn, r)
        finally:
            conn.close()

    def _idle_loop(self) -> None:
        while not self._stop.wait(30):
            now = time.time()
            for r in list(self._roles.values()):
                with r.lock:
                    # 锁内重查：扫描到上锁之间可能已完成一次上下文切换/新唤醒。
                    if r.proc is None or r.busy or r.switching:
                        continue
                    idle_minutes = (now - (r.last_active or now)) / 60.0
                    if idle_minutes >= self.idle_release_minutes:
                        self._slot_persist(r, status="idle")
                        self._kill_locked(r, graceful=True)
                        self._slot_persist(r, status="idle")

    def _kill_locked(self, r: _Role, graceful: bool = True) -> None:
        proc = r.proc
        if proc is None:
            return
        # 先与进程失联：旧 reader 仍可能有缓冲事件，必须立即停止回写共享状态，
        # 否则上下文切换后旧 turn 的 result 会修改新任务的 current_run/tokens。
        r.proc = None
        r.busy = False
        r.busy_since = None
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
        conn = open_conn(self.db_path)
        try:
            self._db_upsert(conn, r, status="dead", pid=None)
        finally:
            conn.close()

    def _build_cic(self, conn, role: str, task_id: str | None) -> str:
        """Constructed Initial Context：任务知识 + 可复用知识 + 最近消息/动作。

        三条负规则：不宣布重置、不列未决问题清单、超过 2 小时的内容只给指针。
        task-scoped 内容只允许当前 task_id；role-scoped 可复用知识可跨任务。
        """
        from . import knowledge as knowledge_service  # 惰性导入避免循环
        from . import memory as memory_service

        task = None
        if task_id:
            task = conn.execute(
                "SELECT id,title,status,tags_json,repo,scope_paths_json FROM tasks "
                "WHERE id=?", (task_id,)).fetchone()
        tags = [str(t) for t in jloads(task["tags_json"], [])] if task else []
        blocks: list[dict] = []

        if task is not None:
            header = (f"## 当前任务\n- {task['title']}（{task_id}）\n"
                      f"- 状态：{task['status']}")
            if tags:
                header += "\n- 标签：" + "、".join(tags)
            if task["repo"]:
                header += f"\n- 仓库：{task['repo']}"
            blocks.append({
                "priority": 5, "order": 5, "cap": 500, "min": 120, "text": header})

            snap = knowledge_service.latest_task_snapshot(conn, role, task_id)
            if snap:
                blocks.append({
                    "priority": 20, "order": 30, "cap": 4000, "min": 600,
                    "text": "## 最近知识快照（自动整理）\n" + str(snap["content"])[:3900],
                })

            query = " ".join([str(task["title"] or ""), *tags,
                              str(task["repo"] or "")]).strip()
            hits = knowledge_service.search_knowledge(
                conn, role, query, task_id=task_id, scope="auto",
                limit=5, snippet_chars=600, repo_scope="same_or_shared") if query else []
            hit_text = knowledge_service.format_hits(
                hits, max_item_chars=600, max_total_chars=4000)
            if hit_text:
                blocks.append({
                    "priority": 40, "order": 40, "cap": 4000, "min": 400,
                    "text": "## 相关记忆（按任务检索）\n" + hit_text,
                })

            wf = conn.execute(
                "SELECT id FROM workflows WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone()
            if wf is not None:
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM workflow_steps "
                    "WHERE workflow_id=? GROUP BY status", (wf["id"],)).fetchall()
                steps = conn.execute(
                    "SELECT step_id, title, role_slug, status, summary, feedback "
                    "FROM workflow_steps WHERE workflow_id=? "
                    "ORDER BY updated_at DESC, step_id DESC LIMIT 30", (wf["id"],)).fetchall()
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
                    })
        else:
            # 无任务（reminder/直聊）：只取 role-scoped 的最近知识，不碰 task scope。
            hits = knowledge_service.search_knowledge(
                conn, role, "", task_id=None, scope="role",
                limit=5, snippet_chars=300, repo_scope="any")
            hit_text = knowledge_service.format_hits(
                hits, max_item_chars=400, max_total_chars=1800)
            if hit_text:
                blocks.append({
                    "priority": 40, "order": 40, "cap": 1800, "min": 200,
                    "text": "## 相关记忆（角色知识库）\n" + hit_text,
                })

        lessons = memory_service.shared_lessons(
            conn, tags, limit=8, repo=(task["repo"] if task else None))
        if lessons:
            lesson_lines = [f"- {str(x.get('content') or '')[:500]}" for x in lessons]
            blocks.append({
                "priority": 60, "order": 60, "cap": 1500, "min": 200,
                "text": "## 共享经验（已批准）\n" + "\n".join(lesson_lines),
            })

        # 留出块标题的长度，保证 MEMORY.md 的最新尾部（最重要的内容）不被截掉。
        mem = memory_service.read_memory_file(role, 3900).strip()
        if mem:
            blocks.append({
                "priority": 10, "order": 10, "cap": 4000, "min": 800,
                "text": "## 记忆（MEMORY.md 尾部，无需重读全文）\n" + mem,
            })

        if task is not None:
            rows = conn.execute(
                "SELECT id, role_slug, kind, payload_json FROM activities "
                "WHERE task_id=? AND kind IN ('comment','mention','step') "
                "ORDER BY id DESC LIMIT 25", (task_id,)).fetchall()
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
                })
            acts = conn.execute(
                "SELECT role_slug, action FROM audit WHERE task_id=? ORDER BY id DESC LIMIT 12",
                (task_id,)).fetchall()
            if acts:
                blocks.append({
                    "priority": 80, "order": 80, "cap": 1000, "min": 100,
                    "text": "## 最近动作\n" + "\n".join(
                        f"- {a['role_slug'] or 'system'}: {a['action']}"
                        for a in reversed(acts)),
                })

        idx = knowledge_service.knowledge_index(conn, role, task_id)
        if idx["total_role"] or idx["total_task"]:
            labels = {"vulnerability_pattern": "漏洞模式",
                      "failure_scenario": "故障场景", "module_knowledge": "模块知识",
                      "decision": "决策", "note": "笔记"}
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
            })

        return knowledge_service.pack_blocks(blocks, 32000)

    def _db_upsert(self, conn, r: _Role, status: str, pid: int | None = -1,
                   error: str | None = None) -> None:
        if pid == -1:
            pid = r.proc.pid if r.proc else None
        conn.execute(
            "INSERT INTO role_sessions (role_slug, session_id, token, model_id, pid, status, "
            "busy_since, last_active_at, last_context_tokens, last_error, "
            "current_task_id, current_run_id, current_step_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_slug) DO UPDATE SET session_id=excluded.session_id, token=excluded.token, "
            "model_id=COALESCE(excluded.model_id, role_sessions.model_id), "
            "pid=excluded.pid, status=excluded.status, busy_since=excluded.busy_since, "
            "last_active_at=excluded.last_active_at, "
            "last_context_tokens=COALESCE(excluded.last_context_tokens, role_sessions.last_context_tokens), "
            "last_error=excluded.last_error, current_task_id=excluded.current_task_id, "
            "current_run_id=excluded.current_run_id, current_step_id=excluded.current_step_id, "
            "updated_at=excluded.updated_at",
            (r.role, r.session_id or "", r.token, r.model_id, pid, status,
             datetime.fromtimestamp(r.busy_since, tz=timezone.utc).isoformat() if r.busy_since else None,
             datetime.fromtimestamp(r.last_active, tz=timezone.utc).isoformat() if r.last_active else None,
             r.context_tokens or None, error,
             r.current_task_id, r.current_run_id, r.current_step_id, now_iso()),
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
            conn.execute("UPDATE role_sessions SET status='dead', pid=NULL, updated_at=? "
                         "WHERE status IN ('starting','idle','busy')", (now_iso(),))
            conn.execute("UPDATE role_task_sessions SET status='dead', updated_at=? "
                         "WHERE status IN ('idle','busy')", (now_iso(),))
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
