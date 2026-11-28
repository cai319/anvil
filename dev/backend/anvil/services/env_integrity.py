"""环境与依赖完整性（v1.62 #7）。

红跑与绿跑共享基础环境哈希（镜像 digest、runner 命令、环境变量）；依赖集单独
记录。测试依赖闭包变化升级人工；生产依赖变化触发在 base 代码 + 新依赖集上的
反事实重跑；依赖 delta 中的 ``pytest11`` entry point 作为评审信号。
"""
from __future__ import annotations

import hashlib
import re
import tomllib
import zipfile
from pathlib import Path

PRODUCTION_REQUIREMENTS_FILES = (
    "requirements.txt", "requirements-prod.txt", "requirements-production.txt",
)
TEST_REQUIREMENTS_FILES = (
    "requirements-test.txt", "requirements-tests.txt",
    "requirements-dev.txt", "tests/requirements.txt",
)
LOCK_FILES = (
    "uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock", "requirements.lock",
)
TEST_GROUP_NAMES = frozenset({"test", "tests", "dev"})

# 依赖完整性信号：这些必须升级人工，不能当作普通通过。
ESCALATION_SIGNALS = frozenset({
    "dependency_test_closure_changed",
    "dependency_counterfactual_passed",
    "dependency_counterfactual_harness_error",
})


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(items) -> list[str]:
    out = {str(item).strip() for item in items or [] if str(item).strip()}
    return sorted(out)


def _requirement_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _pyproject(data: dict) -> tuple[list[str], list[str]]:
    project = data.get("project") or {}
    production = list(project.get("dependencies") or [])
    test: list[str] = []
    optional = project.get("optional-dependencies") or {}
    for name, deps in optional.items():
        if name in TEST_GROUP_NAMES:
            test.extend(deps or [])
        else:
            production.extend(deps or [])
    groups = data.get("dependency-groups") or {}
    for name, deps in groups.items():
        strings = [item for item in deps or [] if isinstance(item, str)]
        if name in TEST_GROUP_NAMES:
            test.extend(strings)
        else:
            production.extend(strings)
    return production, test


def is_dependency_file(path: str) -> bool:
    path = str(path or "").replace("\\", "/").lstrip("./")
    return (path in ("pyproject.toml", *PRODUCTION_REQUIREMENTS_FILES,
                     *TEST_REQUIREMENTS_FILES, *LOCK_FILES))


def _requirement_name(spec: str) -> str:
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", str(spec).strip())
    if not match:
        return str(spec)
    return match.group(1).lower().replace("_", "-").replace(".", "-")


def _delta(locked: list[str], current: list[str]) -> dict:
    locked_by_name = {_requirement_name(spec): spec for spec in locked or []}
    current_by_name = {_requirement_name(spec): spec for spec in current or []}
    return {
        "added": sorted(current_by_name[name] for name in current_by_name
                        if name not in locked_by_name),
        "removed": sorted(locked_by_name[name] for name in locked_by_name
                          if name not in current_by_name),
        "changed": sorted(current_by_name[name] for name in current_by_name
                          if name in locked_by_name
                          and current_by_name[name] != locked_by_name[name]),
    }


def diff(locked: dict, current: dict) -> dict:
    """比较锁定依赖集与当前依赖集；区分生产依赖与测试依赖闭包。"""
    production = _delta((locked or {}).get("production") or [],
                        (current or {}).get("production") or [])
    test = _delta((locked or {}).get("test") or [],
                  (current or {}).get("test") or [])
    production_changed = bool(production["added"] or production["removed"]
                              or production["changed"])
    test_closure_changed = bool(test["added"] or test["removed"]
                                or test["changed"])
    return {
        "production": production,
        "test": test,
        "production_changed": production_changed,
        "test_closure_changed": test_closure_changed,
        "changed": production_changed or test_closure_changed,
    }


def _package_name(spec: str) -> str:
    return _requirement_name(spec)


def _dist_name_to_package(stem: str) -> str:
    """``rich-13.0.0`` / ``rich-13.0.0.dist-info`` → ``rich``。"""
    stem = str(stem or "").strip().rstrip("/")
    if stem.endswith(".dist-info"):
        stem = stem[: -len(".dist-info")]
    return stem.rsplit("-", 1)[0] if "-" in stem else stem


def _parse_entry_points_text(text: str, package: str) -> list[dict]:
    out: list[dict] = []
    group = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
            continue
        if group is None or "=" not in line:
            continue
        plugin, value = (part.strip() for part in line.split("=", 1))
        if plugin and value:
            out.append({"package": package, "group": group,
                        "plugin": plugin, "value": value})
    return out


def entry_points_from_dist_infos(root: Path) -> list[dict]:
    """读取任务环境 site-packages 中已安装发行版的 entry points。"""
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for dist_info in sorted(root.glob("*.dist-info")):
        target = dist_info / "entry_points.txt"
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.extend(_parse_entry_points_text(
            text, _dist_name_to_package(dist_info.name)))
    return out


