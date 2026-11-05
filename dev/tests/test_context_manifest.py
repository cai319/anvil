"""Context manifest：CIC 记录了哪些来源，且任务 B 不含任务 A 的 task-scoped 来源。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import context_manifest, memory
from anvil.services.daemon import DaemonManager
from anvil.util import now_iso


@pytest.fixture
def conn(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "agents" / "dev").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    c = connect(str(tmp_path / "t.db"))
    now = now_iso()
    for tid, title in (("TA", "Auth JWT audit"), ("TB", "Admin JWT console")):
        c.execute(
            "INSERT INTO tasks (id,title,status,tags_json,repo,created_at,updated_at) "
            "VALUES (?,?,'in_progress','[\"jwt\"]','openssl',?,?)",
            (tid, title, now, now))
    c.commit()
    memory.write_note(
        c, "dev", "knowledge/TA/snapshot.md", "TASNAPSHOTUNIQUE jwt strict",
        kind="task_snapshot", scope="task", task_id="TA")
    memory.write_note(
        c, "dev", "vulns/jwt.md", "ROLEPATTERNUNIQUE jwt algorithm confusion",
        kind="vulnerability_pattern", scope="role", tags=["jwt"], repo="openssl")
    yield c
    c.close()


def test_cic_manifest_records_sources_without_cross_task_leak(conn, tmp_path):
    mgr = DaemonManager(str(tmp_path / "t.db"), enabled=False)
    text_a = mgr._build_cic(conn, "dev", "TA", run_id="rA")
    text_b = mgr._build_cic(conn, "dev", "TB", run_id="rB")
    assert "TASNAPSHOTUNIQUE" in text_a
    assert "TASNAPSHOTUNIQUE" not in text_b

    a = context_manifest.list_for_run(conn, "rA")[0]
    b = context_manifest.list_for_run(conn, "rB")[0]
    assert a["kind"] == "cic" and a["task_id"] == "TA"
    assert b["kind"] == "cic" and b["task_id"] == "TB"
    assert a["total_chars"] > 0 and b["total_chars"] > 0
    assert any("TA" in (blk.get("source_ids") or []) for blk in a["blocks"])
    assert all("TA" not in (blk.get("source_ids") or []) for blk in b["blocks"])
    # 角色级知识可以跨任务出现，但 manifest 必须标注 scope=role。
    assert any(blk.get("scope") in ("role", "role_or_task") for blk in b["blocks"])
