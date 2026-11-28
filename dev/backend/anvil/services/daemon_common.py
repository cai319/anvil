"""Daemon 共享类型与上下文工具（进程模型/用量/CIC 基础件）。"""
from __future__ import annotations

import json
import logging
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
from ..util import (decode_child_output, force_utf8_stdio, jdumps, jloads, now_iso,
                    scrub_agent_env)
from . import ledger


_SESSION_ENV_KEYS = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_ENTRYPOINT",
}


_MODEL_VARIANT_SUFFIX = re.compile(r"\[(?:1m|200k|128k)\]$", re.IGNORECASE)


logger = logging.getLogger("anvil.daemon")


class _DeliveryError(RuntimeError):
    """投递失败（spawn/stdin）；由队列循环记录并继续下一条。"""


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    # 继承环境里不能带人类认证；extra 只允许注入角色自己的运行级 ANVIL_TOKEN。
    base = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV_KEYS}
    merged = scrub_agent_env(base, allow_run_token=False)
    merged.update(scrub_agent_env(extra, allow_run_token=True))
    # Windows 默认按系统 ANSI/OEM 代码页输出；强制 Python 子进程使用 UTF-8，
    # 避免角色/tool 输出的中文在 stdout 日志里变成乱码。必须覆盖继承值：
    # 父环境或 runtime env 里的 PYTHONIOENCODING=gbk 会让 setdefault 失效。
    return force_utf8_stdio(merged)


def _hard_limit_for(window: int) -> int:
    """给定窗口的硬阈值；0 比例表示关闭（返回窗口+1）。"""
    ratio = float(config.CONTEXT_HARD_RESET_RATIO)
    if ratio <= 0:
        return window + 1
    reserve = max(0, int(config.CONTEXT_OUTPUT_RESERVE_TOKENS))
    # API 上限按 input+max_tokens 计：既要留出响应额度，也要留出比例余量。
    return max(1, min(int(window * min(ratio, 1.0)), max(1, window - reserve)))


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
RSI_REFLECT_TASK_KEY = "__rsi_reflect__"


CONTEXT_SEP = "::slice::"
# 可选 stage 维度：追加在 task/slice 之后，例如 T1::slice::A::stage::verify。
# 旧键没有该后缀，解析与输出保持完全一致。
CONTEXT_STAGE_SEP = "::stage::"


_ALL_SLOTS = object()


def context_key(task_id: str | None, slice_key: str | None = None,
                origin: str | None = None, stage: str | None = None) -> str:
    """上下文槽 key：task 级 / slice 级 / __global__ / RSI 反思独立槽。

    stage 是可选维度，用于同一 (task, slice) 下按阶段隔离会话槽；不传
    stage 时输出与旧键逐字节一致（扩展步骤，不改变现有行为）。
    """
    if origin == "rsi_reflect":
        return RSI_REFLECT_TASK_KEY
    if not task_id:
        return GLOBAL_TASK_KEY
    key = f"{task_id}{CONTEXT_SEP}{slice_key}" if slice_key else str(task_id)
    stage_text = str(stage or "").strip()
    return f"{key}{CONTEXT_STAGE_SEP}{stage_text}" if stage_text else key


def context_parts_full(
        key: str | None) -> tuple[str | None, str | None, str | None]:
    """Parse a context key into (task_id, slice_key, stage).

    旧键没有 stage 后缀，stage 恒为 None；task/slice 结果与 ``context_parts``
    完全一致。
    """
    if key in (None, GLOBAL_TASK_KEY):
        return (None, None, None)
    text = str(key)
    stage = None
    if CONTEXT_STAGE_SEP in text:
        text, stage = text.split(CONTEXT_STAGE_SEP, 1)
        stage = stage or None
    if CONTEXT_SEP in text:
        task_id, slice_key = text.split(CONTEXT_SEP, 1)
        return (task_id or None, slice_key or None, stage)
    return (text, None, stage)


def context_parts(key: str | None) -> tuple[str | None, str | None]:
    """Parse a context key into (task_id, slice_key); stage 维度被忽略。"""
    task_id, slice_key, _stage = context_parts_full(key)
    return (task_id, slice_key)


def task_key(task_id: str | None) -> str:
    """兼容旧调用：task 级上下文 key。"""
    return context_key(task_id)


def task_id_of(key: str | None) -> str | None:
    return context_parts(key)[0]


def slice_key_of(key: str | None) -> str | None:
    return context_parts(key)[1]


def stage_of(key: str | None) -> str | None:
    """上下文 key 的 stage 维度；旧键返回 None。"""
    return context_parts_full(key)[2]