def entry_points_from_wheels(directory: Path) -> list[dict]:
    """读取本地 wheelhouse 中新增依赖 wheel 的 entry points。

    wheel 是 zip：``<name>-<version>.dist-info/entry_points.txt``。只读任务侧
    产物，不扫描 Anvil 服务器已安装的发行版，避免把服务端插件当成检查环境信号。
    """
    directory = Path(directory)
    out: list[dict] = []
    if not directory.is_dir():
        return out
    for wheel in sorted(directory.glob("*.whl")):
        try:
            with zipfile.ZipFile(wheel) as archive:
                for member in archive.namelist():
                    if not member.endswith(".dist-info/entry_points.txt"):
                        continue
                    package = _dist_name_to_package(member.split("/", 1)[0])
                    text = archive.read(member).decode(
                        "utf-8", errors="replace")
                    out.extend(_parse_entry_points_text(text, package))
        except (OSError, zipfile.BadZipFile):
            continue
    return out


def task_entry_points(workspace: Path) -> list[dict]:
    """任务环境（本地 wheelhouse 或 workspace venv）里的 entry points。"""
    workspace = Path(workspace)
    out: list[dict] = []
    for wheel_dir in (workspace / ".anvil" / "wheelhouse",
                      workspace / "wheelhouse"):
        out.extend(entry_points_from_wheels(wheel_dir))
    for pattern in (".venv/lib/python*/site-packages",
                    ".venv/lib64/python*/site-packages",
                    "venv/lib/python*/site-packages"):
        for site_packages in workspace.glob(pattern):
            out.extend(entry_points_from_dist_infos(site_packages))
    return out


def pytest11_signals(packages, entry_points=None) -> list[dict]:
    """扫描新增依赖中的 ``pytest11`` entry point，作为评审信号。

    ``entry_points`` 必须是任务侧元数据（wheelhouse / 任务环境安装后的
    entry points）；缺省时不扫描 Anvil 服务器自身，返回空信号。
    """
    wanted = {_package_name(str(p)) for p in packages or [] if str(p).strip()}
    if not wanted:
        return []
    points = entry_points or []
    found: dict[tuple[str, str], dict] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        group = str(point.get("group") or "pytest11")
        if group != "pytest11":
            continue
        package = str(point.get("package") or "")
        if _package_name(package) not in wanted:
            continue
        plugin = str(point.get("plugin") or point.get("name") or
                     point.get("value") or "")
        if plugin:
            found[(package, plugin)] = {"package": package, "plugin": plugin}
    return [found[key] for key in sorted(found)]


class EnvironmentMismatch(ValueError):
    """绿跑基础环境与红跑不一致：禁止绿跑（依赖集不参与此比较）。"""


def guard_green_environment(lock: dict, check_id: str, green_spec: dict) -> str:
    """绿跑前校验基础环境哈希必须与红跑一致；不一致抛 EnvironmentMismatch。"""
    from ..sandbox import jobs

    entry = next((c for c in (lock or {}).get("checks") or []
                  if c.get("id") == check_id), None)
    expected = (entry or {}).get("env_hash")
    actual = jobs.environment_hash(green_spec)
    # fail closed：红跑没有记录基础环境哈希（检查不存在或未跑完）时无法证明
    # 同一环境，必须视为不一致并跳过绿跑，让该检查保持 unknown。
    if not expected:
        raise EnvironmentMismatch(
            f"missing red environment hash for {check_id}; "
            "cannot run green checks without a recorded red baseline")
    if actual != expected:
        raise EnvironmentMismatch(
            f"base environment mismatch for {check_id}: "
            f"red={expected} green={actual}")
    return actual


def describe(report: dict) -> str:
    """把依赖完整性报告压成一句可放进评审包证据的说明。"""
    parts: list[str] = []
    for kind in ("production", "test"):
        delta = report.get(kind) or {}
        for action in ("added", "removed", "changed"):
            values = delta.get(action) or []
            if values:
                parts.append(f"{kind} {action}: {', '.join(values)}")
    for check_id, entry in (report.get("counterfactual") or {}).items():
        parts.append(f"counterfactual {check_id}: {entry.get('outcome')}")
    plugins = report.get("pytest11") or []
    if plugins:
        parts.append("pytest11: " + ", ".join(
            f"{item.get('package')}={item.get('plugin')}" for item in plugins))
    return "; ".join(parts) or "no dependency delta"


def snapshot(workspace: Path) -> dict:
    """记录工作区的依赖集与声明文件哈希（锁定/执行/verify 共用）。"""
    workspace = Path(workspace)
    production: list[str] = []
    test: list[str] = []
    files: dict[str, str] = {}

    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        files["pyproject.toml"] = _sha(text)
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        prod, tst = _pyproject(data)
        production.extend(prod)
        test.extend(tst)

    for name in PRODUCTION_REQUIREMENTS_FILES:
        target = workspace / name
        if target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            files[name] = _sha(text)
            production.extend(_requirement_lines(text))

    for name in TEST_REQUIREMENTS_FILES:
        target = workspace / name
        if target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            files[name] = _sha(text)
            test.extend(_requirement_lines(text))

    for name in LOCK_FILES:
        target = workspace / name
        if target.is_file():
            files[name] = _sha(target.read_text(encoding="utf-8", errors="replace"))

    return {
        "production": _clean(production),
        "test": _clean(test),
        "files": dict(sorted(files.items())),
    }
