"""受控 PoC/动态验证作业（M2a）。

职责：spec 校验/归一化、只读快照、job.sh 生成、执行、证据与产物收集。
本模块不写 DB；DB 写入由 ``services.sandbox`` 的 worker 统一完成。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal as signal_mod
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import config
from ..util import jloads, now_iso
from . import check_kinds
from . import pytest_adapter

PHASES = ("prepare", "build", "run")
DEFAULT_RESOURCES = {
    "timeout_seconds": 3600,
    "rss_mib": 8192,
    "pids": 512,
    "scratch_mib": 10240,
    "artifact_mib": 200,
}
HARD_MAX = {
    "timeout_seconds": 14400,
    "rss_mib": 16384,
    "pids": 1024,
    "scratch_mib": 20480,
    "artifact_mib": 500,
}
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,200}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_ENV = {
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV",
}
_MIB = 1024 * 1024


class JobSpecError(ValueError):
    """非法作业 spec；API 返回 400。"""


class JobExecutionError(RuntimeError):
    """执行阶段无法继续（快照/基础设施）；worker 标 failed。"""


# ---- spec ------------------------------------------------------------------

def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(canonical(spec).encode("utf-8")).hexdigest()


def _int(value: Any, default: int, *, minimum: int, maximum: int, field_: str) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise JobSpecError(f"{field_} must be an integer") from None
    if n < minimum or n > maximum:
        raise JobSpecError(f"{field_} must be between {minimum} and {maximum}")
    return n


def _normalize_command(raw: Any, *, phase: str, index: int,
                       job_timeout: int) -> dict:
    if not isinstance(raw, dict):
        raise JobSpecError(f"{phase}[{index}] must be an object")
    name = str(raw.get("name") or f"{phase}-{index + 1}")
    if not _NAME_RE.match(name):
        raise JobSpecError(f"{phase}[{index}] name must match {_NAME_RE.pattern}")
    cmd = raw.get("cmd")
    argv = raw.get("argv")
    if bool(cmd) == bool(argv):
        raise JobSpecError(f"{phase}[{index}] requires exactly one of cmd/argv")
    if cmd is not None:
        if not isinstance(cmd, str) or len(cmd) > 32768 or not cmd.strip():
            raise JobSpecError(f"{phase}[{index}] cmd must be a non-empty string <=32KB")
        argv = None
    else:
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(a, str) for a in argv)):
            raise JobSpecError(f"{phase}[{index}] argv must be a non-empty string list")
        cmd = None
    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise JobSpecError(f"{phase}[{index}] env must be an object")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        key = str(key)
        if not _ENV_KEY_RE.match(key):
            raise JobSpecError(f"{phase}[{index}] env key {key!r} is invalid")
        if key.startswith("ANVIL_") or key in _FORBIDDEN_ENV:
            raise JobSpecError(f"{phase}[{index}] env key {key!r} is forbidden")
        env[key] = str(value)
    timeout = _int(raw.get("timeout_seconds"), min(job_timeout, 600),
                   minimum=1, maximum=job_timeout,
                   field_=f"{phase}[{index}].timeout_seconds")
    return {"name": name, "cmd": cmd, "argv": argv,
            "timeout_seconds": timeout, "env": env}


def parse_spec(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise JobSpecError("spec must be an object")
    task_id = str(raw.get("task_id") or "").strip() or None
    finding_id = str(raw.get("finding_id") or "").strip() or None
    network = str(raw.get("network") or "none").strip().lower()
    if network not in ("none", "bridge"):
        raise JobSpecError("network must be none or bridge")
    image = raw.get("image")
    if image is not None:
        image = str(image).strip()
        if not _IMAGE_RE.match(image):
            raise JobSpecError("image must be a docker image reference")
    resources_raw = raw.get("resources") or {}
    if not isinstance(resources_raw, dict):
        raise JobSpecError("resources must be an object")
    resources = {
        key: _int(resources_raw.get(key), default, minimum=1,
                  maximum=HARD_MAX[key], field_=f"resources.{key}")
        for key, default in DEFAULT_RESOURCES.items()
    }
    phases_raw = raw.get("phases") or {}
    if not isinstance(phases_raw, dict):
        raise JobSpecError("phases must be an object")
    unknown = set(phases_raw) - set(PHASES)
    if unknown:
        raise JobSpecError(f"unknown phase(s): {sorted(unknown)}")
    phases: dict[str, list[dict]] = {}
    total = 0
    for phase in PHASES:
        items = phases_raw.get(phase) or []
        if not isinstance(items, list) or len(items) > 8:
            raise JobSpecError(f"phases.{phase} must be a list of <=8 commands")
        total += len(items)
        phases[phase] = [
            _normalize_command(item, phase=phase, index=i,
                               job_timeout=resources["timeout_seconds"])
            for i, item in enumerate(items)
        ]
    if total == 0:
        raise JobSpecError("at least one command is required")
    if not phases["run"]:
        raise JobSpecError("at least one run command is required")

    expect_raw = raw.get("expect") or {}
    if not isinstance(expect_raw, dict):
        raise JobSpecError("expect must be an object")
    unknown = set(expect_raw) - {"phase", "exit_code", "signal", "stdout_contains"}
    if unknown:
        raise JobSpecError(f"unknown expect key(s): {sorted(unknown)}")
    expect: dict[str, Any] = {}
    if expect_raw:
        phase = str(expect_raw.get("phase") or "run")
        if phase not in PHASES:
            raise JobSpecError("expect.phase is invalid")
        expect["phase"] = phase
        if expect_raw.get("exit_code") is not None:
            expect["exit_code"] = int(expect_raw["exit_code"])
        if expect_raw.get("signal"):
            expect["signal"] = str(expect_raw["signal"])
        if expect_raw.get("stdout_contains"):
            text = str(expect_raw["stdout_contains"])
            if len(text) > 4096:
                raise JobSpecError("expect.stdout_contains is too long")
            expect["stdout_contains"] = text
        if not any(k in expect for k in ("exit_code", "signal", "stdout_contains")):
            raise JobSpecError("expect requires exit_code, signal or stdout_contains")

    artifacts_raw = raw.get("artifacts") or []
    if not isinstance(artifacts_raw, list):
        raise JobSpecError("artifacts must be a list")
    artifacts: list[dict] = []
    for i, item in enumerate(artifacts_raw):
        if not isinstance(item, dict) or not item.get("path"):
            raise JobSpecError(f"artifacts[{i}] requires path")
        path = str(item["path"]).replace("\\", "/")
        if path.startswith("/") or ".." in path.split("/") or re.match(r"^[A-Za-z]:", path):
            raise JobSpecError(f"artifacts[{i}].path must be relative without ..")
        max_bytes = _int(item.get("max_bytes"), resources["artifact_mib"] * _MIB,
                         minimum=1, maximum=resources["artifact_mib"] * _MIB,
                         field_=f"artifacts[{i}].max_bytes")
        artifacts.append({"path": path, "max_bytes": max_bytes})

    spec = {
        "task_id": task_id,
        "finding_id": finding_id,
        "source": "task_workspace",
        "network": network,
        "image": image,
        "resources": resources,
        "phases": phases,
        "expect": expect,
        "artifacts": artifacts,
        "reason": str(raw.get("reason") or "")[:2000],
    }
    spec["profile"] = "safe" if is_safe(spec) else "relaxed"
    requested = str(raw.get("profile") or "").strip().lower()
    if requested:
        if requested not in ("safe", "relaxed"):
            raise JobSpecError("profile must be safe or relaxed")
        if requested == "safe" and not is_safe(spec):
            raise JobSpecError(
                "safe profile requires network=none, default image and default limits")
        spec["profile"] = requested
    return spec


def is_safe(spec: dict) -> bool:
    if spec.get("network") != "none" or spec.get("image"):
        return False
    resources = spec.get("resources") or {}
    return all(
        int(resources.get(key) or 0) <= DEFAULT_RESOURCES[key]
        for key in DEFAULT_RESOURCES
    )


# ---- check spec（引擎执行的验收检查）--------------------------------------

CHECK_KINDS = ("test", "command", "file_assert", "manual")


def _relative_check_path(raw: Any, field_: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise JobSpecError(f"{field_} must be a non-empty relative path")
    path = raw.strip().replace("\\", "/")
    if (path.startswith("/") or ".." in path.split("/")
            or re.match(r"^[A-Za-z]:", path)):
        raise JobSpecError(f"{field_} must be relative without ..")
    return path


def _red_exit_code(raw: Any) -> int:
    """command 检查声明的断言失败退出码；未声明或非法一律拒绝。"""
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 255:
        raise JobSpecError(
            "check.exit_code must be an integer between 1 and 255 "
            "for kind=command (the assertion-failure exit code)")
    return raw


def _normalize_junit_paths(raw: Any, field_: str = "check.junit") -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise JobSpecError(f"{field_} must be a list of paths")
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise JobSpecError(f"{field_}[{i}] must be a non-empty string")
        path = item.replace("\\", "/")
        if (path.startswith("/") or ".." in path.split("/")
                or re.match(r"^[A-Za-z]:", path)):
            raise JobSpecError(f"{field_}[{i}] must be relative without ..")
        out.append(path)
    return out


def _normalize_pytest_config(raw: Any) -> dict:
    """校验 pytest 检查的运行配置：源码相对路径 + 透传参数 + 显式插件。"""
    if not isinstance(raw, dict):
        raise JobSpecError("check.pytest must be an object")
    unknown = set(raw) - {"paths", "args", "plugins"}
    if unknown:
        raise JobSpecError(f"check.pytest has unknown key(s): {sorted(unknown)}")
    paths = _normalize_junit_paths(raw.get("paths"), "check.pytest.paths")
    if not paths:
        raise JobSpecError("check.pytest.paths must not be empty")

    def _strings(value: Any, field_: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value):
            raise JobSpecError(f"{field_} must be a list of non-empty strings")
        return [item.strip() for item in value]

    return {"paths": paths,
            "args": _strings(raw.get("args"), "check.pytest.args"),
            "plugins": _strings(raw.get("plugins"), "check.pytest.plugins")}


def _normalize_install(raw: Any) -> dict | None:
    """校验生产依赖安装声明；依赖集单独记录，不进入基础环境哈希。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise JobSpecError("check.install must be an object")
    unknown = set(raw) - {"requirements", "offline"}
    if unknown:
        raise JobSpecError(f"check.install has unknown key(s): {sorted(unknown)}")
    requirements_raw = raw.get("requirements")
    if not isinstance(requirements_raw, list) or not requirements_raw:
        raise JobSpecError("check.install.requirements must be a non-empty list")
    requirements: list[str] = []
    for i, item in enumerate(requirements_raw):
        if not isinstance(item, str) or not item.strip():
            raise JobSpecError(
                f"check.install.requirements[{i}] must be a non-empty string")
        requirements.append(item.strip())
    offline = bool(raw.get("offline", False))
    return {
        "requirements": sorted(set(requirements)),
        "offline": offline,
        # 默认需要网络；离线（本地 wheelhouse）安装无需人工审批。
        "network_required": not offline,
    }


