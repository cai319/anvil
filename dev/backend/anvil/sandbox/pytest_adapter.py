"""pytest 检查适配器：收集基线、junit 解析与合法红/harness 分类（v1.62 #4）。

纯函数：不读写 DB、不启动进程。沙箱作业管线（``sandbox.jobs``）负责把这里的
命令、环境与分类结果写进验收证据。只有 ``AssertionError`` / ``NotImplementedError``
算合法红；导入/收集/用法错误、超时、信号与其他异常都算 harness 错误。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

PYTHON = "python3"
PLUGIN_AUTOLOAD_ENV = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"
VALID_RED_TYPES = frozenset({"AssertionError", "NotImplementedError"})
_JUNIT_MAX_BYTES = 5 * 1024 * 1024

_COLLECTED_RE = re.compile(r"(\d+)\s+(?:tests?|items?)\s+collected", re.IGNORECASE)
_NO_TESTS_RE = re.compile(r"no tests ran", re.IGNORECASE)
_EXC_NAME_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Warning)")


def pytest_env() -> dict[str, str]:
    """pytest 运行时环境：禁用第三方插件自动加载，插件必须显式 ``-p``。"""
    return {PLUGIN_AUTOLOAD_ENV: "1"}


def _plugin_args(plugins) -> list[str]:
    out: list[str] = []
    for plugin in plugins or []:
        out.extend(["-p", str(plugin)])
    return out


def collect_argv(paths, args=None, plugins=None) -> list[str]:
    """``--collect-only`` 基线命令（退出码 5 = 未收集到测试 = harness）。"""
    return [PYTHON, "-m", "pytest", "--collect-only", "-q",
            *_plugin_args(plugins), *(args or []), *(paths or [])]


def run_argv(paths, args=None, plugins=None,
             junit_path: str = "junit.xml") -> list[str]:
    return [PYTHON, "-m", "pytest", "-q",
            f"--junit-xml=/out/{junit_path}",
            *_plugin_args(plugins), *(args or []), *(paths or [])]


def parse_collected_count(text: str) -> int | None:
    """从 ``pytest --collect-only -q`` 输出解析收集到的测试数。"""
    if not text:
        return None
    matches = _COLLECTED_RE.findall(text)
    if matches:
        return int(matches[-1])
    if _NO_TESTS_RE.search(text):
        return 0
    return None


def _failure_type(element) -> str:
    """junit ``<failure>`` 的异常类型；纯 assert 失败按 AssertionError 计。"""
    declared = (element.get("type") or "").strip()
    if declared:
        return declared.split(".")[-1]
    message = (element.get("message") or "").strip()
    head = message.split(":", 1)[0].strip()
    if head and _EXC_NAME_RE.match(head):
        return head.split(".")[-1]
    text = (element.text or "")
    for line in reversed(text.splitlines()):
        candidate = line.strip().lstrip("E").strip()
        if candidate and _EXC_NAME_RE.match(candidate.split(":", 1)[0].strip()):
            return candidate.split(":", 1)[0].strip().split(".")[-1]
    return "AssertionError"


def parse_junit_text(text: str) -> dict | None:
    """解析 junit XML 文本；返回汇总 + 失败测试的异常类型。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = [child for child in root if child.tag == "testsuite"]
    else:
        return None

    def _int(node, attr: str) -> int:
        try:
            return int(float(node.get(attr) or 0))
        except (TypeError, ValueError):
            return 0

    summary = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failure_types: list[str] = []
    for suite in suites:
        for key in summary:
            summary[key] += _int(suite, key)
        for case in suite.iter("testcase"):
            for child in case:
                if child.tag == "failure":
                    failure_types.append(_failure_type(child))
    summary["failure_types"] = failure_types
    return summary


def parse_junit_path(path: Path) -> dict | None:
    try:
        if Path(path).stat().st_size > _JUNIT_MAX_BYTES:
            return None
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_junit_text(text)


def _result(classification: str, cause: str | None = None,
            failure_types: list[str] | None = None) -> dict:
    return {"classification": classification, "cause": cause,
            "failure_types": failure_types or []}


def classify(*, exit_code: int | None, signal: str | None = None,
             timed_out: bool = False, junit: dict | None = None,
             collected_count: int | None = None,
             baseline_count: int | None = None) -> dict:
    """按 pytest 语义分类一次检查结局。"""
    if timed_out:
        return _result("harness_error", "timeout")
    if signal:
        return _result("harness_error", "signal")
    if exit_code is None:
        return _result("harness_error", "missing_exit_code")
    if exit_code == 0:
        # 绿色通过也必须守住锁定的测试数基线：收集数变少（如 importorskip、
        # 删测试或 skip 整模块）意味着这次通过的不是锁定的检查。
        if baseline_count is not None:
            if collected_count is None:
                return _result("harness_error", "collected_count_missing")
            if collected_count < baseline_count:
                return _result("harness_error", "collected_count_regressed")
        return _result("passed")
    if exit_code == 5:
        return _result("harness_error", "no_tests_collected")
    if exit_code == 4:
        return _result("harness_error", "usage_error")
    if exit_code == 2:
        return _result("harness_error", "collection_error")
    if exit_code == 3:
        return _result("harness_error", "internal_error")
    if exit_code != 1:
        return _result("harness_error", f"exit_code_{exit_code}")
    if junit is None:
        return _result("harness_error", "missing_junit")
    if int(junit.get("errors") or 0) > 0:
        return _result("harness_error", "test_error")
    failures = list(junit.get("failure_types") or [])
    if int(junit.get("failures") or 0) > 0 and not failures:
        # 有失败但 junit 缺少 testcase 明细：无法确认是断言失败。
        return _result("harness_error", "missing_failure_detail")
    if not failures:
        return _result("harness_error", "failed_without_failures")
    unexpected = [t for t in failures if t not in VALID_RED_TYPES]
    if unexpected:
        return _result("harness_error", "unexpected_exception",
                       failure_types=failures)
    return _result("valid_red", failure_types=failures)
