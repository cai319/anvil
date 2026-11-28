"""切片锁定集合（v1.62 #6）。

scope 锁定时固定检查文件、运行器配置（独立文件 + 配置段）、测试依赖组、
stub 签名与测试数基线。execute / fix 不得修改锁定集合，`scope_paths` 必须与其
不相交。锁定记录随 scope 步骤持久化（`workflow_steps.slice_lock_json`）。
"""
from __future__ import annotations

import ast
import configparser
import hashlib
import json
import tomllib
from fnmatch import fnmatch
from pathlib import Path

from ..util import now_iso

LOCK_VERSION = 1

# 独立运行器配置文件：整文件锁定（改动即拒绝）。
STANDALONE_RUNNER_FILES = ("pytest.ini", "conftest.py")
# 段锁定：只锁定运行器相关段，允许生产依赖 / lockfile 变化。
SECTION_RUNNER_CONFIGS = {
    "pyproject.toml": ("tool.pytest.ini_options",),
    "setup.cfg": ("tool:pytest",),
    "tox.ini": ("pytest",),
}
# 测试依赖组候选。
REQUIREMENTS_FILES = (
    "requirements-test.txt", "requirements-tests.txt",
    "requirements-dev.txt", "tests/requirements.txt",
)


class LockConflict(ValueError):
    """锁定集合冲突；服务端以 409 拒绝 scope 锁定或 execute 提交。"""


# ---- helpers ---------------------------------------------------------------

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel(value) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _has_glob(value: str) -> bool:
    return any(ch in value for ch in "*?[")