def _install_command(install: dict) -> str:
    argv = ["python3", "-m", "pip", "install", "--disable-pip-version-check"]
    if install.get("offline"):
        argv.append("--no-index")
    argv.extend(install["requirements"])
    return " ".join(shlex.quote(str(a)) for a in argv)


def parse_check_spec(raw: Any) -> dict:
    """校验一条检查 spec；检查作业必须绑定任务工作区，不要求 finding。"""
    if not isinstance(raw, dict):
        raise JobSpecError("spec must be an object")
    task_id = str(raw.get("task_id") or "").strip()
    if not task_id:
        raise JobSpecError(
            "check jobs must be bound to a task workspace (task_id)")
    check_raw = raw.get("check")
    if not isinstance(check_raw, dict):
        raise JobSpecError("check must be an object")
    check_kind = str(check_raw.get("kind") or "test").strip().lower()
    if check_kind not in CHECK_KINDS:
        raise JobSpecError(f"check.kind must be one of {CHECK_KINDS}")
    network = str(raw.get("network") or "none").strip().lower()
    if network not in ("none", "bridge"):
        raise JobSpecError("network must be none or bridge")
    image = raw.get("image")
    if image is not None:
        image = str(image).strip()
        if not _IMAGE_RE.match(image):
            raise JobSpecError("image must be a docker image reference")
    resources_raw = raw.get("resources") or {}
    if not isinstance(resources_raw, dict):
        raise JobSpecError("resources must be an object")
    resources = {
        key: _int(resources_raw.get(key), default, minimum=1,
                  maximum=HARD_MAX[key], field_=f"resources.{key}")
        for key, default in DEFAULT_RESOURCES.items()
    }
    check_id = str(check_raw.get("id") or "check")
    junit = _normalize_junit_paths(check_raw.get("junit"))
    install = _normalize_install(check_raw.get("install"))
    pytest_spec = None
    collect_cmd = None
    red_exit_code = None
    file_assert: dict[str, Any] | None = None
    run_commands: list[dict] = []
    if check_kind == "manual":
        raise JobSpecError(
            "manual checks are not executed by the engine; "
            "record them in the slice spec for the decide phase")
    if check_kind == "file_assert":
        if (check_raw.get("pytest") is not None
                or check_raw.get("cmd") is not None
                or check_raw.get("argv") is not None):
            raise JobSpecError(
                "check.file_assert is engine-native; it cannot carry cmd/argv/pytest")
        path = _relative_check_path(check_raw.get("path"), "check.path")
        contains = check_raw.get("contains")
        if contains is not None and (not isinstance(contains, str)
                                     or not contains.strip()):
            raise JobSpecError("check.contains must be a non-empty string")
        file_assert = {
            "path": path,
            "contains": contains.strip() if isinstance(contains, str) else None,
        }
        junit = []
    elif check_raw.get("pytest") is not None:
        if check_kind != "test":
            raise JobSpecError("check.pytest is only valid for kind=test")
        if check_raw.get("cmd") is not None or check_raw.get("argv") is not None:
            raise JobSpecError(
                "check.pytest cannot be combined with check.cmd/check.argv")
        pytest_spec = _normalize_pytest_config(check_raw["pytest"])
        if not junit:
            junit = ["junit.xml"]
        env = {**dict(check_raw.get("env") or {}),
               **pytest_adapter.pytest_env()}
        collect_text = " ".join(shlex.quote(a) for a in pytest_adapter.collect_argv(
            pytest_spec["paths"], pytest_spec["args"], pytest_spec["plugins"]))
        if len(collect_text) > 32768:
            raise JobSpecError("check.pytest collect command is too long")
        collect_cmd = collect_text + " > /out/collect.txt 2>&1"
        collect = _normalize_command(
            {"name": f"{check_id}.collect", "cmd": collect_cmd,
             "timeout_seconds": check_raw.get("timeout_seconds"), "env": env},
            phase="run", index=0, job_timeout=resources["timeout_seconds"])
        run_argv = pytest_adapter.run_argv(
            pytest_spec["paths"], pytest_spec["args"], pytest_spec["plugins"],
            junit[0])
        command = _normalize_command(
            {"name": check_id, "argv": run_argv,
             "timeout_seconds": check_raw.get("timeout_seconds"), "env": env},
            phase="run", index=1, job_timeout=resources["timeout_seconds"])
        command_cmd = " ".join(shlex.quote(a) for a in run_argv)
        command_argv = None
        run_commands = [collect, command]
    else:
        if check_kind == "command":
            red_exit_code = _red_exit_code(check_raw.get("exit_code"))
        command_raw = {
            "name": check_id,
            "timeout_seconds": check_raw.get("timeout_seconds"),
            "env": check_raw.get("env") or {},
        }
        if check_raw.get("cmd") is not None:
            command_raw["cmd"] = check_raw["cmd"]
        if check_raw.get("argv") is not None:
            command_raw["argv"] = check_raw["argv"]
        command = _normalize_command(
            command_raw, phase="run", index=0,
            job_timeout=resources["timeout_seconds"])
        command_cmd = command["cmd"]
        command_argv = command["argv"]
        run_commands = [command]
    artifacts = [
        {"path": path, "max_bytes": resources["artifact_mib"] * _MIB}
        for path in junit
    ]
    if file_assert is not None:
        check_spec: dict[str, Any] = {
            "id": check_id,
            "kind": check_kind,
            "path": file_assert["path"],
            "contains": file_assert["contains"],
            "junit": [],
        }
    else:
        check_spec = {
            "id": run_commands[-1]["name"],
            "kind": check_kind,
            "cmd": command_cmd,
            "argv": command_argv,
            "timeout_seconds": run_commands[-1]["timeout_seconds"],
            "env": run_commands[-1]["env"],
            "junit": junit,
        }
        if red_exit_code is not None:
            check_spec["red_exit_code"] = red_exit_code
    if pytest_spec is not None:
        check_spec["pytest"] = pytest_spec
        check_spec["collect_cmd"] = collect_cmd
    prepare = []
    if install is not None:
        check_spec["install"] = install
        install_cmd = _install_command(install)
        prepare.append(_normalize_command(
            {"name": f"{check_id}.install", "cmd": install_cmd,
             "timeout_seconds": check_raw.get("timeout_seconds"),
             "env": dict(check_raw.get("env") or {})},
            phase="prepare", index=0,
            job_timeout=resources["timeout_seconds"]))
    spec: dict[str, Any] = {
        "kind": "check",
        "task_id": task_id,
        "finding_id": None,
        "source": "task_workspace",
        # 生产检查默认标准档；bridge/自定义镜像按 ADR-0003 人工批准。
        "tier": "standard",
        "network": network,
        "image": image,
        "resources": resources,
        "check": check_spec,
        "phases": {"prepare": prepare, "build": [], "run": run_commands},
        "expect": {},
        "artifacts": artifacts,
        "reason": str(raw.get("reason") or "")[:2000],
    }
    # 引擎红跑作业回填锁定集合的测试数基线；由服务端写入，不参与环境哈希。
    lock_ref = raw.get("lock_ref")
    if isinstance(lock_ref, dict):
        ref = {key: str(lock_ref[key]) for key in ("workflow_id", "step_id")
               if lock_ref.get(key)}
        if ref:
            spec["lock_ref"] = ref
    # 反事实重跑：在保存的 base 代码快照上执行同一检查。
    base_snapshot = raw.get("base_snapshot")
    if base_snapshot is not None:
        base_snapshot = str(base_snapshot)
        if not base_snapshot.strip() or not Path(base_snapshot).is_absolute():
            raise JobSpecError("base_snapshot must be an absolute path")
        spec["base_snapshot"] = base_snapshot
    counterfactual = raw.get("counterfactual")
    if counterfactual is not None:
        if not isinstance(counterfactual, dict):
            raise JobSpecError("counterfactual must be an object")
        spec["counterfactual"] = {
            key: str(counterfactual[key])
            for key in ("workflow_id", "slice_key", "check_id",
                        "dependency_digest")
            if counterfactual.get(key)
        }
    spec["profile"] = "safe" if is_safe(spec) else "relaxed"
    requested = str(raw.get("profile") or "").strip().lower()
    if requested:
        if requested not in ("safe", "relaxed"):
            raise JobSpecError("profile must be safe or relaxed")
        if requested == "safe" and not is_safe(spec):
            raise JobSpecError(
                "safe profile requires network=none, default image and default limits")
        spec["profile"] = requested
    return spec


