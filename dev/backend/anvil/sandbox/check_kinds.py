"""command / file_assert 检查的纯函数（v1.62 #5）。

command 检查按 spec 声明的"断言失败退出码"分类：0 为通过，声明值算合法红，
其余退出码、超时与信号都是 harness 错误。file_assert 由引擎原生实现，直接在
工作区快照上判断文件是否存在、内容是否匹配，不退化为 shell 命令。

missing 文件属于"平凡红"（trivial red）：它判红，但不计入有效红/合格检查。
"""
from __future__ import annotations

from pathlib import Path

_MAX_READ_BYTES = 5 * 1024 * 1024


def _result(classification: str, cause: str | None = None) -> dict:
    return {"classification": classification, "cause": cause,
            "failure_types": []}


def classify_command(*, exit_code: int | None, red_exit_code: int | None,
                     signal: str | None = None,
                     timed_out: bool = False) -> dict:
    """按 command 检查语义分类一次运行结局。"""
    if timed_out:
        return _result("harness_error", "timeout")
    if signal:
        return _result("harness_error", "signal")
    if exit_code is None:
        return _result("harness_error", "missing_exit_code")
    if exit_code == 0:
        return _result("passed")
    if red_exit_code is not None and exit_code == red_exit_code:
        return _result("valid_red")
    return _result("harness_error", "unexpected_exit_code")


def evaluate_file_assert(root, path: str, contains: str | None = None) -> dict:
    """引擎原生 file_assert：文件缺失/内容不符判红，匹配判绿。

    返回 ``classification`` / ``cause`` / ``passed``；缺失文件是 ``trivial_red``，
    内容不符是 ``valid_red``，匹配是 ``passed``。读取失败按 harness 错误处理。
    """
    base = Path(root).resolve()
    target = (base / path).resolve()
    if base != target and base not in target.parents:
        return {**_result("harness_error", "path_escape"), "passed": False}
    if not target.exists():
        return {**_result("trivial_red", "file_missing"), "passed": False}
    if not target.is_file():
        return {**_result("trivial_red", "not_a_file"), "passed": False}
    if contains:
        try:
            if target.stat().st_size > _MAX_READ_BYTES:
                return {**_result("harness_error", "file_too_large"),
                        "passed": False}
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {**_result("harness_error", "read_error"), "passed": False}
        if contains not in text:
            return {**_result("valid_red", "content_mismatch"), "passed": False}
    return {**_result("passed"), "passed": True}