def _intersects(a: str, b: str) -> bool:
    """路径模式是否相交：相等、glob 匹配或目录前缀关系。"""
    a, b = _rel(a), _rel(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if fnmatch(b, a) or fnmatch(a, b):
        return True
    if b.startswith(a.rstrip("/") + "/") or a.startswith(b.rstrip("/") + "/"):
        return True
    return False


def _under(path: str, directory: str) -> bool:
    path, directory = _rel(path), _rel(directory)
    return bool(directory) and path.startswith(directory.rstrip("/") + "/")


# ---- test files ------------------------------------------------------------

def _declared_test_paths(spec: dict) -> list[str]:
    paths: list[str] = []
    for item in spec.get("acceptance") or []:
        if item.get("kind") != "test":
            continue
        verification = item.get("verification")
        if not isinstance(verification, dict):
            continue
        pytest_spec = verification.get("pytest")
        if isinstance(pytest_spec, dict):
            paths.extend(_rel(p) for p in pytest_spec.get("paths") or [])
    return paths


def _expand_test_files(workspace: Path, patterns: list[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        target = workspace / pattern
        if target.is_file():
            found.add(pattern)
        elif target.is_dir():
            for child in target.rglob("*"):
                if child.is_file():
                    found.add(_rel(child.relative_to(workspace)))
        else:
            for child in workspace.glob(pattern):
                if child.is_file():
                    found.add(_rel(child.relative_to(workspace)))
    return sorted(found)


def _test_file_match(path: str, lock: dict) -> bool:
    if path in set(lock.get("test_files") or []):
        return True
    for pattern in lock.get("test_patterns") or []:
        if _has_glob(pattern) and fnmatch(path, pattern):
            return True
        if not _has_glob(pattern) and path == pattern:
            return True
    return False


# ---- runner config ---------------------------------------------------------

def _section_hash(path: Path, section: str) -> str | None:
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            value = data
            for part in section.split("."):
                if not isinstance(value, dict) or part not in value:
                    return None
                value = value[part]
            return _sha(_canonical(value))
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        if not parser.has_section(section):
            return None
        return _sha(_canonical(dict(parser.items(section))))
    except (OSError, ValueError, configparser.Error, tomllib.TOMLDecodeError):
        return None


def _runner_config(workspace: Path, test_dirs: list[str]) -> dict:
    files: list[dict] = []
    sections: list[dict] = []
    candidates = list(STANDALONE_RUNNER_FILES)
    for directory in test_dirs:
        candidates.append(_rel(f"{directory}/conftest.py"))
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        target = workspace / name
        if target.is_file():
            files.append({"path": name,
                          "sha256": _sha(target.read_text(encoding="utf-8",
                                                        errors="replace"))})
    for name, section_names in SECTION_RUNNER_CONFIGS.items():
        target = workspace / name
        if not target.is_file():
            continue
        for section in section_names:
            digest = _section_hash(target, section)
            if digest is not None:
                sections.append({"path": name, "section": section,
                                 "sha256": digest})
    files.sort(key=lambda item: item["path"])
    sections.sort(key=lambda item: (item["path"], item["section"]))
    return {"files": files, "sections": sections}


def _test_dependency_groups(workspace: Path) -> list[dict]:
    groups: list[dict] = []
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        dependency_groups = data.get("dependency-groups")
        if isinstance(dependency_groups, dict) and "test" in dependency_groups:
            groups.append({
                "source": "pyproject.toml", "name": "dependency-groups.test",
                "sha256": _sha(_canonical(dependency_groups["test"]))})
        optional = (data.get("project") or {}).get("optional-dependencies")
        if isinstance(optional, dict):
            for name in ("test", "tests", "dev"):
                if name in optional:
                    groups.append({
                        "source": "pyproject.toml",
                        "name": f"project.optional-dependencies.{name}",
                        "sha256": _sha(_canonical(optional[name]))})
    for name in REQUIREMENTS_FILES:
        target = workspace / name
        if target.is_file():
            groups.append({
                "source": name, "name": name,
                "sha256": _sha(target.read_text(encoding="utf-8",
                                                errors="replace"))})
    return groups


# ---- stub signatures -------------------------------------------------------

def _function_signature(node: ast.AST) -> str:
    body = "def"
    if isinstance(node, ast.AsyncFunctionDef):
        body = "async def"
    args = ast.unparse(node.args)
    returns = ast.unparse(node.returns) if node.returns is not None else ""
    decorators = "".join(f"@{ast.unparse(d)}" for d in node.decorator_list)
    return f"{decorators}{body} {node.name}({args})->{returns}"


def _signatures(nodes, prefix: str = "") -> list[str]:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(prefix + _function_signature(node))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            out.append(f"{prefix}class {node.name}({bases})")
            out.extend(_signatures(node.body, prefix=f"{node.name}."))
    return out


def _ast_signature_hash(target: Path) -> str | None:
    try:
        tree = ast.parse(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    return _sha(_canonical(sorted(_signatures(tree.body))))


# ---- build / enforce -------------------------------------------------------

def check_disjoint(lock: dict, execute_paths: list[str]) -> tuple[str, str] | None:
    """返回第一个与锁定路径相交的 (execute 路径, 锁定路径)，没有则 None。"""
    locked = set(lock.get("paths") or []) | set(lock.get("test_patterns") or [])
    for execute_path in execute_paths:
        for locked_path in locked:
            if _intersects(execute_path, locked_path):
                return execute_path, locked_path
    return None


def build(workspace: Path, spec: dict, *,
          execute_paths: list[str] | None = None) -> dict:
    """计算并校验锁定集合；相交时抛 LockConflict。"""
    workspace = Path(workspace)
    test_patterns = sorted(set(_declared_test_paths(spec)))
    test_files = _expand_test_files(workspace, test_patterns)
    test_dirs = sorted({
        _rel(Path(p).parent) for p in [*test_patterns, *test_files]
        if _rel(Path(p).parent) not in ("", ".")
    })
    runner_config = _runner_config(workspace, test_dirs)
    stubs: list[dict] = []
    for stub in spec.get("stubs") or []:
        path = _rel(stub.get("path"))
        target = workspace / path
        stubs.append({
            "path": path,
            "signatures": list(stub.get("signatures") or []),
            "ast_sha256": _ast_signature_hash(target) if target.is_file() else None,
        })
    paths = sorted(set(test_files)
                   | {f["path"] for f in runner_config["files"]}
                   | {p for p in test_patterns if not _has_glob(p)})
    lock = {
        "version": LOCK_VERSION,
        "spec_hash": _sha(_canonical(spec)),
        "locked_at": now_iso(),
        "paths": paths,
        "test_patterns": test_patterns,
        "test_files": test_files,
        "check_dirs": test_dirs,
        "runner_config": runner_config,
        "test_dependency_groups": _test_dependency_groups(workspace),
        "stubs": stubs,
        # 引擎在 base 上 collect 后回填；锁定阶段尚不可知。
        "checks": [],
        "test_count_baseline": None,
    }
    conflict = check_disjoint(lock, list(execute_paths or []))
    if conflict is None:
        # 交付物也不能落在锁定集合里（例如把测试文件当成 execute 交付物）。
        conflict = check_disjoint(lock, list(spec.get("deliverables") or []))
    if conflict is not None:
        execute_path, locked_path = conflict
        raise LockConflict(
            f"{execute_path} intersects locked path {locked_path}")
    return lock


def enforce_changes(lock: dict, workspace: Path, changed: list[str]) -> None:
    """execute / fix 提交时校验改动不触碰锁定集合；违反抛 LockConflict。"""
    workspace = Path(workspace)
    locked_paths = set(lock.get("paths") or [])
    config_files = {f["path"] for f in lock.get("runner_config", {}).get("files") or []}
    sections_by_path: dict[str, list[dict]] = {}
    for section in lock.get("runner_config", {}).get("sections") or []:
        sections_by_path.setdefault(section["path"], []).append(section)
    stubs_by_path = {s["path"]: s for s in lock.get("stubs") or []}
    check_dirs = lock.get("check_dirs") or []
    for raw_path in changed:
        path = _rel(raw_path)
        if not path:
            continue
        if _test_file_match(path, lock):
            raise LockConflict(f"{path} is a locked test file")
        if path in config_files:
            raise LockConflict(f"{path} is a locked runner config file")
        if path in sections_by_path:
            for section in sections_by_path[path]:
                current = _section_hash(workspace / path, section["section"])
                if current != section["sha256"]:
                    raise LockConflict(
                        f"{path} [{section['section']}] is locked runner config")
            continue
        if path in stubs_by_path:
            expected = stubs_by_path[path].get("ast_sha256")
            if expected is not None:
                current = _ast_signature_hash(workspace / path)
                if current != expected:
                    raise LockConflict(f"{path} stub signature changed")
            continue
        if path not in locked_paths and any(_under(path, d) for d in check_dirs):
            raise LockConflict(f"{path} is a new file in locked check directory")


def update_check_baseline(conn, *, workflow_id: str, step_id: str,
                          check_id: str | None, baseline: int | None) -> None:
    """引擎红跑 collect 完成后回填测试数基线；agent 不可调用。"""
    if not workflow_id or not step_id or check_id is None or baseline is None:
        return
    row = conn.execute(
        "SELECT slice_lock_json FROM workflow_steps WHERE workflow_id=? AND step_id=?",
        (workflow_id, step_id)).fetchone()
    if row is None or not row["slice_lock_json"]:
        return
    try:
        lock = json.loads(row["slice_lock_json"])
    except ValueError:
        return
    checks = lock.setdefault("checks", [])
    entry = next((c for c in checks if c.get("id") == check_id), None)
    if entry is None:
        checks.append({"id": check_id, "test_count_baseline": baseline})
    else:
        entry["test_count_baseline"] = baseline
    known = [c["test_count_baseline"] for c in checks
             if c.get("test_count_baseline") is not None]
    lock["test_count_baseline"] = sum(known) if known else None
    conn.execute(
        "UPDATE workflow_steps SET slice_lock_json=? "
        "WHERE workflow_id=? AND step_id=?",
        (json.dumps(lock, ensure_ascii=False), workflow_id, step_id))