def environment_parts(spec: dict) -> dict:
    """环境拆分：基础环境（镜像/网络/tier/runner 命令/环境变量）。

    依赖集不属于基础环境，单独由 :func:`dependency_digest` 记录；红跑与绿跑必须
    基础哈希一致，但允许依赖集变化（变化时走反事实重跑）。
    """
    check = spec.get("check") or {}
    base = {
        "tier": spec.get("tier"),
        "image": spec.get("image") or config.SANDBOX_IMAGE,
        "network": spec.get("network"),
        "kind": check.get("kind"),
        "cmd": check.get("cmd"),
        "argv": check.get("argv"),
        "pytest": check.get("pytest"),
        "red_exit_code": check.get("red_exit_code"),
        "path": check.get("path"),
        "contains": check.get("contains"),
        "env": check.get("env") or {},
    }
    return {
        "base": base,
        "base_hash": "sha256:" + hashlib.sha256(
            canonical(base).encode("utf-8")).hexdigest(),
    }


def environment_hash(spec: dict) -> str:
    """基础环境哈希（不含依赖集）。"""
    return environment_parts(spec)["base_hash"]


def dependency_digest(spec: dict) -> str:
    """依赖集哈希：安装声明（requirements）单独记录，不进基础环境哈希。"""
    install = (spec.get("check") or {}).get("install") or {}
    requirements = sorted(set(install.get("requirements") or []))
    return "sha256:" + hashlib.sha256(
        canonical(requirements).encode("utf-8")).hexdigest()


