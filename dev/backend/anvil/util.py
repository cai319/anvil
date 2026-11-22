"""Small shared helpers: timestamps, JSON, id generation, child-output decoding."""
from __future__ import annotations

import codecs
import hashlib
import json
import locale
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 东亚多字节代码页（codecs.lookup().name 规范化后的名字）：这些 locale 可以
# 优先信任；cp1252 等单字节代码页几乎能解码任意字节，不能排在这些编码前面。
_CJK_CODECS = {
    "gbk", "gb2312", "gb18030", "hz",
    "big5", "cp950", "cp932", "shift_jis",
    "cp949", "euc_kr", "johab",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


_AGENT_SECRET_ENV_KEYS = {
    "ANVIL_AUTH_TOKEN", "ANVIL_AUTH_TOKEN_FILE", "ANVIL_TEST_AUTH",
    "ANVIL_HUMAN_TOKEN", "ANVIL_SESSION",
}


def scrub_agent_env(env: dict[str, str], *, allow_run_token: bool = False) -> dict[str, str]:
    """剥离编排器的人类认证变量；Agent 只能拿到自己的运行级 ANVIL_TOKEN。"""
    out: dict[str, str] = {}
    for key, value in env.items():
        key_text = str(key)
        if key_text in _AGENT_SECRET_ENV_KEYS:
            continue
        if key_text == "ANVIL_TOKEN" and not allow_run_token:
            continue
        out[key_text] = value
    return out


def safe_component(value: Any, *, fallback: str = "item", max_len: int = 64) -> str:
    """把用户提供的 id/名称变成单段安全目录名；拒绝点段并加短 hash 防碰撞。"""
    raw = str(value or "")
    text = unicodedata.normalize("NFKC", raw).strip()
    safe = re.sub(r"[^\w.+-]", "_", text)[:max_len]
    if safe in ("", ".", "..") or not safe.strip("."):
        safe = fallback
    if safe != raw:
        safe = f"{safe[:max_len]}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]}"
    return safe


def validate_component_id(value: Any, *, label: str = "id") -> str:
    """入口 id 校验：拒绝绝对/穿越/空 id（调用方仍应使用 safe_component 做路径段）。"""
    text = str(value or "").strip()
    if (not text or text in (".", "..") or "/" in text or "\\" in text
            or "\x00" in text or not text.strip(".")):
        raise ValueError(f"invalid {label}")
    return text


def jdumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jloads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _try_decode(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _is_windows() -> bool:
    return os.name == "nt"


def _fallback_encoding() -> str:
    """系统 ANSI 代码页（规范化后的 codec 名）。

    不能用 locale.getpreferredencoding(False)：PYTHONUTF8=1 时它在 Windows
    恒返回 utf-8（PEP 540），中文 Windows 的 GBK 分支会静默失效。
    locale.getencoding() 忽略 UTF-8 mode，返回真实 ANSI 代码页。
    """
    try:
        getter = getattr(locale, "getencoding", None)
        encoding = getter() if getter is not None else locale.getpreferredencoding(False)
    except Exception:  # noqa: BLE001 —— locale 查询失败不应影响解码
        return "utf-8"
    try:
        return codecs.lookup(encoding or "utf-8").name
    except LookupError:
        return encoding or "utf-8"


def _is_cjk_codec(encoding: str) -> bool:
    return encoding in _CJK_CODECS


def _is_cjk_char(ch: str) -> bool:
    return (
        "㐀" <= ch <= "䶿"      # CJK 扩展 A
        or "一" <= ch <= "鿿"   # CJK 基本区
        or "　" <= ch <= "〿"   # CJK 标点
        or "＀" <= ch <= "￯"   # 全角字符
        or "぀" <= ch <= "ヿ"   # 日文假名
        or "가" <= ch <= "힯"   # 谚文
        or "𠀀" <= ch <= "𿿿"   # CJK 扩展 B+
    )


def _cjk_stats(text: str) -> tuple[int, int]:
    """返回 (CJK 字符数, 最长连续 CJK 串长度)。

    西文被误读成 GBK 时通常只是零散单字（如 "Grüße"），而真正的中文行有
    连续汉字；要求“至少 2 个连续 CJK 字符”能进一步降低误判。
    """
    count = 0
    run = 0
    best = 0
    for ch in text:
        if _is_cjk_char(ch):
            count += 1
            run += 1
            best = max(best, run)
        else:
            run = 0
    return count, best


def _decode_line(raw: bytes) -> str:
    """单行解码：UTF-8 优先，失败后按 locale/CJK/GB18030 回退。

    回退顺序很关键：locale 可能是 cp1252 这类单字节编码，几乎能“成功”解码
    任意字节；若把它排在 GB18030/GBK 前面，西文 Windows 上的 GBK 输出会变成
    "ÖÐÎÄ" 式乱码。因此仅当 locale 本身是 CJK 编码时才优先使用它。
    """
    text = _try_decode(raw, "utf-8")
    if text is not None:
        return text

    pref = _fallback_encoding()
    if _is_cjk_codec(pref):
        text = _try_decode(raw, pref)
        if text is not None:
            return text

    # 结构判据：GBK/GB18030 的每个 CJK 字符对应 2 个高位字节。
    # 只要求“解码结果含汉字”会误判西文：cp1252 的 "Grüße"（FC DF）也是
    # 合法的 GBK 双字节。要求 高字节数 == 2×CJK 字符数 且至少 2 个字符，
    # 可挡掉 "Grüße"/"Ölöl"/"Schön" 等短西文。
    high = sum(1 for byte in raw if byte >= 0x80)
    if high >= 4:
        text = _try_decode(raw, "gb18030")
        if text is not None:
            cjk, run = _cjk_stats(text)
            if cjk >= 2 and run >= 2 and high == 2 * cjk:
                return text

    text = _try_decode(raw, pref)
    if text is not None:
        return text
    for enc in ("gb18030", "cp1252"):
        text = _try_decode(raw, enc)
        if text is not None:
            return text
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
        body = data[len(codecs.BOM_UTF8):]
        text = _try_decode(body, "utf-8")
        if text is not None:
            return text
        return "\n".join(_decode_line(part) for part in body.split(b"\n"))
    # UTF-32 BOM（FF FE 00 00）以 UTF-16LE BOM 开头，必须先判断。
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return data.decode("utf-32", errors="replace")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        # 用 utf-16 而不是 utf-16-le/be：自动选端序并去掉 BOM。
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return "\n".join(_decode_line(part) for part in data.split(b"\n"))


def _looks_utf16_without_bom(data: bytes) -> str | None:
    """无 BOM 的 UTF-16 探测：NUL 高度集中在奇/偶字节。

    只对 ASCII 主导的内容（JSON/YAML/配置）可靠；纯中文无 BOM 的 UTF-16
    与 GBK 无法可靠区分，不在此支持。带 BOM 的数据交给
    decode_child_output 处理（它会剥离 BOM）。
    """
    if len(data) < 4 or data.startswith((
            codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE,
            codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return None
    if data.count(0) <= len(data) // 4:
        return None
    even_nuls = data[0::2].count(0)
    odd_nuls = data[1::2].count(0)
    if odd_nuls > even_nuls * 4:
        return "utf-16-le"
    if even_nuls > odd_nuls * 4:
        return "utf-16-be"
    return None


def decode_text_file(data: bytes) -> str:
    """解码文本文件（兼容 Windows 编辑器/工具的输出）。

    覆盖 UTF-8（含 BOM）、UTF-16/UTF-32（含 BOM）、无 BOM 的 ASCII 主导
    UTF-16，以及 GBK/本地代码页回退。供角色 home、notes、配置等读取使用；
    写回仍由调用方显式使用 UTF-8。
    """
    if not data:
        return ""
    detected = _looks_utf16_without_bom(data)
    if detected is not None:
        return data.decode(detected, errors="replace")
    return decode_child_output(data)


def read_text_file(path: str | Path) -> str:
    """读取文本文件并自动识别编码；不可读时与 Path.read_text 一样抛 OSError。

    与 Path.read_text 保持 universal-newline 语义：\r\n / \r 归一为 \n。
    否则在 Windows 上“读改写”会把已有的 \r\n 再翻译成 \r\r\n。
    """
    text = decode_text_file(Path(path).read_bytes())
    return text.replace("\r\n", "\n").replace("\r", "\n")


def force_utf8_stdio(env: dict[str, str]) -> dict[str, str]:
    """Windows 上强制 Python 子进程使用 UTF-8 stdio（覆盖继承来的旧值）。

    - PYTHONIOENCODING 优先级高于 PYTHONUTF8，必须显式覆盖；
    - PYTHONLEGACYWINDOWSSTDIO 只要非空就会启用 legacy 控制台 I/O，
      因此删除而不是设为 "0"；
    - 环境变量名在 Windows 上大小写不敏感，先按大小写不敏感删掉同名键，
      避免把重复键传给 CreateProcess。
    """
    if _is_windows():
        for key in [k for k in env
                    if k.upper() in ("PYTHONIOENCODING", "PYTHONUTF8",
                                     "PYTHONLEGACYWINDOWSSTDIO")]:
            env.pop(key, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
    return env


class ConflictError(Exception):
    """状态转换/治理闸门被拒 → HTTP 409。"""
