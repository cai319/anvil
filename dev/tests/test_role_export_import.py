"""P5 role@version export/import tests (spec RSI-VER-010/011/EXP-*)."""
from __future__ import annotations

import json
import zipfile

import pytest

from anvil import config, db
from anvil.services import role_export, role_space, roles


def _seed(conn):
    roles.create(conn, slug="rsi-role", display_name="RSI",
                 definition={"runtime": {"id": "missing-runtime"},
                             "responsibilities": ["backend"]})
    home = config.AGENTS_DIR / "rsi-role"
    (home / "instruction.md").write_text(
        "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
        "<!-- ANVIL:STRATEGY:BEGIN -->\n策略 v1\n<!-- ANVIL:STRATEGY:END -->\n",
        encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("准则\n", encoding="utf-8")
    card = home / "notes" / "memory" / "cards" / "c1.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("经验卡\n", encoding="utf-8")
    return home, role_space.snapshot(
        conn, "rsi-role", kind="evolution", source="baseline", status="active")


def test_export_and_import_new_role_roundtrip(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    try:
        home, version = _seed(conn)
        package = role_export.export_version(
            conn, "rsi-role", version["version"], target_dir=tmp_path)
        assert package.name.startswith("role-rsi-role-v")
        assert package.suffix == ".zip"
        with zipfile.ZipFile(package) as zf:
            names = set(zf.namelist())
        assert {"export.json", "manifest.json", "manifest.sha256",
                "charter.json", "files/instruction.md"} <= names
        assert not any("logs" in n or "eval" in n for n in names)
        preview = role_export.preview_import(conn, package)
        assert preview["ok"] is True
        assert "missing-runtime" in preview["dependencies"]["missing"][0]
        result = role_export.import_role(
            conn, package, mode="new_role", slug="imported-role")
        assert result["role"] == "imported-role"
        target = config.AGENTS_DIR / "imported-role"
        assert "策略 v1" in (target / "instruction.md").read_text(encoding="utf-8")
        assert (target / "notes/memory/cards/c1.md").read_text(
            encoding="utf-8") == "经验卡\n"
        row = conn.execute(
            "SELECT enabled FROM roles WHERE slug='imported-role'").fetchone()
        assert row["enabled"] == 0
        imported = conn.execute(
            "SELECT kind, status, source, note FROM role_versions "
            "WHERE slug='imported-role' ORDER BY version DESC LIMIT 1").fetchone()
        assert imported["kind"] == "evolution"
        assert imported["status"] == "active"
        assert imported["source"] == "import"
        assert imported["note"].startswith("imported from rsi-role@")
    finally:
        conn.close()


def test_add_version_does_not_switch_live_role(tmp_path):
    conn = db.connect(str(tmp_path / "b.db"))
    try:
        home, version = _seed(conn)
        package = role_export.export_version(
            conn, "rsi-role", version["version"], target_dir=tmp_path)
        result = role_export.import_role(
            conn, package, mode="add_version", slug="rsi-role",
            allow_charter_change=True)
        assert result["version"] != version["version"]
        assert "策略 v1" in (home / "instruction.md").read_text(encoding="utf-8")
        active = conn.execute(
            "SELECT version FROM role_versions WHERE slug='rsi-role' AND "
            "kind='evolution' AND status='active'").fetchone()
        assert active["version"] == version["version"]
        candidate = conn.execute(
            "SELECT status, source FROM role_versions WHERE slug='rsi-role' "
            "AND version=?", (result["version"],)).fetchone()
        assert candidate["status"] == "candidate"
        assert candidate["source"] == "import"
    finally:
        conn.close()


def test_tampered_package_is_rejected(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    try:
        _, version = _seed(conn)
        package = role_export.export_version(
            conn, "rsi-role", version["version"], target_dir=tmp_path)
        with zipfile.ZipFile(package) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        entries["files/instruction.md"] = b"tampered"
        bad = tmp_path / "bad.anvil.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        with pytest.raises(ValueError):
            role_export.preview_import(conn, bad)
    finally:
        conn.close()


def test_zip_slip_is_rejected(tmp_path):
    conn = db.connect(str(tmp_path / "d.db"))
    try:
        bad = tmp_path / "slip.anvil.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("files/../../evil.md", b"x")
            zf.writestr("export.json", json.dumps({"schema_version": 1}))
            zf.writestr("manifest.json", "{}")
        with pytest.raises(ValueError):
            role_export.preview_import(conn, bad)
    finally:
        conn.close()