# ---- junit -----------------------------------------------------------------

def _junit_int(node, attr: str) -> int:
    try:
        return int(float(node.get(attr) or 0))
    except (TypeError, ValueError):
        return 0


_JUNIT_MAX_BYTES = 5 * 1024 * 1024


def parse_junit(path: Path) -> dict | None:
    """解析单个 junit xml（testsuites 或 testsuite 根）；不可解析返回 None。"""
    try:
        if path.stat().st_size > _JUNIT_MAX_BYTES:
            return None
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = [child for child in root if child.tag == "testsuite"]
    else:
        return None
    summary = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in summary:
            summary[key] += _junit_int(suite, key)
    return summary


def junit_evidence(artifact_dir: Path,
                   rel_paths: list[str]) -> tuple[dict | None, int | None]:
    """汇总已声明的 junit 文件；返回 (summary, collected_count)。"""
    total: dict[str, int] | None = None
    for rel in rel_paths or []:
        summary = parse_junit(Path(artifact_dir) / rel)
        if summary is None:
            continue
        if total is None:
            total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for key in total:
            total[key] += summary[key]
    return (total, total["tests"]) if total is not None else (None, None)


def junit_detail_evidence(artifact_dir: Path,
                          rel_paths: list[str]) -> dict | None:
    """汇总 junit 明细（含失败测试的异常类型），用于红/harness 分类。"""
    total: dict | None = None
    for rel in rel_paths or []:
        detail = pytest_adapter.parse_junit_path(Path(artifact_dir) / rel)
        if detail is None:
            continue
        if total is None:
            total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
                     "failure_types": []}
        for key in ("tests", "failures", "errors", "skipped"):
            total[key] += int(detail.get(key) or 0)
        total["failure_types"].extend(detail.get("failure_types") or [])
    return total


