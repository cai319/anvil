"""pytest 检查适配器：收集基线、junit 解析与红/harness 分类（v1.62 #4）。"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from anvil.sandbox import pytest_adapter as pa


def test_pytest_env_disables_plugin_autoload():
    env = pa.pytest_env()
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_commands_carry_collect_only_junit_and_explicit_plugins():
    collect = pa.collect_argv(["tests/"], ["-q"], ["pytest_timeout"])
    assert "--collect-only" in collect
    assert "tests/" in collect
    assert "-p" in collect and "pytest_timeout" in collect
    run = pa.run_argv(["tests/"], ["-q"], ["pytest_timeout"])
    assert "-p" in run and "pytest_timeout" in run
    assert "--collect-only" not in run
    assert any(arg.startswith("--junit-xml=") for arg in run)


@pytest.mark.parametrize("text,count", [
    ("5 tests collected in 0.01s\n", 5),
    ("1 test collected in 0.00s\n", 1),
    ("no tests ran in 0.01s\n", 0),
    ("garbage output\n", None),
])
def test_parse_collected_count(text, count):
    assert pa.parse_collected_count(text) == count


JUNIT_NOT_IMPLEMENTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_x" name="test_stub">
    <failure message="NotImplementedError: stub">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""

JUNIT_ASSERT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_x" name="test_assert">
    <failure message="assert 1 == 2">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""

JUNIT_VALUE_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
  <testcase classname="tests.test_x" name="test_bad">
    <failure message="ValueError: boom">traceback</failure>
  </testcase>
</testsuite></testsuites>
"""

JUNIT_SETUP_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="0" skipped="0" tests="1">
  <testcase classname="tests.test_x" name="test_bad">
    <error message="fixture failed">traceback</error>
  </testcase>
</testsuite></testsuites>
"""

JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2">
  <testcase classname="tests.test_x" name="test_a"/>
  <testcase classname="tests.test_x" name="test_b"/>
</testsuite></testsuites>
"""


def _classify(junit_text, **kwargs):
    detail = pa.parse_junit_text(junit_text)
    params = {"exit_code": 1, "signal": None, "timed_out": False,
              "junit": detail, "collected_count": detail["tests"],
              "baseline_count": detail["tests"]}
    params.update(kwargs)
    return pa.classify(**params)


def test_assertion_failure_is_valid_red():
    result = _classify(JUNIT_ASSERT)
    assert result["classification"] == "valid_red"
    assert "AssertionError" in result["failure_types"]


def test_not_implemented_stub_is_valid_red():
    result = _classify(JUNIT_NOT_IMPLEMENTED)
    assert result["classification"] == "valid_red"
    assert "NotImplementedError" in result["failure_types"]


def test_passing_exit_zero_is_passed():
    assert _classify(JUNIT_PASS, exit_code=0)["classification"] == "passed"


def test_passing_run_with_shrunken_collection_is_harness_error():
    """锁定的测试数基线被绕过（如 importorskip）时，绿色通过不算数。"""
    result = pa.classify(
        exit_code=0, signal=None, timed_out=False,
        junit=pa.parse_junit_text(JUNIT_PASS),
        collected_count=1, baseline_count=2)
    assert result["classification"] == "harness_error"
    assert result["cause"] == "collected_count_regressed"


def test_passing_run_without_collected_count_or_baseline_stays_passed():
    no_baseline = pa.classify(
        exit_code=0, junit=pa.parse_junit_text(JUNIT_PASS),
        collected_count=2, baseline_count=None)
    assert no_baseline["classification"] == "passed"
    no_collected = pa.classify(
        exit_code=0, junit=pa.parse_junit_text(JUNIT_PASS),
        collected_count=None, baseline_count=None)
    assert no_collected["classification"] == "passed"
    missing_count = pa.classify(
        exit_code=0, junit=pa.parse_junit_text(JUNIT_PASS),
        collected_count=None, baseline_count=2)
    assert missing_count["classification"] == "harness_error"
    assert missing_count["cause"] == "collected_count_missing"


def test_other_exception_type_is_harness_error():
    result = _classify(JUNIT_VALUE_ERROR)
    assert result["classification"] == "harness_error"
    assert result["cause"] == "unexpected_exception"


def test_setup_or_collection_error_element_is_harness():
    result = _classify(JUNIT_SETUP_ERROR)
    assert result["classification"] == "harness_error"
    assert result["cause"] == "test_error"


@pytest.mark.parametrize("exit_code,cause", [
    (2, "collection_error"),
    (3, "internal_error"),
    (4, "usage_error"),
    (5, "no_tests_collected"),
])
def test_pytest_exit_codes_are_harness_errors(exit_code, cause):
    result = _classify(JUNIT_PASS, exit_code=exit_code)
    assert result["classification"] == "harness_error"
    assert result["cause"] == cause


def test_timeout_and_signal_are_harness_errors():
    timeout = pa.classify(exit_code=124, signal=None, timed_out=True,
                          junit=None, collected_count=None, baseline_count=1)
    assert (timeout["classification"], timeout["cause"]) == (
        "harness_error", "timeout")
    signal = pa.classify(exit_code=139, signal="SIGSEGV", timed_out=False,
                         junit=None, collected_count=None, baseline_count=1)
    assert (signal["classification"], signal["cause"]) == (
        "harness_error", "signal")


def test_missing_junit_on_failure_is_harness():
    result = pa.classify(exit_code=1, signal=None, timed_out=False,
                         junit=None, collected_count=None, baseline_count=1)
    assert (result["classification"], result["cause"]) == (
        "harness_error", "missing_junit")


def _run_pytest(tmp_path, body: str):
    (tmp_path / "test_sample.py").write_text(body, encoding="utf-8")
    junit = tmp_path / "junit.xml"
    env = {**os.environ, **pa.pytest_env(), "PYTHONPATH": ""}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    baseline = pa.parse_collected_count(proc.stdout + proc.stderr)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         f"--junit-xml={junit}", "test_sample.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    detail = pa.parse_junit_path(junit)
    return pa.classify(
        exit_code=proc.returncode, signal=None, timed_out=False, junit=detail,
        collected_count=detail["tests"] if detail else None,
        baseline_count=baseline)


def test_real_pytest_stub_not_implemented_is_valid_red(tmp_path):
    result = _run_pytest(
        tmp_path,
        "def test_stub():\n    raise NotImplementedError('not implemented')\n")
    assert result["classification"] == "valid_red"
    assert "NotImplementedError" in result["failure_types"]


def test_real_pytest_assertion_is_valid_red(tmp_path):
    result = _run_pytest(
        tmp_path, "def test_assert():\n    assert 1 == 2\n")
    assert result["classification"] == "valid_red"
    assert "AssertionError" in result["failure_types"]


def test_real_pytest_import_error_is_harness(tmp_path):
    result = _run_pytest(
        tmp_path, "import module_that_does_not_exist_xyz\n")
    assert result["classification"] == "harness_error"
