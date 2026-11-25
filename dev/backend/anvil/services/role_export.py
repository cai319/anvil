"""Role@version export/import (v1.50 P5, spec RSI-VER-010/011/EXP-*)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath

from ..util import jdumps, jloads, new_id, now_iso
from ..version import VERSION
from . import role_space, roles

MAX_PACKAGE_BYTES = 20 * 1024 * 1024
EXPORT_SCHEMA = 1


class ImportError_(ValueError):
    pass


def _safe_member(name: str) -> str:
    norm = name.replace("\\", "/")
    path = PurePosixPath(norm)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ImportError_(f"unsafe zip member: {name}")
    return str(path)


def _read_zip(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ImportError_("package exceeds size limit")
    with zipfile.ZipFile(path) as zf:
        names = [_safe_member(n) for n in zf.namelist() if not n.endswith("/")]
        if len(set(names)) != len(names):
            raise ImportError_("duplicate zip members")
        total = 0
        out: dict[str, bytes] = {}
        for name in names:
            data = zf.read(name)
            total += len(data)
            if total > MAX_PACKAGE_BYTES:
                raise ImportError_("uncompressed package exceeds size limit")
            out[name] = data
    return out


def export_version(conn: sqlite3.Connection, slug: str, version: int,
                   *, target_dir: Path | None = None) -> Path:
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?",
        (slug, version)).fetchone()
    if row is None:
        raise LookupError(f"version not found: {slug}@{version}")
    if row["kind"] != "evolution":
        raise ValueError("only evolution versions can be exported")
    manifest = role_space.get_manifest(conn, slug, version)
    verified = role_space.verify_version(conn, slug, version)
    if not verified.get("ok"):
        raise ValueError(f"version is corrupt: {verified.get('reason')}")
    role = conn.execute(
        "SELECT display_name, definition_json FROM roles WHERE slug=?", (slug,)).fetchone()
    tools = conn.execute(
        "SELECT preset, max_tool_tokens, allowlist_json, skills_json, mcp_json "
        "FROM role_tools WHERE role_slug=?", (slug,)).fetchone()
    export = {
        "schema_version": EXPORT_SCHEMA,
        "anvil_version": VERSION,
        "exported_at": now_iso(),
        "slug": slug,
        "display_name": role["display_name"] if role else slug,
        "version": version,
        "kind": row["kind"],
        "source": row["source"],
        "content_hash": row["content_hash"],
        "manifest_hash": row["space_manifest_hash"],
        "charter_digest": row["charter_digest"],
        "eval": ({"run_id": row["eval_run_id"]} if row["eval_run_id"] else None),
        "actual_performance": role_space.actual_performance(conn, slug, version),
        "imported_from": None,
    }
    charter = {
        "definition": jloads(role["definition_json"] if role else "{}", {}),
        "tools": dict(tools) if tools else {},
    }
    short = (row["content_hash"] or "nohash").removeprefix("sha256:")[:8]
    name = f"role-{slug}-v{version}-{short}.anvil.zip"
    target_dir = Path(target_dir) if target_dir else Path.cwd() / "exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    cas = role_space.cas_dir(slug)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("export.json",
                    json.dumps(export, ensure_ascii=False, indent=2))
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("manifest.sha256", (manifest["manifest_hash"] + "\n").encode())
        zf.writestr("charter.json",
                    json.dumps(charter, ensure_ascii=False, indent=2))
        for item in manifest.get("files", []):
            data = role_space._read_blob(cas, item)
            zf.writestr(f"files/{item['path']}", data)
        zf.writestr("README.md", _readme(export, manifest))
    return target


def _readme(export: dict, manifest: dict) -> str:
    return f"""# {export['slug']} @ v{export['version']}

- content_hash: {export['content_hash']}
- manifest_hash: {export['manifest_hash']}
- files: {len(manifest.get('files', []))}
- knowledge cards: {len(manifest.get('knowledge_cards', []))}
- source: {export['source']}