# ---- script ----------------------------------------------------------------

def _body(cmd: dict) -> str:
    if cmd.get("argv"):
        text = " ".join(shlex.quote(str(a)) for a in cmd["argv"])
    else:
        text = str(cmd["cmd"])
    exports = "".join(
        f"export {key}={shlex.quote(value)}; "
        for key, value in (cmd.get("env") or {}).items())
    return exports + text


RUN_STEP = r'''
run_step() {
  local phase="$1"; local name="$2"; local timeout_s="$3"; local body="$4"
  local start end rc
  start=$(date +%s%N)
  set +e
  timeout --signal=TERM --kill-after=5s "$timeout_s" bash -lc "$body"
  rc=$?
  set -e
  end=$(date +%s%N)
  printf '{"phase":"%s","name":"%s","exit_code":%s,"duration_ms":%s}\n' \
    "$phase" "$name" "$rc" "$(( (end - start) / 1000000 ))" >> /out/phases.jsonl
  if [ "$rc" -ne 0 ] && [ "$phase" != "run" ]; then
    exit "$rc"
  fi
}
'''


def build_script(spec: dict) -> str:
    lines = [
        "set -euo pipefail",
        'export HOME=/scratch/home',
        'mkdir -p "$HOME" "$HOME/tmp" /out',
        'export TMPDIR="$HOME/tmp"',
        "cd /scratch",
        "rm -rf /scratch/work",
        "mkdir -p /scratch/work",
        # 容器内以 uid 1000 运行，不能 preserve 宿主 uid（否则非 root cp 失败）。
        "cp -a --no-preserve=ownership /src/. /scratch/work/",
        "cd /scratch/work",
        "rm -f /out/phases.jsonl",
        RUN_STEP,
    ]
    for phase in PHASES:
        for cmd in spec["phases"][phase]:
            lines.append(
                f"run_step {shlex.quote(phase)} {shlex.quote(cmd['name'])} "
                f"{int(cmd['timeout_seconds'])} {shlex.quote(_body(cmd))}")
    lines += ["touch /out/.done", "exit 0"]
    return "\n".join(lines) + "\n"


