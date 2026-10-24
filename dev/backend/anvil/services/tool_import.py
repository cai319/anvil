"""Skill / MCP 目录与压缩包导入（工具治理）。

导入目标固定为运行时配置：
- Skill：runtime YAML 的 `catalog.skills_dir`，按目录整体复制（保留 scripts/references）。
- MCP：解析 bundle 内的 mcpServers/servers 配置，登记到 Anvil MCP registry；
  角色绑定的运行时会通过 mcp_config_for 把 registry 合并进工作区配置。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .. import config
from ..util import ConflictError
from . import runtime as runtime_service
from . import tooling as tooling_service

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
MAX_ARCHIVE_MEMBERS = 10000
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_rel(name: str) -> Path:
    raw = str(name or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError("invalid upload path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"unsafe upload path: {name}")
    return Path(*parts)


def _safe_name(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]", "-", str(name or "")).strip(".-")
    if not text or not _SAFE_NAME.fullmatch(text):
        raise ValueError(f"invalid tool name: {name!r}")
    return text


def _materialize(uploads: list[tuple[str, bytes]], paths: list[str],
                 work: Path, fallback_name: str) -> tuple[Path, str]:
    """把上传的目录/压缩包展开到 work/src，返回 (root, bundle_name)。"""
    if not uploads:
        raise ValueError("no files uploaded")
    total = sum(len(data) for _name, data in uploads)
    if total > MAX_UPLOAD_BYTES:
        raise ValueError("upload is too large")
    root = work / "src"
    root.mkdir(parents=True, exist_ok=True)
    first_name = uploads[0][0].lower()
    if len(uploads) == 1 and first_name.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        archive = work / "bundle.archive"
        archive.write_bytes(uploads[0][1])
        bundle = _safe_name(Path(archive and uploads[0][0]).stem.rsplit(".tar", 1)[0]
                            or fallback_name)
        if first_name.endswith(".zip"):
            _extract_zip(archive, root)
        else:
            _extract_tar(archive, root)
        return root, bundle

    if len(uploads) > MAX_FILES:
        raise ValueError("too many files")
    rels: list[str] = []
    for idx, (filename, data) in enumerate(uploads):
        rel = paths[idx] if idx < len(paths) else filename
        dest = root / _safe_rel(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        rels.append(rel)
    top = rels[0].replace("\\", "/").split("/", 1)[0] if rels else fallback_name
    bundle = _safe_name(Path(top).stem) if top else fallback_name
    return root, bundle


def _extract_zip(archive: Path, root: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        if len(zf.infolist()) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("archive has too many entries")
        total = 0
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            rel = _safe_rel(info.filename)
            if info.is_dir():
                (root / rel).mkdir(parents=True, exist_ok=True)
                continue
            total += int(info.file_size or 0)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("archive is too large after extraction")
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_tar(archive: Path, root: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("archive has too many entries")
        total = 0
        for member in members:
            if member.issym() or member.islnk():
                continue
            rel = _safe_rel(member.name)
            if member.isdir():
                (root / rel).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            total += int(member.size or 0)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("archive is too large after extraction")
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _skill_description(skill_dir: Path) -> str:
    md = skill_dir / "SKILL.md"
    try:
        for line in md.read_text(encoding="utf-8", errors="replace").splitlines()[:30]:
            if line.lower().startswith("description:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _find_skill_dirs(root: Path, bundle_name: str) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    if (root / "SKILL.md").is_file():
        return [(root, bundle_name)]
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".") and depth < 5
        ]
        if "SKILL.md" in filenames:
            found.append((current, current.name))
            dirnames[:] = []
    return found


def _replace_dir(src: Path, dest: Path) -> None:
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{dest.name}.importing"
    backup = parent / f".{dest.name}.backup"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if dest.exists():
        dest.rename(backup)
    staging.rename(dest)
    shutil.rmtree(backup, ignore_errors=True)


def _runtime_catalog(runtime_id: str) -> dict:
    definition = runtime_service.load_definitions().get(runtime_id) or {}
    return definition.get("catalog") or {}


def import_skills(conn, runtime_id: str, uploads: list[tuple[str, bytes]],
                  paths: list[str] | None = None, *, overwrite: bool = False) -> dict:
    if runtime_id not in runtime_service.load_definitions():
        raise ValueError(f"运行时 {runtime_id} 不存在或不可用")
    work = Path(tempfile.mkdtemp(prefix="anvil-skill-import-"))
    try:
        root, bundle = _materialize(uploads, paths or [], work, "imported-skill")
        skill_dirs = _find_skill_dirs(root, bundle)
        if not skill_dirs:
            raise ValueError("未找到包含 SKILL.md 的 skill 目录")
        skills_dir = os.path.expanduser(str(_runtime_catalog(runtime_id).get("skills_dir") or ""))
        if not skills_dir:
            raise ValueError(f"运行时 {runtime_id} 未配置 catalog.skills_dir")
        target_root = Path(skills_dir)
        target_root.mkdir(parents=True, exist_ok=True)
        imported = []
        skipped = []
        for src, raw_name in skill_dirs:
            try:
                name = _safe_name(raw_name)
            except ValueError as exc:
                skipped.append({"name": raw_name, "reason": str(exc)})
                continue
            dest = target_root / name
            existed = dest.exists()
            if existed and not overwrite:
                skipped.append({"name": name, "reason": "同名 skill 已存在"})
                continue
            _replace_dir(src, dest)
            imported.append({
                "name": name,
                "description": _skill_description(dest),
                "path": str(dest),
                "files": sum(1 for f in dest.rglob("*") if f.is_file()),
                "replaced": existed,
            })
        if not imported:
            raise ConflictError("没有可导入的 skill（全部同名或名称非法）")
        return {"runtime": runtime_id, "imported": imported, "skipped": skipped}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _find_mcp_configs(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.rglob("*.json")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = data.get("servers")
        if isinstance(servers, dict) and servers:
            found.append((path, servers))
    return found


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and (
        text.startswith(("./", "../", "~/")) or "/" in text or "\\" in text
    )


def _uses_relative_root(cfg: dict, base: Path) -> bool:
    values = [cfg.get("command"), cfg.get("cwd"), *(cfg.get("args") or [])]
    for value in values:
        if not isinstance(value, str) or not value or value.startswith("-"):
            continue
        if value.startswith(("./", "../", "~/")):
            return True
        if not value.startswith("/") and (base / value).exists():
            return True
    return False


def _rewrite_value(value: Any, base: Path, installed_base: Path) -> Any:
    if not isinstance(value, str) or not value or value.startswith("-"):
        return value
    if value.startswith("~/"):
        return value
    if value.startswith(("./", "../")):
        rel = value[2:] if value.startswith("./") else value
        candidate = (base / rel)
        if candidate.exists():
            return str((installed_base / rel).resolve())
        return value
    if not value.startswith("/") and (base / value).exists():
        return str((installed_base / value).resolve())
    return value


def _missing_relative(cfg: dict, base: Path) -> str | None:
    """检查 bundle 里声明的相对脚本是否存在，避免导入后指向已删除的临时目录。"""
    pathy_suffixes = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".rb", ".jar"}
    values = [cfg.get("command"), cfg.get("cwd"), *(cfg.get("args") or [])]
    for value in values:
        if not isinstance(value, str) or not value or value.startswith("-"):
            continue
        if value.startswith(("./", "../")):
            rel = value[2:] if value.startswith("./") else value
            if not (base / rel).exists():
                return value
        elif not value.startswith("/") and Path(value).suffix.lower() in pathy_suffixes:
            if not (base / value).exists():
                return value
    return None


def _install_bundle(root: Path, bundle: str, destination: Path,
                    overwrite: bool) -> tuple[Path, bool]:
    if destination.exists():
        if overwrite:
            _replace_dir(root, destination)
            return destination, True
        return destination, False
    _replace_dir(root, destination)
    return destination, True


def _normalize_transport(cfg: dict) -> str:
    transport = str(cfg.get("type") or cfg.get("transport") or "").strip().lower()
    if transport in ("streamable-http", "streamable_http"):
        transport = "http"
    if transport not in ("stdio", "http", "sse"):
        transport = "http" if cfg.get("url") else "stdio"
    return transport


def import_mcp(conn, runtime_id: str, uploads: list[tuple[str, bytes]],
               paths: list[str] | None = None, *, overwrite: bool = False) -> dict:
    if runtime_id not in runtime_service.load_definitions():
        raise ValueError(f"运行时 {runtime_id} 不存在或不可用")
    work = Path(tempfile.mkdtemp(prefix="anvil-mcp-import-"))
    try:
        root, bundle = _materialize(uploads, paths or [], work, "imported-mcp")
        configs = _find_mcp_configs(root)
        if not configs:
            raise ValueError("未找到 mcpServers 或 servers 配置")
        merged: dict[str, tuple[dict, Path]] = {}
        for config_path, servers in configs:
            for name, cfg in servers.items():
                if isinstance(cfg, dict):
                    merged[str(name)] = (cfg, config_path.parent)
        install_base: Path | None = None
        if any(_uses_relative_root(cfg, base) for cfg, base in merged.values()):
            destination = config.DATA_DIR / "imported_mcp" / bundle
            install_base, _created = _install_bundle(root, bundle, destination, overwrite)
        imported = []
        skipped = []
        for name, (cfg, base) in sorted(merged.items()):
            try:
                safe = _safe_name(name)
            except ValueError as exc:
                skipped.append({"name": name, "reason": str(exc)})
                continue
            transport = _normalize_transport(cfg)
            missing = _missing_relative(cfg, base)
            if missing:
                skipped.append({"name": safe, "reason": f"bundle 缺少文件 {missing}"})
                continue
            command = cfg.get("command")
            args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
            cwd = cfg.get("cwd")
            url = cfg.get("url")
            if install_base is not None:
                installed_base = install_base / base.relative_to(root)
                command = _rewrite_value(command, base, installed_base)
                cwd = _rewrite_value(cwd, base, installed_base)
                args = [_rewrite_value(a, base, installed_base) for a in args]
            if transport == "stdio" and not command:
                skipped.append({"name": safe, "reason": "stdio MCP 缺少 command"})
                continue
            if transport != "stdio" and not url:
                skipped.append({"name": safe, "reason": f"{transport} MCP 缺少 url"})
                continue
            env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
            description = str(cfg.get("description") or cfg.get("title") or "").strip()
            existing = tooling_service.get_mcp_server(conn, safe)
            if existing and not overwrite:
                skipped.append({"name": safe, "reason": "同名 MCP 已存在"})
                continue
            if existing:
                server = tooling_service.update_mcp_server(
                    conn, safe, description=description, runtime=runtime_id,
                    transport=transport, command=command,
                    args=[str(a) for a in args],
                    env={str(k): str(v) for k, v in env.items()},
                    url=url)
            else:
                server = tooling_service.create_mcp_server(
                    conn, name=safe, description=description, runtime=runtime_id,
                    transport=transport, command=command,
                    args=[str(a) for a in args],
                    env={str(k): str(v) for k, v in env.items()}, url=url)
            imported.append({
                "name": safe,
                "description": server.get("description") if server else description,
                "transport": transport,
                "command": command,
                "tools": len((server or {}).get("tools") or []),
            })
        if not imported:
            raise ConflictError("没有可导入的 MCP server（全部同名或配置不完整）")
        return {"runtime": runtime_id, "imported": imported, "skipped": skipped}
    finally:
        shutil.rmtree(work, ignore_errors=True)