导入方式：`anvil role import <file> --mode new_role --slug <new>` 或
`--mode add_version --slug <existing>`。导入不会自动切换/启用。
"""


def preview_import(conn: sqlite3.Connection, path: Path) -> dict:
    files = _read_zip(Path(path))
    if "export.json" not in files or "manifest.json" not in files:
        raise ImportError_("export.json/manifest.json are required")
    export = json.loads(files["export.json"].decode("utf-8"))
    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    if int(export.get("schema_version") or 0) != EXPORT_SCHEMA:
        raise ImportError_("unsupported export schema")
    expected = manifest.get("manifest_hash")
    if not expected or role_space._manifest_hash(manifest) != expected:
        raise ImportError_("manifest hash mismatch")
    for item in manifest.get("files", []):
        member = f"files/{item['path']}"
        if member not in files:
            raise ImportError_(f"missing file in package: {item['path']}")
        data = files[member]
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ImportError_(f"file hash mismatch: {item['path']}")
        if len(data) != int(item.get("size") or len(data)):
            raise ImportError_(f"file size mismatch: {item['path']}")
    charter = json.loads(files.get("charter.json", b"{}").decode("utf-8"))
    runtime_id = ((charter.get("definition") or {}).get("runtime") or {}).get("id")
    missing: list[str] = []
    if runtime_id:
        found = conn.execute(
            "SELECT 1 FROM runtimes WHERE id=?", (runtime_id,)).fetchone()
        if found is None:
            missing.append(f"runtime:{runtime_id}")
    return {
        "ok": True,
        "export": export,
        "manifest": manifest,
        "charter": charter,
        "files": {item["path"]: files[f"files/{item['path']}"]
                  for item in manifest.get("files", [])},
        "dependencies": {"runtime_id": runtime_id, "missing": missing},
        "warnings": [f"missing dependency: {m}" for m in missing],
    }


def import_role(conn: sqlite3.Connection, path: Path, *,
                mode: str = "new_role", slug: str | None = None,
                allow_charter_change: bool = False) -> dict:
    preview = preview_import(conn, Path(path))
    export = preview["export"]
    source = str(export["slug"])
    version = int(export["version"])
    note = f"imported from {source}@v{version}@{str(export.get('content_hash') or '')[:12]}"
    definition = (preview["charter"].get("definition") or {})
    if mode == "new_role":
        target = roles.validate_slug(slug or f"{source}-import-{new_id()[:4]}")
        if roles.get(conn, target) is not None:
            raise ImportError_(f"role already exists: {target}")
        roles.create(conn, slug=target, display_name=str(
            export.get("display_name") or target), definition=definition)
        home = role_space.role_home(target)
        for rel, data in preview["files"].items():
            role_space._write_role_file(home, rel, data)
        row = role_space.snapshot(
            conn, target, kind="evolution", source="import", status="active",
            charter_definition=definition, note=note)
        _import_notes(conn, target, preview, row["version"])
        if preview["dependencies"]["missing"]:
            conn.execute("UPDATE roles SET enabled=0 WHERE slug=?", (target,))
            conn.commit()
        return {"mode": mode, "role": target, "version": row["version"],
                "created": True, "dependencies": preview["dependencies"],
                "warnings": preview["warnings"]}
    if mode != "add_version":
        raise ImportError_(f"unknown import mode: {mode}")
    target = roles.validate_slug(slug or source)
    if roles.get(conn, target) is None:
        raise ImportError_(f"role not found: {target}")
    current_digest = role_space.charter_digest(conn, target)
    if (export.get("charter_digest") and export["charter_digest"] != current_digest
            and not allow_charter_change):
        raise ImportError_(
            "charter differs; pass allow_charter_change or import as new_role")
    charter_definition = definition if allow_charter_change else None
    row = role_space.snapshot(
        conn, target, kind="evolution", source="import", status="candidate",
        charter_definition=charter_definition,
        files=list(preview["files"]), file_overrides=preview["files"],
        note=note, force=True)
    _import_notes(conn, target, preview, row["version"])
    return {"mode": mode, "role": target, "version": row["version"],
            "created": True, "dependencies": preview["dependencies"],
            "warnings": preview["warnings"]}


def _import_notes(conn: sqlite3.Connection, role: str, preview: dict,
                  local_version: int) -> None:
    now = now_iso()
    for card in preview["manifest"].get("knowledge_cards", []):
        rel = card["path"]
        data = preview["files"].get(rel)
        if data is None:
            continue
        note_id = "N-" + hashlib.sha1(
            f"{role}|{rel}".encode("utf-8")).hexdigest()[:20]
        conn.execute(
            "INSERT INTO notes (id, role_slug, path, content, created_at, kind, "
            "scope, origin, source_role_version, last_modified_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_slug, path) DO UPDATE SET content=excluded.content, "
            "origin='system', source_role_version=excluded.source_role_version, "
            "last_modified_by='system', updated_at=excluded.updated_at",
            (note_id, role, rel, data.decode("utf-8", errors="replace"), now,
             "personal_lesson", "role", "system", local_version, "system", now))
    conn.commit()