# ---- runner protocol -------------------------------------------------------

@dataclass
class JobPaths:
    root: Path
    source: Path
    script: Path
    logs: Path
    artifacts: Path
    scratch: Path


@dataclass
class RunnerResult:
    exit_code: int | None = None
    timed_out: bool = False
    signal: str | None = None
    image: str | None = None
    backend: str = "unknown"
    error: str | None = None


class JobRunner(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        ...

    def run(self, *, spec: dict, paths: JobPaths, job_id: str,
            log_path: Path) -> RunnerResult:
        ...


@dataclass
class JobExecution:
    status: str
    exit_code: int | None
    signal: str | None
    failure_reason: str | None
    evidence: dict
    phases: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    log_dir: str | None = None
    artifact_dir: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


# ---- execution -------------------------------------------------------------

def container_name(job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job_id))[:48]
    return f"anvil-poc-{safe}"


def job_paths(job_id: str) -> JobPaths:
    root = config.SANDBOX_JOBS_DIR / job_id
    return JobPaths(
        root=root,
        source=root / "src",
        script=root / "job" / "job.sh",
        logs=root / "logs",
        artifacts=root / "out",
        scratch=root / "scratch",
    )


def _prepare_paths(job_id: str) -> JobPaths:
    paths = job_paths(job_id)
    for p in (paths.root, paths.script.parent, paths.logs,
              paths.artifacts, paths.scratch):
        p.mkdir(parents=True, exist_ok=True)
    try:
        paths.artifacts.chmod(0o777)
        paths.scratch.chmod(0o777)
    except OSError:
        pass
    return paths