def clear_session(conn, role: str, note: str = "session reset",
                  task_key_value=_ALL_SLOTS, all_slices: bool = False) -> None:
    """清空角色会话槽；下一次唤醒新建会话（不 --resume）。

    task_key_value 默认清空该角色的**所有任务槽**；传入具体 key 时只清对应
    context（task 级 key 只清 task 级槽；slice key 只清该 slice）。
    `all_slices=True` 才把 task id 展开成该 task 的全部 slice 槽（任务删除用）。
    role_sessions 是当前活跃进程的镜像，只有清到当前槽（或全部）时才清空镜像。
    """
    if task_key_value is _ALL_SLOTS:
        conn.execute("DELETE FROM role_task_sessions WHERE role_slug=?", (role,))
        conn.execute(
            "UPDATE role_sessions SET session_id='', status='dead', pid=NULL, "
            "last_context_tokens=NULL, current_task_id=NULL, current_run_id=NULL, "
            "current_step_id=NULL, current_context_key=NULL, current_slice_key=NULL, current_stage=NULL, "
            "last_error=?, updated_at=? WHERE role_slug=?",
            (note, now_iso(), role),
        )
    else:
        key = str(task_key_value)
        exact_slice = CONTEXT_SEP in key and key != GLOBAL_TASK_KEY
        task_id = context_parts(key)[0]
        if exact_slice:
            conn.execute(
                "DELETE FROM role_task_sessions WHERE role_slug=? AND task_key=?",
                (role, key),
            )
        elif key == GLOBAL_TASK_KEY:
            conn.execute(
                "DELETE FROM role_task_sessions WHERE role_slug=? AND task_key=?",
                (role, key),
            )
        elif all_slices:
            # 任务删除：展开为该 task 的全部 slice 槽。
            conn.execute(
                "DELETE FROM role_task_sessions WHERE role_slug=? "
                "AND (task_id=? OR task_key=?)",
                (role, task_id, task_id),
            )
        else:
            # 精确 task 级 key：只清 task 级槽，不误伤同 task 的 slice 槽。
            conn.execute(
                "DELETE FROM role_task_sessions WHERE role_slug=? AND task_key=?",
                (role, key),
            )
        row = conn.execute(
            "SELECT current_context_key, current_task_id FROM role_sessions WHERE role_slug=?",
            (role,),
        ).fetchone()
        active_key = row["current_context_key"] if row else None
        if not active_key and row:
            active_key = context_key(row["current_task_id"])
        active_task = task_id_of(active_key)
        if all_slices and not exact_slice and key != GLOBAL_TASK_KEY:
            clear_active = active_task == task_id
        else:
            clear_active = active_key == key
        if clear_active:
            conn.execute(
                "UPDATE role_sessions SET session_id='', last_context_tokens=NULL, "
                "current_task_id=NULL, current_run_id=NULL, current_step_id=NULL, "
                "current_context_key=NULL, current_slice_key=NULL, current_stage=NULL, "
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
    # v1.60 沙箱策略快照；digest 变化时在同一上下文槽内重建进程。
    sandbox_policy: dict = field(default_factory=dict)
    sandbox_policy_digest: str | None = None


@dataclass
class _Queued:
    """一条待投递消息；task_id + slice_key 决定文本属于哪个上下文槽。"""
    text: str
    run_id: str | None
    task_id: str | None
    spec: _SpawnSpec
    slice_key: str | None = None
    priority: int = 100
    # 入队时 run.wake_note 的快照；写 stdin 成功后按它做 CAS 清除，
    # 避免把渲染后追加的新 note 一并清掉。
    wake_note: str | None = None


def clear_slice_sessions(conn, role: str, task_id: str,
                         note: str = "workflow restarted") -> None:
    """只清该 task 的 slice 槽，保留 task 级上下文槽。

    新一次 workflow execution 的 slice_key 可能复用旧 key；不清旧槽就会
    --resume 上一次执行的对话。task 级上下文（规划/讨论）不在清理范围。
    """
    conn.execute(
        "DELETE FROM role_task_sessions WHERE role_slug=? AND task_id=? "
        "AND (slice_key IS NOT NULL OR task_key LIKE ?)",
        (role, task_id, f"%{CONTEXT_SEP}%"),
    )
    row = conn.execute(
        "SELECT current_task_id, current_context_key, current_slice_key "
        "FROM role_sessions WHERE role_slug=?", (role,)).fetchone()
    if row is not None and row["current_task_id"] == task_id and (
            row["current_slice_key"]
            or CONTEXT_SEP in str(row["current_context_key"] or "")):
        conn.execute(
            "UPDATE role_sessions SET session_id='', last_context_tokens=NULL, "
            "current_task_id=NULL, current_run_id=NULL, current_step_id=NULL, "
            "current_context_key=NULL, current_slice_key=NULL, current_stage=NULL, "
            "last_error=?, updated_at=? WHERE role_slug=?",
            (note, now_iso(), role))
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
    queue: list[_Queued] = field(default_factory=list)
    current_run_id: str | None = None
    current_task_id: str | None = None
    current_step_id: str | None = None
    last_task_id: str | None = None
    last_slice_key: str | None = None
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
    # 最近一次已完成投影的 run；同一 run 的重复投递不重复投影。
    last_projected_run_id: str | None = None
    context_overflow: bool = False
    context_overflow_reason: str | None = None
    # 触发溢出的进程；旧 reader 输掉进程竞态时不能被后来者消费。
    context_overflow_proc: subprocess.Popen | None = None
    # 进程世代：每次 spawn +1，用于防止旧 reader 回写新进程状态。
    proc_generation: int = 0
    # v1.60 沙箱实例（与当前进程一对一；kill/退出后清空）。
    sandbox_instance_id: str | None = None
    sandbox_backend: str | None = None
    sandbox_policy_digest: str | None = None
    suppress_exit: bool = False
    current_log_path: str | None = None
    # 当前 run 中运行时原生 subagent（Task/Agent）的观测状态；不是 Anvil 任务。
    active_subagents: dict[str, dict] = field(default_factory=dict)
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    stop: bool = False
