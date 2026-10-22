"""Small shared helpers: timestamps, JSON, id generation, child-output decoding."""
from __future__ import annotations

import codecs
import json
import locale
import uuid
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def jdumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jloads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _decode_line(raw: bytes) -> str:
    """单行解码：UTF-8 优先，失败后回退到系统本地编码/GB18030/GBK。"""
    for enc in ("utf-8", locale.getpreferredencoding(False) or "utf-8",
                "gb18030", "gbk", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def decode_child_output(data: bytes) -> str:
    """解码 CLI 子进程的 stdout/stderr。

    Windows 上子进程可能按系统 ANSI/OEM 代码页（中文环境常见 GBK/CP936）输出，
    而前端与日志按 UTF-8 展示，直接 UTF-8 解码会产生乱码/替换字符。
    这里先按 UTF-8（含 BOM）解码；失败后逐行回退到本地编码与 GB18030/GBK，
    兼容同一日志里 UTF-8 与 GBK 行混排的情况。
    """
    if not data:
        return ""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith(codecs.BOM_UTF16_LE):
        return data.decode("utf-16-le", errors="replace")
    if data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16-be", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return "\n".join(_decode_line(part) for part in data.split(b"\n"))


class ConflictError(Exception):
    """状态转换/治理闸门被拒 → HTTP 409。"""