def _tree_hash(root: Path) -> str:
    """快照哈希：不跟随 symlink、不整读超大文件。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root)).replace("\\", "/")
        if path.is_symlink():
            digest.update(rel.encode() + b"\0symlink\0")
            try:
                digest.update(os.readlink(path).encode())
            except OSError:
                pass
            digest.update(b"\0")
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        digest.update(rel.encode() + b"\0" + str(stat.st_size).encode() + b"\0")
        if stat.st_size <= 50 * 1024 * 1024:
            digest.update(path.read_bytes())
        else:
            digest.update(str(stat.st_mtime_ns).encode())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _snapshot(source: Path | None, dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    if source is not None and source.exists():
        shutil.copytree(
            source, dest, dirs_exist_ok=True,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", ".anvil", "node_modules", ".venv", "__pycache__"))
    # 容器以 uid 1000 运行；确保宿主快照对非 root 可读（不改变已有可执行位）。
    for path in dest.rglob("*"):
        try:
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(mode | (0o555 if path.is_dir() else 0o444))
        except OSError:
            pass
    return _tree_hash(dest)


def _signal_name(code: int | None) -> str | None:
    if code is None or code <= 128:
        return None
    try:
        return signal_mod.Signals(code - 128).name
    except ValueError:
        return f"SIG{code - 128}"


def parse_phases(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for seq, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        exit_code = ev.get("exit_code")
        try:
            exit_code = int(exit_code)
        except (TypeError, ValueError):
            exit_code = None
        out.append({
            "seq": seq,
            "phase": str(ev.get("phase") or ""),
            "name": str(ev.get("name") or ""),
            "exit_code": exit_code,
            "signal": _signal_name(exit_code),
            "timed_out": exit_code == 124,
            "duration_ms": ev.get("duration_ms"),
            "status": "succeeded" if exit_code == 0 else "failed",
        })
    return out


def _selected(rel: str, patterns: list[dict]) -> dict | None:
    for item in patterns:
        if fnmatch.fnmatchcase(rel, item["path"]):
            return item
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_artifacts(paths: JobPaths, spec: dict) -> list[dict]:
    patterns = spec.get("artifacts") or []
    if not patterns:
        return []
    total = 0
    total_limit = int(spec["resources"]["artifact_mib"]) * _MIB
    out: list[dict] = []
    for path in sorted(paths.artifacts.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(paths.artifacts)).replace("\\", "/")
        if rel in ("phases.jsonl", ".done"):
            continue
        matched = _selected(rel, patterns)
        if matched is None:
            continue
        size = path.stat().st_size
        if size > int(matched["max_bytes"]):
            raise JobExecutionError(
                f"artifact {rel} exceeds its max_bytes limit")
        total += size
        if total > total_limit:
            raise JobExecutionError(
                f"artifacts exceed {spec['resources']['artifact_mib']} MiB")
        out.append({"rel_path": rel, "size_bytes": size,
                    "sha256": _sha256_file(path)})
    return out


def _verify(spec: dict, phases: list[dict], log_text: str,
            result: RunnerResult) -> tuple[bool, str | None]:
    if result.timed_out:
        return False, "sandbox_timeout"
    if result.error:
        return False, "sandbox_error"
    if not phases:
        return False, "sandbox_missing_phases"
    for phase in phases:
        if phase["phase"] in ("prepare", "build") and phase["exit_code"] != 0:
            return False, "sandbox_build_failed"
    expect = spec.get("expect") or {}
    run_phases = [p for p in phases if p["phase"] == "run"]
    if expect:
        target = [p for p in phases if p["phase"] == expect.get("phase", "run")]
        checks: list[bool] = []
        if "exit_code" in expect:
            checks.append(any(p["exit_code"] == int(expect["exit_code"]) for p in target))
        if "signal" in expect:
            checks.append(any(p["signal"] == str(expect["signal"]) for p in target))
        if "stdout_contains" in expect:
            checks.append(str(expect["stdout_contains"]) in log_text)
        return (all(checks), None if all(checks) else "sandbox_expectation_failed")
    ok = bool(run_phases) and all(p["exit_code"] == 0 for p in run_phases)
    return ok, None if ok else "sandbox_run_failed"


_CHECK_FAILURE = {
    "failed": "check_failed",
    "signal": "check_signal",
    "timeout": "check_timeout",
    "error": "check_error",
}


def _check_status(phases: list[dict], result: RunnerResult) -> str:
    """把检查结局分为 passed/failed/signal/timeout/error，区分超时与信号。"""
    if result.error or not phases:
        return "error"
    runs = [p for p in phases if p["phase"] == "run"]
    if not runs:
        return "error"
    phase = runs[-1]
    if result.timed_out or phase.get("timed_out"):
        return "timeout"
    if phase.get("signal"):
        return "signal"
    if phase.get("exit_code") == 0:
        return "passed"
    return "failed"


def _command_text(check: dict) -> str:
    if check.get("argv"):
        return " ".join(shlex.quote(str(a)) for a in check["argv"])
    return str(check.get("cmd") or "")


def execute_job(spec: dict, job_id: str, runner: JobRunner, *,
                source_dir: Path | None = None,
                check: bool = False) -> JobExecution:
    check = bool(check or spec.get("kind") == "check")
    started = now_iso()
    paths = _prepare_paths(job_id)
    log_path = paths.logs / "container.log"
    source = None
    if source_dir is not None:
        source = Path(source_dir)
    elif spec.get("base_snapshot"):
        source = Path(str(spec["base_snapshot"]))
    elif spec.get("task_id"):
        source = config.TASK_WORKSPACES_DIR / str(spec["task_id"])
    try:
        tree_hash = _snapshot(source, paths.source)
    except OSError as exc:
        raise JobExecutionError(f"snapshot failed: {exc}") from exc
    script = build_script(spec)
    paths.script.write_text(script, encoding="utf-8")
    try:
        paths.script.chmod(0o644)
    except OSError:
        pass

    check_spec_early = spec.get("check") or {}
    file_assert_eval: dict | None = None
    if check and check_spec_early.get("kind") == "file_assert":
        # file_assert 是引擎原生检查：不启动容器命令，直接在工作区快照上判定。
        file_assert_eval = check_kinds.evaluate_file_assert(
            paths.source, str(check_spec_early.get("path") or ""),
            check_spec_early.get("contains"))
        exit_code = 0 if file_assert_eval["passed"] else 1
        phases = [{
            "seq": 0, "phase": "run",
            "name": str(check_spec_early.get("id") or "file_assert"),
            "exit_code": exit_code, "signal": None, "timed_out": False,
            "duration_ms": 0,
            "status": "succeeded" if exit_code == 0 else "failed",
        }]
        (paths.logs / "phases.json").write_text(
            json.dumps(phases, ensure_ascii=False, indent=2), encoding="utf-8")
        result = RunnerResult(exit_code=exit_code, backend="engine")
    else:
        result = runner.run(
            spec=spec, paths=paths, job_id=job_id, log_path=log_path)
        phases = parse_phases(paths.artifacts / "phases.jsonl")
        if phases:
            (paths.logs / "phases.json").write_text(
                json.dumps(phases, ensure_ascii=False, indent=2),
                encoding="utf-8")
    log_text = ""
    try:
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")[-2_000_000:]
    except OSError:
        pass
    check_status = _check_status(phases, result) if check else None
    if check:
        verified = check_status == "passed"
        failure = _CHECK_FAILURE.get(check_status or "error")
    else:
        verified, failure = _verify(spec, phases, log_text, result)
    # 失败作业也要保留产物（崩溃现场/最小复现输入）。
    try:
        artifacts = collect_artifacts(paths, spec)
    except JobExecutionError as exc:
        verified, failure = False, failure or "sandbox_artifact_limit"
        artifacts = []
        (paths.logs / "error.txt").write_text(str(exc), encoding="utf-8")
    signal = next((p["signal"] for p in phases if p["signal"]), result.signal)
    evidence = {
        "job_id": job_id,
        "spec_hash": spec_hash(spec),
        "backend": result.backend,
        "image": result.image,
        "network": spec.get("network"),
        "source_tree_hash": tree_hash,
        "phases": phases,
        "artifacts": artifacts,
        "expect": spec.get("expect") or {},
        "verified": verified,
        "runner": {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "signal": result.signal,
            "error": result.error,
        },
        "started_at": started,
        "finished_at": now_iso(),
    }
    if check:
        check_spec = spec.get("check") or {}
        summary, collected = junit_evidence(
            paths.artifacts, list(check_spec.get("junit") or []))
        run_phase = next(
            (p for p in reversed(phases) if p["phase"] == "run"), None)
        phase_exit = run_phase.get("exit_code") if run_phase else None
        detail = junit_detail_evidence(
            paths.artifacts, list(check_spec.get("junit") or []))
        baseline = None
        collect_path = paths.artifacts / "collect.txt"
        if check_spec.get("pytest") is not None and collect_path.exists():
            try:
                baseline = pytest_adapter.parse_collected_count(
                    collect_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                baseline = None
        result_info = {
            "classification": None, "cause": None, "failure_types": [],
        }
        if check_spec.get("kind") == "test":
            result_info = pytest_adapter.classify(
                exit_code=(phase_exit if phase_exit is not None
                           else result.exit_code),
                signal=(run_phase or {}).get("signal") or result.signal,
                timed_out=bool(result.timed_out
                               or (run_phase or {}).get("timed_out")),
                junit=detail,
                collected_count=collected,
                baseline_count=baseline,
            )
        elif check_spec.get("kind") == "command":
            result_info = check_kinds.classify_command(
                exit_code=(phase_exit if phase_exit is not None
                           else result.exit_code),
                red_exit_code=check_spec.get("red_exit_code"),
                signal=(run_phase or {}).get("signal") or result.signal,
                timed_out=bool(result.timed_out
                               or (run_phase or {}).get("timed_out")),
            )
        elif check_spec.get("kind") == "file_assert":
            result_info = {
                "classification": (file_assert_eval or {}).get("classification"),
                "cause": (file_assert_eval or {}).get("cause"),
                "failure_types": [],
            }
        evidence["check"] = {
            "run_id": job_id,
            "check_id": check_spec.get("id"),
            "kind": check_spec.get("kind"),
            "command": _command_text(check_spec),
            "exit_code": phase_exit if phase_exit is not None else result.exit_code,
            "signal": (run_phase or {}).get("signal") or result.signal,
            "timed_out": bool(result.timed_out
                              or (run_phase or {}).get("timed_out")),
            "junit_summary": summary,
            "collected_count": collected,
            "collected_baseline": baseline,
            "classification": result_info["classification"],
            "cause": result_info["cause"],
            "failure_types": result_info["failure_types"],
            "env_hash": environment_hash(spec),
            "dependency_digest": dependency_digest(spec),
            "duration_ms": (run_phase or {}).get("duration_ms"),
            "status": check_status,
        }
        if check_spec.get("red_exit_code") is not None:
            evidence["check"]["red_exit_code"] = check_spec["red_exit_code"]
        if check_spec.get("kind") == "file_assert":
            evidence["check"]["path"] = check_spec.get("path")
            evidence["check"]["contains"] = check_spec.get("contains")
    status = "succeeded" if verified else "failed"
    # 快照/scratch 不入库；日志/产物/证据留在 job 目录。
    shutil.rmtree(paths.source, ignore_errors=True)
    shutil.rmtree(paths.scratch, ignore_errors=True)
    return JobExecution(
        status=status,
        exit_code=result.exit_code,
        signal=signal,
        failure_reason=None if verified else (failure or "sandbox_failed"),
        evidence=evidence,
        phases=phases,
        artifacts=artifacts,
        log_dir=str(paths.logs),
        artifact_dir=str(paths.artifacts),
        started_at=started,
        finished_at=now_iso(),
    )


def execute_check_job(spec: dict, job_id: str, runner: JobRunner, *,
                      source_dir: Path | None = None) -> JobExecution:
    """执行一条检查；复用 PoC 的快照/脚本/产物/证据管线。"""
    return execute_job(spec, job_id, runner, source_dir=source_dir, check=True)
