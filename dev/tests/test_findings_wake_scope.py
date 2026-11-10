"""Findings 复评只唤醒/创建 task-level run，不能 resume 切片 step run。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anvil import config
from anvil.db import connect
from anvil.util import now_iso
from anvil.api import findings as findings_api

pytest.importorskip("anvil.graph")


def test_findings_wake_does_not_resume_slice_run(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO tasks (id,title,status,created_at,updated_at) "
            "VALUES ('T1','t','in_progress',?,?)", (now, now))
        conn.execute(
            "INSERT INTO runs (id,role_slug,task_id,status,slice_key,step_id,started_at) "
            "VALUES ('R-SLICE','dev','T1','suspended','A','A.verify',?)", (now,))
        conn.commit()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                db_path=db_path, orchestrator=None)))
        findings_api._wake_finding_reviewer(request, conn, "T1", "dev", "复核 finding")
        runs = conn.execute(
            "SELECT id, status, slice_key FROM runs WHERE task_id='T1' ORDER BY started_at"
        ).fetchall()
        assert {r["id"] for r in runs} == {"R-SLICE", runs[-1]["id"]}
        slice_run = next(r for r in runs if r["id"] == "R-SLICE")
        assert slice_run["status"] == "suspended"
        new_run = next(r for r in runs if r["id"] != "R-SLICE")
        assert new_run["slice_key"] is None
    finally:
        conn.close()
