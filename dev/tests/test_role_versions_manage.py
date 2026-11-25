"""P5 version list/summary/purge tests (spec RSI-VER-008/009)."""
from __future__ import annotations

import pytest

from anvil import config, db
from anvil.services import role_space, roles
from anvil.util import now_iso


def _seed(conn):
    roles.create(conn, slug="rsi-role", display_name="RSI",
                 definition={"runtime": {"id": "claude-code"}})
    home = config.AGENTS_DIR / "rsi-role"
    (home / "instruction.md").write_text("v1", encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("p1", encoding="utf-8")
    return home


def _snapshot(conn, *, source="baseline", status="active", note=None):
    return role_space.snapshot(
        conn, "rsi-role", kind="evolution", source=source, status=status,
        note=note)


def test_ensure_baseline_is_idempotent(tmp_path):
    conn = db.connect(str(tmp_path / "baseline.db"))
    try:
        _seed(conn)
        first = role_space.ensure_baseline(conn, "rsi-role")
        assert first["created"] is True
        assert first["kind"] == "evolution"
        assert first["status"] == "active"
        assert first["source"] == "baseline"
        manifest = role_space.get_manifest(conn, "rsi-role", first["version"])
        assert {x["path"] for x in manifest["files"]} >= {
            "instruction.md", "PRINCIPLES.md"}
        again = role_space.ensure_baseline(conn, "rsi-role")
        assert again["created"] is False
        assert again["version"] == first["version"]
        count = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug='rsi-role' "
            "AND kind='evolution' AND status='active'").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_repeated_identical_updates_do_not_grow_config_versions(tmp_path):
    conn = db.connect(str(tmp_path / "noop.db"))
    try:
        _seed(conn)
        roles.update_profile(conn, "rsi-role", "prompt", ["a"])
        before = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug='rsi-role' "
            "AND kind='config'").fetchone()[0]
        for _ in range(5):
            roles.update_profile(conn, "rsi-role", "prompt", ["a"])
        after = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug='rsi-role' "
            "AND kind='config'").fetchone()[0]
        assert after == before
        latest = conn.execute(
            "SELECT coalesced_count FROM role_versions WHERE slug='rsi-role' "
            "AND kind='config' ORDER BY version DESC LIMIT 1").fetchone()
        assert latest["coalesced_count"] >= 6
    finally:
        conn.close()


def test_dedupe_config_versions_keeps_latest(tmp_path):
    conn = db.connect(str(tmp_path / "dedupe-config.db"))
    try:
        _seed(conn)
        first = role_space.snapshot_config(conn, "rsi-role", source="update")
        for extra in (1, 2):
            version = first["version"] + extra
            conn.execute(
                "INSERT INTO role_versions (slug, version, display_name, "
                "definition_json, source, created_at, kind, status, content_hash) "
                "SELECT slug, ?, display_name, definition_json, source, created_at, "
                "kind, status, content_hash FROM role_versions WHERE slug=? AND version=?",
                (version, "rsi-role", first["version"]))
        conn.commit()
        dup = role_space.version_duplicates(conn, "rsi-role", kind="config")
        assert dup["count"] == 1
        assert len(dup["groups"][0]["versions"]) == 3
        dry = role_space.dedupe_config_versions(conn, "rsi-role", dry_run=True)
        assert dry["candidates"] == 2
        assert len(dry["deleted"]) == 2
        assert dry["kept"] == [first["version"] + 2]
        real = role_space.dedupe_config_versions(conn, "rsi-role", dry_run=False)
        assert len(real["deleted"]) == 2
        rows = conn.execute(
            "SELECT version, status FROM role_versions WHERE slug='rsi-role' "
            "AND kind='config' ORDER BY version").fetchall()
        assert [(r["version"], r["status"]) for r in rows] == [
            (first["version"] - 1, "archived"),
            (first["version"], "deleted"),
            (first["version"] + 1, "deleted"),
            (first["version"] + 2, "archived"),
        ]
        summary = role_space.version_summary(conn, "rsi-role")
        assert summary["counts"]["kind"]["config"] == 2
        assert summary["counts"]["deleted"] == 2
        listed = {v["version"] for v in roles.list_versions(conn, "rsi-role")}
        assert first["version"] not in listed
        assert first["version"] + 1 not in listed
        assert first["version"] + 2 in listed
    finally:
        conn.close()


def test_list_versions_filters_and_paginates(tmp_path):
    conn = db.connect(str(tmp_path / "list.db"))
    try:
        home = _seed(conn)
        v1 = _snapshot(conn)
        (home / "instruction.md").write_text("v2", encoding="utf-8")
        v2 = _snapshot(conn, source="rsi_promote", status="candidate")
        role_space.snapshot_config(conn, "rsi-role", source="update")
        page = role_space.list_versions_page(
            conn, "rsi-role", kind="evolution", limit=1, offset=0)
        assert page["total"] == 2
        assert len(page["items"]) == 1
        assert page["items"][0]["version"] == v2["version"]
        candidates = role_space.list_versions_page(
            conn, "rsi-role", kind="evolution", status="candidate")
        assert [x["version"] for x in candidates["items"]] == [v2["version"]]
        cfg = role_space.list_versions_page(conn, "rsi-role", kind="config")
        assert cfg["total"] >= 1
    finally:
        conn.close()


def test_summary_and_duplicates(tmp_path):
    conn = db.connect(str(tmp_path / "summary.db"))
    try:
        _seed(conn)
        _snapshot(conn)
        role_space.snapshot_config(conn, "rsi-role", source="update")
        summary = role_space.version_summary(conn, "rsi-role")
        assert summary["counts"]["kind"]["evolution"] == 1
        assert summary["counts"]["kind"]["config"] >= 1
        assert summary["active_evolution"]
        dup = role_space.version_duplicates(conn, "rsi-role", kind="config")
        assert "groups" in dup
    finally:
        conn.close()


def test_purge_protects_active_and_baseline_and_soft_deletes(tmp_path):
    conn = db.connect(str(tmp_path / "purge.db"))
    try:
        home = _seed(conn)
        v1 = _snapshot(conn)
        (home / "instruction.md").write_text("v2", encoding="utf-8")
        v2 = _snapshot(conn, source="rsi_promote", status="candidate")
        result = role_space.purge_versions(
            conn, "rsi-role", [v1["version"], v2["version"]], dry_run=False)
        assert any(x["version"] == v1["version"] for x in result["skipped"])
        assert v2["version"] in result["deleted"]
        row = conn.execute(
            "SELECT status, deleted_at FROM role_versions WHERE slug=? AND version=?",
            ("rsi-role", v2["version"])).fetchone()
        assert row["status"] == "deleted"
        assert row["deleted_at"]
        assert not role_space.version_dir("rsi-role", v2["version"]).exists()
        # duplicate purge is idempotent
        again = role_space.purge_versions(
            conn, "rsi-role", [v2["version"]], dry_run=False)
        assert again["deleted"] == []
    finally:
        conn.close()
