"""Role space versions: evolution snapshots, compressed CAS, manifest integrity.

v1.50 P0 implementation. Files are the source of truth for role space; the CAS
stores compressed content-addressed blobs, and each version has a manifest with
per-file hashes. The DB row only indexes the manifest (no content mirror).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import zlib
from pathlib import Path
from typing import Iterable

from .. import config
from ..util import jloads, now_iso

CAS_VERSION = "v1"
CAS_DIRNAME = ".anvil/space-cas"
VERSIONS_DIRNAME = ".anvil/space-versions"
MANIFEST_NAME = "manifest.json"
MANIFEST_HASH_NAME = "manifest.sha256"

# 版本化面：只含 Strategy + Experience；不含 logs/scratch/任务快照/MEMORY.md。
DEFAULT_VERSIONED_PATHS = (
    "instruction.md",
    "PRINCIPLES.md",
)
DEFAULT_VERSIONED_DIRS = (
    "playbooks",
    "notes/playbooks",
    "notes/memory/cards",
    "notes/knowledge",
)


def _canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def role_home(slug: str) -> Path:
    return config.AGENTS_DIR / slug


def cas_dir(slug: str) -> Path:
    return role_home(slug) / CAS_DIRNAME / CAS_VERSION


def versions_dir(slug: str) -> Path:
    return role_home(slug) / VERSIONS_DIRNAME


def version_dir(slug: str, version: int) -> Path:
    return versions_dir(slug) / f"v{int(version)}"


def _role_row(conn: sqlite3.Connection, slug: str):
    return conn.execute(
        "SELECT slug, display_name, definition_json FROM roles WHERE slug=?",
        (slug,)).fetchone()


def _charter_block(instruction: str) -> str:
    begin = "<!-- ANVIL:CHARTER:BEGIN -->"
    end = "<!-- ANVIL:CHARTER:END -->"
    if begin in instruction and end in instruction:
        return instruction.split(begin, 1)[1].split(end, 1)[0].strip()
    # 未标记时整份 instruction 视为 Charter（RSI 不得编辑）。
    return instruction.strip()


def charter_digest(conn: sqlite3.Connection, slug: str,
                   definition: dict | None = None,
                   instruction: str | None = None) -> str:
    """Charter 摘要：definition 规范化 + instruction 的 Charter 段。"""
    row = _role_row(conn, slug)
    if row is None:
        raise ValueError(f"role not found: {slug}")
    definition = definition if definition is not None else jloads(row["definition_json"], {})
    if instruction is None:
        path = role_home(slug) / "instruction.md"
        try:
            instruction = path.read_text(encoding="utf-8")
        except OSError:
            instruction = ""
    tools_row = conn.execute(
        "SELECT preset, max_tool_tokens, allowlist_json, skills_json, mcp_json "
        "FROM role_tools WHERE role_slug=?", (slug,)).fetchone()
    tools = dict(tools_row) if tools_row is not None else {}
    payload = _canonical({"definition": definition, "tools": tools}) + \
        "\n" + _charter_block(instruction)
    return "sha256:" + _sha256_text(payload)


def _iter_versioned_files(slug: str) -> Iterable[str]:
    home = role_home(slug)
    for rel in DEFAULT_VERSIONED_PATHS:
        if (home / rel).is_file():
            yield rel
    for dirname in DEFAULT_VERSIONED_DIRS:
        root = home / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                yield str(path.relative_to(home)).replace("\\", "/")


def _read_relative(home: Path, rel: str) -> bytes | None:
    target = (home / rel).resolve()
    try:
        target.relative_to(home.resolve())
    except ValueError:
        raise ValueError(f"path escapes role home: {rel}")
    if not target.is_file():
        return None
    return target.read_bytes()


def _compress(data: bytes) -> tuple[bytes, str, int]:
    packed = zlib.compress(data, 6)
    if len(packed) < int(len(data) * 0.9):
        return packed, "zlib", len(packed)
    return data, "raw", len(data)


def _write_blob(cas: Path, data: bytes) -> dict:
    stored, encoding, compressed_size = _compress(data)
    stored_sha = _sha256_bytes(stored)
    cas.mkdir(parents=True, exist_ok=True)
    path = cas / f"{stored_sha}.z"
    if not path.exists():
        path.write_bytes(stored)
    return {
        "sha256": _sha256_bytes(data),
        "size": len(data),
        "stored_sha256": stored_sha,
        "encoding": encoding,
        "compressed_size": compressed_size,
    }


def _read_blob(cas: Path, item: dict) -> bytes:
    path = cas / f"{item['stored_sha256']}.z"
    stored = path.read_bytes()
    if _sha256_bytes(stored) != item["stored_sha256"]:
        raise ValueError(f"blob hash mismatch: {path.name}")
    encoding = item.get("encoding") or "raw"
    if encoding == "zlib":
        try:
            data = zlib.decompress(stored)
        except zlib.error as exc:
            raise ValueError(f"blob decompress failed: {path.name}") from exc
    elif encoding == "raw":
        data = stored
    else:
        raise ValueError(f"unknown blob encoding: {encoding}")
    if _sha256_bytes(data) != item["sha256"]:
        raise ValueError(f"content hash mismatch: {path.name}")
    return data


def _content_hash(files: list[dict], cards: list[dict], charter: str) -> str:
    payload = {
        "files": sorted((f["path"], f["sha256"]) for f in files),
        "cards": sorted((c["id"], c["sha256"]) for c in cards),
        "charter_digest": charter,
    }
    return "sha256:" + _sha256_text(_canonical(payload))


def _manifest_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return "sha256:" + _sha256_text(_canonical(body))


def _row_dict(row) -> dict:
    d = dict(row)
    d["definition"] = jloads(d.pop("definition_json", None), {})
    return d


def latest_for_kind(conn: sqlite3.Connection, slug: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND kind=? "
        "AND status != 'deleted' ORDER BY version DESC LIMIT 1",
        (slug, kind)).fetchone()
    return _row_dict(row) if row else None


def snapshot(conn: sqlite3.Connection, slug: str, *, kind: str,
             source: str, status: str = "archived",
             files: list[str] | None = None,
             note: str | None = None,
             parent_version: int | None = None,
             charter_definition: dict | None = None,
             charter: str | None = None,
             eval_run_id: str | None = None,
             file_overrides: dict[str, bytes] | None = None,
             force: bool = False) -> dict:
    """Create an immutable role-space snapshot and its manifest/CAS blobs.

    Returns the version row plus `created` and `manifest_hash`. A no-op content
    hash returns the latest matching version with `created=False`.
    """
    if kind not in ("evolution", "config"):
        raise ValueError(f"invalid kind: {kind}")
    row = _role_row(conn, slug)
    if row is None:
        raise ValueError(f"role not found: {slug}")
    definition = charter_definition if charter_definition is not None else jloads(
        row["definition_json"], {})
    home = role_home(slug)
    rels = list(files) if files is not None else sorted(set(_iter_versioned_files(slug)))
    # Explicit paths are still constrained to the role home.
    file_items: list[dict] = []
    card_items: list[dict] = []
    cas = cas_dir(slug)
    for rel in rels:
        data = (file_overrides or {}).get(rel)
        if data is None:
            data = _read_relative(home, rel)
        if data is None:
            continue
        blob = _write_blob(cas, data)
        item = {
            "path": rel,
            **blob,
            "mode": "100644",
            "origin": "experience" if rel.startswith("notes/") else "strategy",
        }
        file_items.append(item)
        if rel.startswith("notes/memory/cards/") or rel.startswith("notes/knowledge/"):
            card_items.append({
                "id": rel,
                "path": rel,
                "sha256": blob["sha256"],
                "origin": item["origin"],
            })
    charter = charter or charter_digest(
        conn, slug, definition=definition,
        instruction=(home / "instruction.md").read_text(encoding="utf-8")
        if (home / "instruction.md").is_file() else "")
    content = _content_hash(file_items, card_items, charter)

    existing = None if force else conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND kind=? AND content_hash=? "
        "AND status != 'deleted' ORDER BY version DESC LIMIT 1",
        (slug, kind, content)).fetchone()
    if existing is not None:
        d = _row_dict(existing)
        d["created"] = False
        d["manifest_hash"] = d.get("space_manifest_hash")
        return d

    row_max = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM role_versions WHERE slug=?",
        (slug,)).fetchone()
    version = int(row_max["v"]) + 1
    created_at = now_iso()
    if kind == "evolution" and status == "active":
        conn.execute(
            "UPDATE role_versions SET status='archived' "
            "WHERE slug=? AND kind='evolution' AND status='active'", (slug,))
    manifest = {
        "schema_version": 1,
        "role": slug,
        "version": version,
        "kind": kind,
        "source": source,
        "status": status,
        "parent_version": parent_version,
        "created_at": created_at,
        "charter_digest": charter,
        "content_hash": content,
        "files": file_items,
        "knowledge_cards": card_items,
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    vdir = version_dir(slug, version)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    (vdir / MANIFEST_HASH_NAME).write_text(
        manifest["manifest_hash"] + "\n", encoding="utf-8")
    manifest_path = str(vdir / MANIFEST_NAME)
    conn.execute(
        "INSERT INTO role_versions (slug, version, display_name, definition_json, "
        "source, created_at, kind, status, parent_version, space_manifest_path, "
        "space_manifest_hash, charter_digest, eval_run_id, note, content_hash, "
        "updated_at, coalesced_count, integrity_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, version, row["display_name"], json.dumps(definition, ensure_ascii=False),
         source, created_at, kind, status, parent_version, manifest_path,
         manifest["manifest_hash"], charter, eval_run_id, note, content,
         created_at, 1, "unverified"))
    conn.commit()
    created = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?",
        (slug, version)).fetchone()
    d = _row_dict(created)
    d["created"] = True
    d["manifest_hash"] = manifest["manifest_hash"]
    return d


def snapshot_config(conn: sqlite3.Connection, slug: str, *,
                    source: str = "update", note: str | None = None) -> dict:
    """Manual config revision: hidden from the evolution view, never active."""
    return snapshot(conn, slug, kind="config", source=source,
                    status="archived", note=note)


def get_manifest(conn: sqlite3.Connection, slug: str, version: int) -> dict:
    row = conn.execute(
        "SELECT space_manifest_path FROM role_versions WHERE slug=? AND version=?",
        (slug, version)).fetchone()
    if row is None or not row["space_manifest_path"]:
        raise ValueError(f"manifest not found: {slug}@{version}")
    return json.loads(Path(row["space_manifest_path"]).read_text(encoding="utf-8"))


def materialize_version(conn: sqlite3.Connection, slug: str, version: int,
                        target_home: Path) -> list[str]:
    """Restore a version's Strategy/Experience files into a disposable home.

    Used by the shadow evaluator; never writes the real role home. Integrity is
    checked per blob before the file is written.
    """
    manifest = get_manifest(conn, slug, version)
    cas = cas_dir(slug)
    restored: list[str] = []
    for item in manifest.get("files", []):
        data = _read_blob(cas, item)
        _write_role_file(target_home, item["path"], data)
        restored.append(str(item["path"]))
    return restored


def verify_version(conn: sqlite3.Connection, slug: str, version: int) -> dict:
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?",
        (slug, version)).fetchone()
    if row is None:
        return {"ok": False, "reason": "version not found", "checked": 0}
    path = row["space_manifest_path"]
    ok = False
    checked = 0
    reason = ""
    try:
        if not path:
            raise ValueError("no manifest")
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        expected = _manifest_hash(manifest)
        if expected != manifest.get("manifest_hash") or expected != row["space_manifest_hash"]:
            raise ValueError("manifest hash mismatch")
        cas = cas_dir(slug)
        for item in manifest.get("files", []):
            _read_blob(cas, item)
            checked += 1
        ok = True
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reason = str(exc)
    conn.execute(
        "UPDATE role_versions SET integrity_status=?, last_verified_at=? "
        "WHERE slug=? AND version=?",
        ("ok" if ok else "corrupt", now_iso(), slug, version))
    conn.commit()
    return {"ok": ok, "checked": checked, "reason": reason}


def list_versions(conn: sqlite3.Connection, slug: str, *, kind: str | None = None,
                  status: str | None = None, limit: int = 50,
                  offset: int = 0) -> list[dict]:
    clauses = ["slug=?", "status != 'deleted'"]
    params: list[object] = [slug]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if status:
        clauses.append("status=?")
        params.append(status)
    sql = (f"SELECT * FROM role_versions WHERE {' AND '.join(clauses)} "
           f"ORDER BY version DESC LIMIT ? OFFSET ?")
    params.extend([max(1, int(limit)), max(0, int(offset))])
    rows = conn.execute(sql, params).fetchall()
    return [_row_dict(r) for r in rows]


CARD_DIRS = ("notes/memory/cards/", "notes/knowledge/")


def _is_card(rel: str) -> bool:
    return rel.startswith(CARD_DIRS)


def _note_row(conn: sqlite3.Connection, slug: str, rel: str):
    return conn.execute(
        "SELECT origin, last_modified_by FROM notes WHERE role_slug=? AND path=?",
        (slug, rel)).fetchone()


def _write_role_file(home: Path, rel: str, data: bytes) -> None:
    target = (home / rel).resolve()
    try:
        target.relative_to(home.resolve())
    except ValueError:
        raise ValueError(f"path escapes role home: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def _current_cards(slug: str) -> set[str]:
    home = role_home(slug)
    cards: set[str] = set()
    for rel in _iter_versioned_files(slug):
        if _is_card(rel):
            cards.add(rel)
    return cards


def switch(conn: sqlite3.Connection, slug: str, version: int, *,
           restore_charter: bool = False,
           preserve_human_modified: bool = True) -> dict:
    """Restore an evolution snapshot (files + role knowledge cards)."""
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?",
        (slug, version)).fetchone()
    if row is None:
        raise ValueError(f"version not found: {slug}@{version}")
    if row["kind"] != "evolution":
        raise ValueError("only evolution versions can be switched")
    checked = verify_version(conn, slug, version)
    if not checked.get("ok"):
        raise ValueError(f"version is corrupt: {checked.get('reason')}")
    manifest = get_manifest(conn, slug, version)
    home = role_home(slug)
    cas = cas_dir(slug)
    manifest_cards = {c["path"] for c in manifest.get("knowledge_cards", [])}
    restored_cards = 0
    preserved_cards = 0
    archived_cards = 0
    partial = False
    for item in manifest.get("files", []):
        rel = item["path"]
        data = _read_blob(cas, item)
        if _is_card(rel) and preserve_human_modified:
            current = home / rel
            if current.is_file():
                current_hash = _sha256_bytes(current.read_bytes())
                note = _note_row(conn, slug, rel)
                human = bool(note and (
                    note["last_modified_by"] == "human"
                    or note["origin"] == "human"))
                if human and current_hash != item["sha256"]:
                    preserved_cards += 1
                    partial = True
                    continue
        _write_role_file(home, rel, data)
        if _is_card(rel):
            restored_cards += 1
    for rel in _current_cards(slug) - manifest_cards:
        note = _note_row(conn, slug, rel)
        rsi_owned = bool(note and note["origin"] == "rsi"
                         and note["last_modified_by"] != "human")
        if not rsi_owned:
            preserved_cards += 1
            continue
        src = home / rel
        dest = home / "notes" / "archive" / f"switch-v{version}" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        archived_cards += 1
    if restore_charter:
        conn.execute(
            "UPDATE roles SET definition_json=?, updated_at=? WHERE slug=?",
            (row["definition_json"], now_iso(), slug))
    conn.execute(
        "UPDATE role_versions SET status='archived' "
        "WHERE slug=? AND kind='evolution' AND status='active'", (slug,))
    conn.execute(
        "UPDATE role_versions SET status='active' WHERE slug=? AND version=?",
        (slug, version))
    from . import ledger
    ledger.emit(
        conn, "rsi.version_switched", actor_type="human", actor="human",
        payload={"role": slug, "version": version,
                 "restored_cards": restored_cards,
                 "preserved_cards": preserved_cards,
                 "archived_cards": archived_cards,
                 "partial_restore": partial},
        source="rsi_switch",
        source_id=_stable_event_id(f"switch|{slug}|{version}|{now_iso()}"))
    conn.commit()
    return {
        "role": slug,
        "version": version,
        "restored_cards": restored_cards,
        "preserved_cards": preserved_cards,
        "archived_cards": archived_cards,
        "partial_restore": partial,
    }


def preview_switch(conn: sqlite3.Connection, slug: str, version: int) -> dict:
    """Read-only diff for applying an evolution version to the current space."""
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?",
        (slug, int(version))).fetchone()
    if row is None:
        raise ValueError(f"version not found: {slug}@{version}")
    if row["kind"] != "evolution" or row["status"] == "deleted":
        raise ValueError("only non-deleted evolution versions can be applied")
    manifest = get_manifest(conn, slug, int(version))
    if _manifest_hash(manifest) != manifest.get("manifest_hash") or \
            manifest.get("manifest_hash") != row["space_manifest_hash"]:
        raise ValueError("manifest hash mismatch")
    home = role_home(slug)
    cas = cas_dir(slug)
    files: list[dict] = []
    restored_cards = 0
    preserved_human: list[str] = []
    target_cards: set[str] = set()
    for item in manifest.get("files", []):
        rel = item["path"]
        _read_blob(cas, item)  # verify content hash without writing
        if _is_card(rel):
            target_cards.add(rel)
        current = home / rel
        current_hash = _sha256_bytes(current.read_bytes()) if current.is_file() else None
        status = ("added" if current_hash is None else
                  "unchanged" if current_hash == item["sha256"] else "modified")
        preserved = False
        if _is_card(rel) and current_hash is not None and current_hash != item["sha256"]:
            note = _note_row(conn, slug, rel)
            if note and (note["last_modified_by"] == "human"
                         or note["origin"] == "human"):
                preserved = True
                preserved_human.append(rel)
        files.append({
            "path": rel,
            "status": status,
            "target_sha256": item["sha256"],
            "current_sha256": current_hash,
            "preserved_human": preserved,
        })
        if _is_card(rel) and not preserved:
            restored_cards += 1
    archive: list[str] = []
    for rel in _current_cards(slug) - target_cards:
        note = _note_row(conn, slug, rel)
        rsi_owned = bool(note and note["origin"] == "rsi"
                         and note["last_modified_by"] != "human")
        if rsi_owned:
            archive.append(rel)
        else:
            preserved_human.append(rel)
    active = conn.execute(
        "SELECT version FROM role_versions WHERE slug=? AND kind='evolution' "
        "AND status='active' ORDER BY version DESC LIMIT 1", (slug,)).fetchone()
    role = _role_row(conn, slug)
    current_def = jloads(role["definition_json"], {}) if role else {}
    target_def = jloads(row["definition_json"], {})
    ws = working_set(conn, slug)
    return {
        "role": slug,
        "active_version": int(active["version"]) if active else None,
        "target_version": int(version),
        "target_source": row["source"],
        "target_created_at": row["created_at"],
        "files": files,
        "knowledge": {
            "restore": restored_cards,
            "archive": archive,
            "preserve_human": sorted(set(preserved_human)),
        },
        "working_set": {"count": ws["count"], "hash": ws["hash"]},
        "charter_changed": _canonical(current_def) != _canonical(target_def),
        "restore_charter_default": False,
        "manifest_hash": row["space_manifest_hash"],
    }


def restore_config(conn: sqlite3.Connection, slug: str, version: int, *,
                   dry_run: bool = False) -> dict:
    """Restore a config revision (definition + captured strategy files + tools).

    Legacy revisions without a manifest/tools snapshot restore only what they
    captured; the response carries explicit warnings instead of pretending a
    full restore.
    """
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?",
        (slug, int(version))).fetchone()
    if row is None:
        raise ValueError(f"version not found: {slug}@{version}")
    if row["kind"] != "config":
        raise ValueError("only config revisions can be restored here; "
                         "evolution versions use switch")
    if row["status"] == "deleted":
        raise ValueError("version is deleted")
    warnings: list[str] = []
    files: list[str] = []
    manifest = None
    if row["space_manifest_path"]:
        try:
            manifest = get_manifest(conn, slug, int(version))
        except (OSError, ValueError) as exc:
            raise ValueError(f"manifest unreadable: {exc}") from exc
        checked = verify_version(conn, slug, int(version))
        if not checked.get("ok"):
            raise ValueError(f"version is corrupt: {checked.get('reason')}")
        files = [item["path"] for item in manifest.get("files", [])]
    else:
        warnings.append("legacy revision: only definition_json is restorable")
    tools = jloads(row["role_tools_json"], None) if row["role_tools_json"] else None
    if tools is None:
        warnings.append("no role_tools snapshot: tools/catalog left unchanged")
    if dry_run:
        return {"role": slug, "version": int(version), "dry_run": True,
                "restored_files": files, "tools_restored": tools is not None,
                "warnings": warnings}
    home = role_home(slug)
    cas = cas_dir(slug)
    for item in (manifest or {}).get("files", []):
        data = _read_blob(cas, item)
        _write_role_file(home, item["path"], data)
    definition = jloads(row["definition_json"], {})
    conn.execute(
        "UPDATE roles SET display_name=?, definition_json=?, updated_at=? "
        "WHERE slug=?",
        (row["display_name"], row["definition_json"], now_iso(), slug))
    if "avatar" in definition:
        conn.execute("UPDATE roles SET avatar=? WHERE slug=?",
                     (definition.get("avatar"), slug))
    if isinstance(tools, dict):
        now = now_iso()
        conn.execute(
            "INSERT INTO role_tools (role_slug, preset, max_tool_tokens, "
            "allowlist_json, skills_json, mcp_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(role_slug) DO UPDATE SET "
            "preset=excluded.preset, max_tool_tokens=excluded.max_tool_tokens, "
            "allowlist_json=excluded.allowlist_json, "
            "skills_json=excluded.skills_json, mcp_json=excluded.mcp_json, "
            "updated_at=excluded.updated_at",
            (slug, tools.get("preset"), tools.get("max_tool_tokens"),
             tools.get("allowlist_json") or "[]", tools.get("skills_json") or "[]",
             tools.get("mcp_json") or "[]", tools.get("created_at") or now, now))
    from . import ledger
    ledger.emit(
        conn, "role.config_restored", actor_type="human", actor="human",
        payload={"role": slug, "version": int(version),
                 "restored_files": files,
                 "tools_restored": isinstance(tools, dict),
                 "warnings": warnings},
        source="role_config_restore",
        source_id=_stable_event_id(f"restore-config|{slug}|{version}|{now_iso()}"))
    conn.commit()
    return {"role": slug, "version": int(version), "dry_run": False,
            "restored_files": files, "tools_restored": isinstance(tools, dict),
            "warnings": warnings}


def _stable_event_id(text: str) -> int:
    return int.from_bytes(hashlib.sha1(text.encode("utf-8")).digest()[:8],
                          "big", signed=True)


def working_set(conn: sqlite3.Connection, slug: str) -> dict:
    """Unversioned RSI knowledge deltas on top of the active evolution baseline.

    Daily harvest writes origin=rsi cards/playbooks; they are not part of the
    active manifest until the next promotion snapshots them. Runs record this
    hash so evaluation can separate version effect from working-set effect.
    """
    active = conn.execute(
        "SELECT version, space_manifest_path FROM role_versions "
        "WHERE slug=? AND kind='evolution' AND status='active' "
        "ORDER BY version DESC LIMIT 1", (slug,)).fetchone()
    base_version = int(active["version"]) if active else None
    manifest_cards: set[str] = set()
    if active and active["space_manifest_path"]:
        try:
            manifest = json.loads(
                Path(active["space_manifest_path"]).read_text(encoding="utf-8"))
            manifest_cards = {c["path"] for c in manifest.get("knowledge_cards", [])}
        except (OSError, json.JSONDecodeError):
            manifest_cards = set()
    cards = []
    home = role_home(slug)
    for rel in sorted(_current_cards(slug) - manifest_cards):
        note = _note_row(conn, slug, rel)
        if note is None or note["origin"] != "rsi":
            continue
        path = home / rel
        if not path.is_file():
            continue
        cards.append({
            "path": rel,
            "sha256": _sha256_bytes(path.read_bytes()),
            "human_modified": note["last_modified_by"] == "human",
        })
    payload = {
        "base_version": base_version,
        "cards": cards,
        "count": len(cards),
    }
    payload["hash"] = "sha256:" + _sha256_text(_canonical(payload))
    return payload


def actual_performance(conn: sqlite3.Connection, slug: str, version: int) -> dict:
    runs = conn.execute(
        "SELECT id, task_id FROM runs WHERE role_slug=? AND role_space_version=?",
        (slug, version)).fetchall()
    run_ids = [str(r["id"]) for r in runs]
    task_ids = sorted({str(r["task_id"]) for r in runs if r["task_id"]})
    success = 0
    if task_ids:
        marks = ",".join("?" * len(task_ids))
        rows = conn.execute(
            f"SELECT status FROM tasks WHERE id IN ({marks})", task_ids).fetchall()
        success = sum(1 for r in rows if r["status"] == "done")
    rework = 0
    if task_ids:
        marks = ",".join("?" * len(task_ids))
        row = conn.execute(
            f"SELECT COUNT(*) FROM workflow_steps s "
            f"JOIN workflows w ON w.id=s.workflow_id "
            f"WHERE s.role_slug=? AND w.task_id IN ({marks}) "
            f"AND s.rework_count > 0", (slug, *task_ids)).fetchone()
        rework = int(row[0] if row else 0)
    tokens = 0
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        row = conn.execute(
            f"SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens"
            f"+cache_creation_tokens),0) FROM token_usage WHERE run_id IN ({marks})",
            run_ids).fetchone()
        tokens = int(row[0] if row else 0)
    return {
        "version": version,
        "runs": len(runs),
        "tasks": len(task_ids),
        "success_rate": round(success / len(task_ids), 4) if task_ids else 0.0,
        "rework_steps": rework,
        "rework_rate": round(rework / len(task_ids), 4) if task_ids else 0.0,
        "tokens": tokens,
    }


def list_versions_page(conn: sqlite3.Connection, slug: str, *,
                       kind: str | None = None, status: str | None = None,
                       source: str | None = None, limit: int = 50,
                       offset: int = 0, include_deleted: bool = False) -> dict:
    clauses = ["slug=?"]
    params: list[object] = [slug]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if status:
        clauses.append("status=?")
        params.append(status)
    if source:
        clauses.append("source=?")
        params.append(source)
    if not include_deleted:
        clauses.append("status != 'deleted'")
    where = " AND ".join(clauses)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM role_versions WHERE {where}", params).fetchone()[0])
    rows = conn.execute(
        f"SELECT * FROM role_versions WHERE {where} ORDER BY version DESC "
        f"LIMIT ? OFFSET ?",
        (*params, max(1, min(int(limit), 200)), max(0, int(offset)))).fetchall()
    items = []
    for row in rows:
        item = _row_dict(row)
        item["eval"] = None
        if row["eval_run_id"]:
            ev = conn.execute(
                "SELECT verdict, held_in_json, held_out_json FROM eval_runs WHERE id=?",
                (row["eval_run_id"],)).fetchone()
            if ev is not None:
                item["eval"] = {"verdict": ev["verdict"],
                                "run_id": row["eval_run_id"]}
        items.append(item)
    return {"items": items, "total": total, "limit": int(limit),
            "offset": int(offset)}


def ensure_baseline(conn: sqlite3.Connection, slug: str) -> dict:
    """Create the first active evolution baseline from the current role space."""
    row = conn.execute(
        "SELECT version FROM role_versions WHERE slug=? AND kind='evolution' "
        "AND status='active' ORDER BY version DESC LIMIT 1", (slug,)).fetchone()
    if row is not None:
        found = conn.execute(
            "SELECT * FROM role_versions WHERE slug=? AND version=?",
            (slug, row["version"])).fetchone()
        result = _row_dict(found)
        result["created"] = False
        return result
    if _role_row(conn, slug) is None:
        raise ValueError(f"role not found: {slug}")
    version = snapshot(
        conn, slug, kind="evolution", source="baseline", status="active",
        note="baseline: current role space", force=True)
    version["created"] = bool(version.get("created", True))
    return version


def version_summary(conn: sqlite3.Connection, slug: str) -> dict:
    kinds: dict[str, int] = {}
    statuses: dict[str, int] = {}
    sources: dict[str, int] = {}
    # Soft-deleted revisions remain auditable but must not inflate user counts.
    for row in conn.execute(
            "SELECT kind, status, source FROM role_versions WHERE slug=? "
            "AND status != 'deleted'", (slug,)).fetchall():
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        sources[row["source"]] = sources.get(row["source"], 0) + 1
    deleted = int(conn.execute(
        "SELECT COUNT(*) FROM role_versions WHERE slug=? AND status='deleted'",
        (slug,)).fetchone()[0])
    active = conn.execute(
        "SELECT version FROM role_versions WHERE slug=? AND kind='evolution' "
        "AND status='active' ORDER BY version DESC LIMIT 1", (slug,)).fetchone()
    return {
        "role": slug,
        "active_evolution": int(active["version"]) if active else None,
        "counts": {"kind": kinds, "status": statuses, "source": sources,
                   "deleted": deleted},
    }


def version_duplicates(conn: sqlite3.Connection, slug: str, *,
                       kind: str | None = None) -> dict:
    clauses = ["slug=?", "content_hash IS NOT NULL", "status != 'deleted'"]
    params: list[object] = [slug]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    rows = conn.execute(
        "SELECT version, kind, content_hash, status, created_at FROM role_versions "
        "WHERE " + " AND ".join(clauses) + " ORDER BY version", params).fetchall()
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["content_hash"], []).append(dict(row))
    dup = [{"content_hash": k, "versions": v} for k, v in groups.items()
           if len(v) > 1]
    return {"role": slug, "groups": dup, "count": len(dup)}


def dedupe_config_versions(conn: sqlite3.Connection, slug: str, *,
                           dry_run: bool = True) -> dict:
    """Soft-delete older config revisions sharing the same content hash.

    Keeps the newest row of each duplicate group (the current manual config)
    and defers to purge_versions' active/baseline/reference protections.
    """
    duplicates = version_duplicates(conn, slug, kind="config")
    stale: list[int] = []
    kept: list[int] = []
    for group in duplicates["groups"]:
        versions = sorted((int(v["version"]) for v in group["versions"]),
                          reverse=True)
        if not versions:
            continue
        kept.append(versions[0])
        stale.extend(versions[1:])
    result = purge_versions(conn, slug, stale, dry_run=dry_run)
    result.update({"kept": kept, "duplicate_groups": duplicates["count"],
                   "candidates": len(stale)})
    return result


def purge_versions(conn: sqlite3.Connection, slug: str, versions: list[int], *,
                   dry_run: bool = False) -> dict:
    deleted: list[int] = []
    skipped: list[dict] = []
    for version in versions:
        row = conn.execute(
            "SELECT * FROM role_versions WHERE slug=? AND version=?",
            (slug, int(version))).fetchone()
        if row is None:
            skipped.append({"version": int(version), "reason": "not_found"})
            continue
        if row["status"] == "active":
            skipped.append({"version": int(version), "reason": "active"})
            continue
        if row["kind"] == "evolution" and row["source"] == "baseline":
            skipped.append({"version": int(version), "reason": "baseline"})
            continue
        if row["status"] == "deleted":
            skipped.append({"version": int(version), "reason": "already_deleted"})
            continue
        referenced = conn.execute(
            "SELECT 1 FROM proposals WHERE role_slug=? AND base_version=? "
            "AND status IN ('draft','queued','evaluating','pending') LIMIT 1",
            (slug, int(version))).fetchone()
        if referenced is not None:
            skipped.append({"version": int(version), "reason": "referenced_by_candidate"})
            continue
        running_eval = conn.execute(
            "SELECT 1 FROM eval_runs WHERE role_slug=? AND "
            "(candidate_version=? OR baseline_version=?) "
            "AND status IN ('queued','running') LIMIT 1",
            (slug, int(version), int(version))).fetchone()
        if running_eval is not None:
            skipped.append({"version": int(version), "reason": "referenced_by_eval"})
            continue
        if dry_run:
            deleted.append(int(version))
            continue
        conn.execute(
            "UPDATE role_versions SET status='deleted', deleted_at=? "
            "WHERE slug=? AND version=?", (now_iso(), slug, int(version)))
        vdir = version_dir(slug, int(version))
        if vdir.is_dir():
            shutil.rmtree(vdir, ignore_errors=True)
        from . import ledger
        ledger.emit(
            conn, "role_version.purged", actor_type="human", actor="human",
            payload={"role": slug, "version": int(version),
                     "source": row["source"]})
        deleted.append(int(version))
    conn.commit()
    return {"role": slug, "dry_run": dry_run, "deleted": deleted,
            "skipped": skipped}


def gc(conn: sqlite3.Connection, slug: str, *, dry_run: bool = True) -> dict:
    """Delete CAS blobs not referenced by any non-deleted version manifest."""
    referenced: set[str] = set()
    for row in conn.execute(
            "SELECT space_manifest_path FROM role_versions "
            "WHERE slug=? AND status != 'deleted'", (slug,)).fetchall():
        path = row["space_manifest_path"]
        if not path:
            continue
        try:
            manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in manifest.get("files", []):
            if item.get("stored_sha256"):
                referenced.add(item["stored_sha256"])
    orphans = []
    cas = cas_dir(slug)
    if cas.is_dir():
        for path in cas.iterdir():
            if not path.is_file() or path.suffix != ".z":
                continue
            if path.stem not in referenced:
                orphans.append(path)
    if not dry_run:
        for path in orphans:
            path.unlink(missing_ok=True)
    return {"dry_run": dry_run, "orphans": [p.name for p in orphans],
            "deleted": 0 if dry_run else len(orphans)}
