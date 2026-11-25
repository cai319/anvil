"""Hidden replay suite storage (v1.50 P2).

Files are the source of truth: suite index + per-item JSON. The index stores
only paths/hashes/counts; prompt/verifier bodies stay in item files and are
never written to SQLite or the diagnosis.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .. import config
from ..util import now_iso

SCHEMA_VERSION = 1
MIN_HELD_IN = 8
MIN_HELD_OUT = 8
_SUITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite_root(suite_id: str) -> Path:
    suite_id = str(suite_id or "").strip()
    if not _SUITE_RE.match(suite_id):
        raise ValueError(f"invalid suite id: {suite_id!r}")
    return config.ROLE_EVAL_DIR / "suites" / suite_id


def suite_path(suite_id: str) -> Path:
    return suite_root(suite_id) / "suite.json"


def items_dir(suite_id: str) -> Path:
    return suite_root(suite_id) / "items"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)


def _load_index(suite_id: str) -> dict:
    path = suite_path(suite_id)
    if not path.is_file():
        raise ValueError(f"suite not found: {suite_id}")
    return _read_json(path)


def create_suite(suite_id: str, *, name: str = "", description: str = "",
                 hidden: bool = True) -> dict:
    root = suite_root(suite_id)
    if suite_path(suite_id).exists():
        raise ValueError(f"suite already exists: {suite_id}")
    root.mkdir(parents=True, exist_ok=True)
    items_dir(suite_id).mkdir(parents=True, exist_ok=True)
    now = now_iso()
    index = {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite_id,
        "name": name or suite_id,
        "description": description,
        "hidden": bool(hidden),
        "status": "draft",
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "frozen_at": None,
        "digest": None,
        "items": [],
    }
    _write_json(suite_path(suite_id), index)
    return index


def _validate_item(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("item must be an object")
    split = str(item.get("split") or "")
    if split not in ("held_in", "held_out"):
        raise ValueError("item.split must be held_in|held_out")
    family = str(item.get("family") or "").strip()
    if not family:
        raise ValueError("item.family is required")
    item_id = str(item.get("item_id") or "").strip()
    if not item_id:
        raise ValueError("item.item_id is required")
    prompt = item.get("prompt")
    if not isinstance(prompt, dict):
        raise ValueError("item.prompt must be an object")
    verifier = item.get("verifier")
    if not isinstance(verifier, dict) or not isinstance(verifier.get("checks"), list):
        raise ValueError("item.verifier.checks must be a list")
    return item


def add_item(suite_id: str, item: dict) -> str:
    index = _load_index(suite_id)
    item = _validate_item(dict(item))
    item["suite_id"] = suite_id
    if any(x["item_id"] == item["item_id"] for x in index["items"]):
        raise ValueError(f"duplicate item_id: {item['item_id']}")
    opposite = "held_out" if item["split"] == "held_in" else "held_in"
    for existing in index["items"]:
        if existing["split"] == opposite and existing["family"] == item["family"]:
            raise ValueError(
                f"family leak: {item['family']} appears in both splits")
    name = _sha_text(item["item_id"])[:20] + ".json"
    path = items_dir(suite_id) / name
    _write_json(path, item)
    index["items"].append({
        "item_id": item["item_id"],
        "split": item["split"],
        "family": item["family"],
        "path": str(path.relative_to(suite_root(suite_id))),
        "sha256": _sha_file(path),
    })
    index["updated_at"] = now_iso()
    index["digest"] = None
    index["status"] = "draft"
    _write_json(suite_path(suite_id), index)
    return item["item_id"]


def _counts(index: dict) -> dict:
    counts = {"held_in": 0, "held_out": 0}
    for item in index.get("items", []):
        counts[item["split"]] = counts.get(item["split"], 0) + 1
    return counts


def _families(index: dict, split: str) -> set[str]:
    return {x["family"] for x in index.get("items", []) if x["split"] == split}


def families_disjoint(index: dict) -> bool:
    return _families(index, "held_in").isdisjoint(_families(index, "held_out"))


def ready(index: dict) -> bool:
    counts = _counts(index)
    return (counts["held_in"] >= MIN_HELD_IN
            and counts["held_out"] >= MIN_HELD_OUT
            and families_disjoint(index))


def _digest(index: dict) -> str:
    body = {
        "suite_id": index["suite_id"],
        "schema_version": index["schema_version"],
        "items": sorted(
            (x["item_id"], x["split"], x["family"], x["sha256"])
            for x in index["items"]),
    }
    return "sha256:" + _sha_text(_canonical(body))


def freeze(suite_id: str) -> str:
    index = _load_index(suite_id)
    index["digest"] = _digest(index)
    index["status"] = "frozen"
    index["frozen_at"] = now_iso()
    index["updated_at"] = index["frozen_at"]
    _write_json(suite_path(suite_id), index)
    return index["digest"]


def load_suite(suite_id: str) -> dict:
    index = _load_index(suite_id)
    root = suite_root(suite_id)
    items: list[dict] = []
    for entry in index["items"]:
        path = root / entry["path"]
        if not path.is_file():
            raise ValueError(f"missing item file: {entry['item_id']}")
        if _sha_file(path) != entry["sha256"]:
            raise ValueError(f"item hash mismatch: {entry['item_id']}")
        item = _read_json(path)
        if item.get("item_id") != entry["item_id"]:
            raise ValueError(f"item id mismatch: {entry['item_id']}")
        items.append(item)
    digest = index.get("digest") or _digest(index)
    return {
        "suite_id": suite_id,
        "name": index["name"],
        "schema_version": index["schema_version"],
        "status": index["status"],
        "version": index["version"],
        "hidden": index.get("hidden", True),
        "digest": digest,
        "counts": _counts(index),
        "ready": ready(index),
        "families_disjoint": families_disjoint(index),
        "items": items,
    }


def status(suite_id: str) -> dict:
    index = _load_index(suite_id)
    return {
        "suite_id": suite_id,
        "status": index["status"],
        "version": index["version"],
        "hidden": index.get("hidden", True),
        "digest": index.get("digest"),
        "counts": _counts(index),
        "minimum": {"held_in": MIN_HELD_IN, "held_out": MIN_HELD_OUT},
        "ready": ready(index),
        "families_disjoint": families_disjoint(index),
    }


def list_suites() -> list[dict]:
    root = config.ROLE_EVAL_DIR / "suites"
    if not root.is_dir():
        return []
    return [status(p.name) for p in sorted(root.iterdir()) if p.is_dir()]
