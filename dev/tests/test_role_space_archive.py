"""P0 role-space archive tests (spec RSI-VER-001/003/004/005/012).

TDD target: services.role_space must create compressed content-addressed snapshots
with manifest hashes, dedupe no-op content, and detect tampering.
"""
from __future__ import annotations

from anvil import config, db
from anvil.services import role_space, roles


def _seed_role(conn, slug: str = "rsi-role") -> None:
    roles.create(
        conn,
        slug=slug,
        display_name="RSI 角色",
        definition={
            "runtime": {"id": "claude-code"},
            "responsibilities": ["backend"],
            "capabilities": {"python": 0.9},
        },
    )
    home = config.AGENTS_DIR / slug
    (home / "instruction.md").write_text(
        "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
        "<!-- ANVIL:STRATEGY:BEGIN -->\n先跑验收\n<!-- ANVIL:STRATEGY:END -->\n",
        encoding="utf-8",
    )
    (home / "PRINCIPLES.md").write_text("始终附证据\n", encoding="utf-8")
    card = home / "notes" / "memory" / "cards" / "c1.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("# 经验\n先跑测试\n", encoding="utf-8")
    knowledge = home / "notes" / "knowledge" / "role-card.md"
    knowledge.parent.mkdir(parents=True, exist_ok=True)
    knowledge.write_text("# 角色知识\nOpenSSL 用例\n", encoding="utf-8")


def _snapshot(conn, slug: str = "rsi-role") -> dict:
    return role_space.snapshot(
        conn,
        slug,
        kind="evolution",
        source="baseline",
        status="active",
        note="baseline",
    )


def test_snapshot_manifest_covers_strategy_and_role_knowledge(tmp_path):
    conn = db.connect(str(tmp_path / "archive.db"))
    try:
        _seed_role(conn)
        v = _snapshot(conn)
        assert v["created"] is True
        manifest = role_space.get_manifest(conn, "rsi-role", v["version"])
        paths = {f["path"] for f in manifest["files"]}
        assert {"instruction.md", "PRINCIPLES.md"}.issubset(paths)
        assert any(p.startswith("notes/memory/cards/") for p in paths)
        assert any(p.startswith("notes/knowledge/") for p in paths)
        assert manifest["content_hash"] == v["content_hash"]
        assert manifest["charter_digest"]
        assert v["kind"] == "evolution"
        assert v["status"] == "active"
    finally:
        conn.close()


def test_cas_is_compressed_and_verifiable(tmp_path):
    conn = db.connect(str(tmp_path / "cas.db"))
    try:
        _seed_role(conn)
        v = _snapshot(conn)
        manifest = role_space.get_manifest(conn, "rsi-role", v["version"])
        cas_dir = config.AGENTS_DIR / "rsi-role" / ".anvil" / "space-cas" / "v1"
        assert cas_dir.is_dir()
        for item in manifest["files"]:
            blob = cas_dir / f"{item['stored_sha256']}.z"
            assert blob.is_file(), item["path"]
            assert item["encoding"] in {"zlib", "raw"}
            assert item["size"] >= 0
            assert item["compressed_size"] >= 0
        result = role_space.verify_version(conn, "rsi-role", v["version"])
        assert result["ok"] is True
        assert result["checked"] >= 4
    finally:
        conn.close()


