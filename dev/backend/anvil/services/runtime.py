"""Runtime discovery + registration（design §2.2/§2.4/§2.6）。

存储分层：内置 `anvil/runtimes/*.yaml`（随包）+ 用户 `%APPDATA%/Anvil/runtimes/*.yaml`
（macOS 用 ~/.config/Anvil/runtimes/）；`runtimes` 表只存发现态，真相在 YAML。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import yaml  # type: ignore

from .. import config
from ..util import ConflictError, jdumps, jloads, now_iso


# ---- 定义加载（存储分层） -------------------------------------------------


def load_definitions() -> dict[str, dict]:
    """内置 + 用户目录合并；用户覆盖内置。返回 {id: definition(含 _raw/_source)}。"""
    defs: dict[str, dict] = {}
    for d in (config.PACKAGE_RUNTIMES_DIR, config.USER_RUNTIMES_DIR):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.yaml")):
            raw = p.read_text(encoding="utf-8")
            definition = yaml.safe_load(raw) or {}
            rid = definition.get("id") or p.stem
            definition["_raw"] = raw
            definition["_source"] = str(p)
            defs[rid] = definition
    return defs


def _definition_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---- 发现策略（§2.6 第一层） ----------------------------------------------


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _is_windows() -> bool:
    return os.name == "nt"


def _find_exe_on_path(command: str) -> str | None:
    """Windows: 在 PATH 中查找同名 .exe（可能在 .cmd 之后的目录）。"""
    if not _is_windows() or not command:
        return None
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        directory = raw.strip().strip('"')
        if not directory:
            continue
        candidate = Path(directory) / f"{command}.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _prefer_native_exe(executable: str | None, command: str | None = None) -> str | None:
    """Windows: .cmd/.bat 经 cmd.exe 展开参数，可能破坏 UTF-8 中文；优先原生 .exe。"""
    if not executable or not _is_windows():
        return executable
    p = Path(executable)
    if p.suffix.lower() not in (".cmd", ".bat"):
        return executable
    stem = p.stem

    # 1) 同目录同名 .exe
    sibling = p.with_suffix(".exe")
    if sibling.is_file():
        return str(sibling)
    # 2) PATH 中其它目录的同名 .exe
    found = _find_exe_on_path(command or stem)
    if found:
        return found
    # 3) 常见原生安装位置
    up = os.environ.get("USERPROFILE")
    local = os.environ.get("LOCALAPPDATA")
    candidates: list[Path] = []
    if up:
        home = Path(up)
        candidates += [
            home / ".local" / "bin" / f"{stem}.exe",
            home / ".claude" / "local" / f"{stem}.exe",
        ]
    if local:
        local_app = Path(local)
        candidates += [
            local_app / "Programs" / stem / f"{stem}.exe",
            local_app / "Microsoft" / "WinGet" / "Links" / f"{stem}.exe",
        ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    # 4) npm 全局包内自带的原生 exe
    packages = {
        "claude": ("@anthropic-ai", "claude-code"),
        "codex": ("@openai", "codex"),
        "gemini": ("@google", "gemini-cli"),
    }
    appdata = os.environ.get("APPDATA")
    if appdata and stem in packages:
        scope, package = packages[stem]
        root = Path(appdata) / "npm" / "node_modules" / scope / package
        for candidate in (root / "bin" / f"{stem}.exe", root / f"{stem}.exe"):
            if candidate.is_file():
                return str(candidate)
    return executable


def effective_executable(executable: str | None) -> str | None:
    """对外入口：Windows 下把 .cmd/.bat 升级为原生 .exe；找不到则保持原样。"""
    return _prefer_native_exe(executable)


def _wrapper_warning(executable: str) -> str | None:
    if not _is_windows():
        return None
    if Path(executable).suffix.lower() in (".cmd", ".bat"):
        return (
            "使用 .cmd/.bat wrapper；Windows 下中文参数可能被 cmd.exe 破坏，"
            "建议安装原生 .exe（如 %USERPROFILE%\\.local\\bin\\claude.exe）后重新扫描"
        )
    return None


def _with_wrapper_warning(log: str | None, executable: str) -> str | None:
    warning = _wrapper_warning(executable)
    if not warning:
        return log
    return f"{log}；{warning}" if log else warning


def _resolve_executable(strategies: list[dict]) -> str | None:
    fallback: str | None = None
    for s in strategies or []:
        t = s.get("type")
        command = None
        if t == "path_lookup":
            command = s.get("command", "")
            p = shutil.which(command)
        elif t == "manual":
            p = _expand(s.get("path", ""))
            p = p if (p and os.path.isfile(p)) else None
        elif t == "fixed_path":
            p = next((_expand(c) for c in s.get("candidates", []) if os.path.isfile(_expand(c))), None)
        elif t == "npm_global":
            p = _npm_global(s.get("package", ""))
        else:
            p = None
        if p:
            p = _prefer_native_exe(p, command)
            # Windows: 先把 .cmd/.bat 作为兜底，继续尝试后续策略找原生 .exe。
            if _is_windows() and Path(p).suffix.lower() in (".cmd", ".bat"):
                fallback = fallback or p
                continue
            return p
    return fallback


# 各包管理器查询全局 bin 目录的命令。npm 返回 prefix（Unix 的 bin 在 prefix/bin，
# Windows 的 claude.cmd 直接在 prefix 下）；pnpm/bun 的 bin 子命令直接返回 bin 目录。
_PACKAGE_MANAGER_BIN_ARGS = {
    "npm": ["prefix", "-g"],
    "pnpm": ["bin", "-g"],
    "bun": ["pm", "bin", "-g"],
}


def _package_bin_names(package: str) -> list[str]:
    """包名 → 可能的可执行文件名（claude-code → claude，gemini-cli → gemini）。"""
    last = package.rsplit("/", 1)[-1]
    names = [last]
    for suffix in ("-cli", "-code"):
        if last.endswith(suffix):
            names.append(last[: -len(suffix)])
            break
    return names


def _find_executable(directory: Path, names: list[str]) -> str | None:
    """在目录中查找可执行文件；显式兼容 Windows 的 .exe/.cmd/.bat 布局。"""
    if not directory.is_dir():
        return None
    suffixes = (".exe", "", ".cmd", ".bat") if _is_windows() else ("", ".exe", ".cmd", ".bat")
    for name in names:
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def _npm_global(package: str) -> str | None:
    """尽力定位全局 npm/pnpm/bun 包的可执行文件；命中率低时靠 path_lookup/fixed_path 兜底。"""
    if not package:
        return None
    names = _package_bin_names(package)
    for pm, bin_args in _PACKAGE_MANAGER_BIN_ARGS.items():
        exe = shutil.which(pm)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, *bin_args], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10)
        except Exception:  # noqa: BLE001
            continue
        prefix = (out.stdout or "").strip()
        if not prefix:
            continue
        # 同时检查 prefix 本身（Windows npm 布局）和 prefix/bin（Unix npm 布局）。
        for directory in (Path(prefix), Path(prefix) / "bin"):
            found = _find_executable(directory, names)
            if found:
                return found
    return None


# ---- 版本 / 健康 ----------------------------------------------------------


def _parse_version(text: str, parse: str | None) -> str | None:
    if parse and parse.startswith("regex:"):
        m = re.search(parse[len("regex:"):], text)
        return m.group(1) if m else None
    if text.strip():
        return text.strip().splitlines()[0]
    return None


def _satisfies_range(version: str | None, vr: str | None) -> bool:
    if not vr or not version:
        return True
    m = re.match(r"^(>=|<=|>|<|==)?\s*([\d.]+)$", vr.strip())
    if not m:
        return True  # 无法解析的范围不硬卡
    op, target = m.group(1) or "==", m.group(2)
    try:
        v = tuple(int(x) for x in version.split(".") if x.isdigit())
        t = tuple(int(x) for x in target.split(".") if x.isdigit())
    except ValueError:
        return True
    n = max(len(v), len(t))
    v += (0,) * (n - len(v))
    t += (0,) * (n - len(t))
    return {"==": v == t, ">=": v >= t, "<=": v <= t, ">": v > t, "<": v < t}[op]


def _run_version_probe(executable: str, definition: dict | None) -> tuple[str, str | None, str | None]:
    """返回 (status, version, log)。definition 为 None 时用默认 --version。"""
    executable = effective_executable(executable) or executable
    vp = (definition or {}).get("version_probe") or {}
    args = vp.get("args") or ["--version"]
    try:
        out = subprocess.run([executable, *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15)
    except Exception as exc:  # noqa: BLE001
        return "unhealthy", None, _with_wrapper_warning(str(exc), executable)
    text = (out.stdout or out.stderr) or ""
    version = _parse_version(text, vp.get("parse"))
    if out.returncode == 0 and version:
        if not _satisfies_range(version, (definition or {}).get("version_range")):
            return (
                "unverified", version,
                _with_wrapper_warning(
                    f"version {version} outside {(definition or {}).get('version_range')}", executable),
            )
        return "ready", version, _with_wrapper_warning(None, executable)
    return (
        "unhealthy", version,
        _with_wrapper_warning((text.strip()[:200] or f"exit {out.returncode}"), executable),
    )


def _discovery_log(definition: dict) -> str:
    """记录发现失败时尝试过的策略，供 UI / last_probe_log 诊断。"""
    strategies = (definition.get("discovery") or {}).get("strategies") or []
    tried: list[str] = []
    for s in strategies:
        t = s.get("type")
        if t == "path_lookup":
            tried.append(f"PATH 查找 {s.get('command')!r}")
        elif t == "npm_global":
            tried.append(f"npm 全局包 {s.get('package')!r}")
        elif t == "fixed_path":
            candidates = s.get("candidates") or []
            tried.append("固定路径 " + " / ".join(str(c) for c in candidates))
        elif t == "manual":
            tried.append(f"手动路径 {s.get('path')!r}")
        else:
            tried.append(str(t))
    if not tried:
        return "未找到可执行文件（未配置发现策略）"
    return "未找到可执行文件；已尝试：" + "；".join(tried)


# ---- CRUD / 探针 ----------------------------------------------------------


def register(conn: sqlite3.Connection, *, id: str, display_name: str, executable_path: str,
             detected_version: str | None = None, status: str = "unverified",
             capabilities: dict | None = None) -> dict:
    capabilities = capabilities or {}
    executable_path = effective_executable(executable_path) or executable_path
    conn.execute(
        """INSERT INTO runtimes (id, display_name, executable_path, detected_version,
                                 status, capabilities, enabled)
           VALUES (?,?,?,?,?,?,1)
           ON CONFLICT(id) DO UPDATE SET
             display_name=excluded.display_name,
             executable_path=excluded.executable_path,
             detected_version=excluded.detected_version,
             status=excluded.status,
             capabilities=excluded.capabilities""",
        (id, display_name, executable_path, detected_version, status, jdumps(capabilities)),
    )
    conn.commit()
    return get(conn, id)  # type: ignore[return-value]


def register_definition(conn: sqlite3.Connection, definition: dict,
                        *, raw_yaml: str | None = None) -> dict:
    """手工注册：写 YAML 到用户目录 + 探针 + 落库（§2.6 第二层）。

    raw_yaml 用于保留用户在 UI/文件中写的原始格式与注释；未提供时按 dict 序列化。
    """
    rid = str(definition["id"])
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{1,63}$", rid) or ".." in rid:
        raise ConflictError(f"invalid runtime id: {rid!r}")
    config.USER_RUNTIMES_DIR.mkdir(parents=True, exist_ok=True)
    base = config.USER_RUNTIMES_DIR.resolve()
    yaml_path = (base / f"{rid}.yaml").resolve()
    try:
        yaml_path.relative_to(base)
    except ValueError:
        raise ConflictError(f"invalid runtime id: {rid!r}")
    text = raw_yaml if raw_yaml is not None else yaml.safe_dump(
        definition, allow_unicode=True, sort_keys=False)
    yaml_path.write_text(text, encoding="utf-8")

    executable = definition.get("executable") or definition.get("executable_path")
    strategies = (definition.get("discovery") or {}).get("strategies")
    if not executable and strategies:
        executable = _resolve_executable(strategies)
    executable = effective_executable(executable) or executable
    if executable:
        status, version, log = _run_version_probe(executable, definition)
    else:
        status, version, log = "missing", None, _discovery_log(definition)

    conn.execute(
        """INSERT INTO runtimes (id, display_name, executable_path, detected_version, status,
                                 capabilities, definition_hash, last_probe_at, last_probe_log, enabled)
           VALUES (?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(id) DO UPDATE SET
             display_name=excluded.display_name, executable_path=excluded.executable_path,
             detected_version=excluded.detected_version, status=excluded.status,
             capabilities=excluded.capabilities, definition_hash=excluded.definition_hash,
             last_probe_at=excluded.last_probe_at, last_probe_log=excluded.last_probe_log""",
        (rid, definition.get("display_name", rid), executable or "", version, status,
         jdumps(definition.get("capabilities", {})),
         _definition_hash(yaml_path.read_text(encoding="utf-8")), now_iso(), log),
    )
    conn.commit()
    return get(conn, rid)  # type: ignore[return-value]


def get(conn: sqlite3.Connection, rid: str) -> dict | None:
    r = conn.execute("SELECT * FROM runtimes WHERE id=?", (rid,)).fetchone()
    return _row(r) if r else None


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM runtimes ORDER BY display_name")]


def update(conn: sqlite3.Connection, rid: str, **fields) -> dict | None:
    if not fields:
        return get(conn, rid)
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE runtimes SET {cols} WHERE id=?", (*fields.values(), rid))
    conn.commit()
    return get(conn, rid)


def delete(conn: sqlite3.Connection, rid: str) -> bool:
    cur = conn.execute("DELETE FROM runtimes WHERE id=?", (rid,))
    conn.commit()
    return cur.rowcount > 0


def probe(conn: sqlite3.Connection, rid: str) -> dict | None:
    rt = get(conn, rid)
    if rt is None:
        return None
    definition = load_definitions().get(rid)
    executable = effective_executable(rt["executable_path"]) or rt["executable_path"]
    status, version, log = _run_version_probe(executable, definition)
    conn.execute(
        "UPDATE runtimes SET executable_path=?, detected_version=?, status=?, "
        "last_probe_at=?, last_probe_log=? WHERE id=?",
        (executable, version, status, now_iso(), log, rid),
    )
    conn.commit()
    return get(conn, rid)


def health_probe(conn: sqlite3.Connection, rid: str) -> dict | None:
    """真实健康检查（design §2.4 步骤 3）：在临时目录跑一次最小任务。

    `health.args` 可用 `{{probe_prompt}}` 占位；未配置时默认 `--print <prompt>`。
    会消耗一次极小的模型额度，因此与版本探针分开，由用户显式触发。
    """
    rt = get(conn, rid)
    if rt is None:
        return None
    definition = load_definitions().get(rid) or {}
    health = definition.get("health") or {}
    probe_prompt = health.get("probe_prompt")
    if not probe_prompt:
        return {"id": rid, "ok": None, "status": rt["status"], "detail": "该运行时未定义 health.probe_prompt"}

    args = [str(a).replace("{{probe_prompt}}", probe_prompt) for a in (health.get("args") or ["--print", "{{probe_prompt}}"])]
    timeout = float(health.get("timeout_seconds") or 60)
    expect = health.get("expect_contains") or ""
    executable = effective_executable(rt["executable_path"]) or rt["executable_path"]
    with tempfile.TemporaryDirectory(prefix="anvil-health-") as tmp:
        try:
            proc = subprocess.run(
                [executable, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp
            )
            text = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0 and (expect in text if expect else True)
            log = text.strip()[:500]
        except Exception as exc:  # noqa: BLE001
            ok = False
            log = str(exc)

    status = "ready" if ok else "unhealthy"
    conn.execute(
        "UPDATE runtimes SET status=?, last_probe_at=?, last_probe_log=? WHERE id=?",
        (status, now_iso(), log, rid),
    )
    conn.commit()
    return {"id": rid, "ok": ok, "status": status, "log": log}


def discover(conn: sqlite3.Connection) -> list[dict]:
    """扫描内置+用户定义，按策略定位可执行、探针、落库（§2.4 绑定流程）。"""
    config.ensure_dirs()
    for rid, definition in load_definitions().items():
        executable = _resolve_executable((definition.get("discovery") or {}).get("strategies", []))
        if not executable:
            executable = definition.get("executable") or definition.get("executable_path")
        executable = effective_executable(executable) or executable
        if executable:
            status, version, log = _run_version_probe(executable, definition)
        else:
            status, version, log = "missing", None, _discovery_log(definition)
        conn.execute(
            """INSERT INTO runtimes (id, display_name, executable_path, detected_version, status,
                                     capabilities, definition_hash, last_probe_at, last_probe_log, enabled)
               VALUES (?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(id) DO UPDATE SET
                 display_name=excluded.display_name, executable_path=excluded.executable_path,
                 detected_version=excluded.detected_version, status=excluded.status,
                 capabilities=excluded.capabilities, definition_hash=excluded.definition_hash,
                 last_probe_at=excluded.last_probe_at, last_probe_log=excluded.last_probe_log""",
            (rid, definition.get("display_name", rid), executable or "", version, status,
             jdumps(definition.get("capabilities", {})),
             _definition_hash(definition.get("_raw", "")), now_iso(), log),
        )
    conn.commit()
    return list_all(conn)


def _row(r) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    d["capabilities"] = jloads(d["capabilities"], {})
    return d


# ---- catalog（运行时默认 skill / MCP 发现，M3） ---------------------------


def _skill_description(skill_dir: Path) -> str:
    """读 skill 目录下 SKILL.md 的 description 头，作为一句简介。"""
    md = skill_dir / "SKILL.md"
    if md.is_file():
        try:
            for line in md.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                if line.startswith("description:"):
                    return line.split(":", 1)[1].strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def catalog(conn: sqlite3.Connection, rid: str) -> dict:
    """发现某运行时自身的默认 skill 列表与 MCP server 列表（供角色勾选加载）。

    依据运行时 YAML 的 `catalog` 段：`skills_dir`（扫描每个子目录为一个 skill）、
    `mcp_config` + `mcp_pointer`（读 JSON 配置里的 mcpServers 键）。
    """
    definition = load_definitions().get(rid) or {}
    cat = definition.get("catalog") or {}

    skills: list[dict] = []
    skills_dir = _expand(str(cat.get("skills_dir", "")))
    if skills_dir and os.path.isdir(skills_dir):
        for p in sorted(Path(skills_dir).iterdir()):
            if p.is_dir():
                skills.append({"name": p.name, "description": _skill_description(p)})

    mcp_servers: list[dict] = []
    mcp_config = _expand(str(cat.get("mcp_config", "")))
    mcp_pointer = str(cat.get("mcp_pointer", "mcpServers"))
    if mcp_config and os.path.isfile(mcp_config):
        try:
            with open(mcp_config, encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get(mcp_pointer) or {}
            mcp_servers = [{"name": k} for k in sorted(servers.keys())]
        except Exception:  # noqa: BLE001
            pass

    return {"runtime": rid, "skills": skills, "mcp_servers": mcp_servers}