def test_noop_content_does_not_create_a_version(tmp_path):
    conn = db.connect(str(tmp_path / "noop.db"))
    try:
        _seed_role(conn)
        v1 = _snapshot(conn)
        v2 = _snapshot(conn)
        assert v2["created"] is False
        assert v2["version"] == v1["version"]
        count = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug=? AND kind='evolution'",
            ("rsi-role",)).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_changed_content_shares_unchanged_blobs(tmp_path):
    conn = db.connect(str(tmp_path / "dedupe.db"))
    try:
        _seed_role(conn)
        v1 = _snapshot(conn)
        m1 = role_space.get_manifest(conn, "rsi-role", v1["version"])
        by_path_1 = {f["path"]: f["stored_sha256"] for f in m1["files"]}

        home = config.AGENTS_DIR / "rsi-role"
        (home / "instruction.md").write_text(
            "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
            "<!-- ANVIL:STRATEGY:BEGIN -->\n先跑验收，再提交\n<!-- ANVIL:STRATEGY:END -->\n",
            encoding="utf-8",
        )
        v2 = role_space.snapshot(
            conn, "rsi-role", kind="evolution", source="rsi_promote",
            status="candidate", parent_version=v1["version"])
        assert v2["created"] is True
        assert v2["version"] != v1["version"]
        m2 = role_space.get_manifest(conn, "rsi-role", v2["version"])
        by_path_2 = {f["path"]: f["stored_sha256"] for f in m2["files"]}
        assert by_path_1["PRINCIPLES.md"] == by_path_2["PRINCIPLES.md"]
        assert by_path_1["instruction.md"] != by_path_2["instruction.md"]
        cas_dir = home / ".anvil" / "space-cas" / "v1"
        blobs = {p.stem for p in cas_dir.iterdir() if p.is_file()}
        # CAS keeps old blobs (including the role-create config snapshot);
        # current version blobs are a subset and unchanged files share blobs.
        assert set(by_path_2.values()) <= blobs
        assert len(blobs) >= len(set(by_path_2.values())) + 1
    finally:
        conn.close()


def test_tampered_blob_fails_verification_and_marks_corrupt(tmp_path):
    conn = db.connect(str(tmp_path / "tamper.db"))
    try:
        _seed_role(conn)
        v = _snapshot(conn)
        manifest = role_space.get_manifest(conn, "rsi-role", v["version"])
        item = manifest["files"][0]
        blob = (config.AGENTS_DIR / "rsi-role" / ".anvil" / "space-cas" / "v1"
                / f"{item['stored_sha256']}.z")
        blob.write_bytes(b"tampered")
        result = role_space.verify_version(conn, "rsi-role", v["version"])
        assert result["ok"] is False
        row = conn.execute(
            "SELECT integrity_status FROM role_versions WHERE slug=? AND version=?",
            ("rsi-role", v["version"])).fetchone()
        assert row["integrity_status"] == "corrupt"
    finally:
        conn.close()


def test_config_revision_is_separate_from_evolution_view(tmp_path):
    conn = db.connect(str(tmp_path / "config.db"))
    try:
        _seed_role(conn)
        cfg = role_space.snapshot_config(conn, "rsi-role", source="update")
        assert cfg["kind"] == "config"
        assert cfg["status"] == "archived"
        evo = _snapshot(conn)
        evo_list = role_space.list_versions(conn, "rsi-role", kind="evolution")
        assert [x["version"] for x in evo_list] == [evo["version"]]
        assert all(x["kind"] == "evolution" for x in evo_list)
    finally:
        conn.close()


def test_manual_role_updates_write_config_revisions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    conn = db.connect(str(tmp_path / "manual.db"))
    try:
        _seed_role(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug=? AND kind='config'",
            ("rsi-role",)).fetchone()[0]
        roles.update_profile(conn, "rsi-role", "新系统提示", ["backend", "review"])
        rows = conn.execute(
            "SELECT kind, status FROM role_versions WHERE slug=? "
            "ORDER BY version DESC LIMIT 1", ("rsi-role",)).fetchone()
        assert rows["kind"] == "config"
        assert rows["status"] == "archived"
        roles.update_tools(conn, "rsi-role", "backend-default",
                           [{"server": "mcp", "tools": ["read"]}], 5000)
        after = conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug=? AND kind='config'",
            ("rsi-role",)).fetchone()[0]
        assert after > before
        # No evolution version was created by manual writes.
        assert conn.execute(
            "SELECT COUNT(*) FROM role_versions WHERE slug=? AND kind='evolution'",
            ("rsi-role",)).fetchone()[0] == 0
    finally:
        conn.close()
