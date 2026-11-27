"""资源 supervisor（M1）：超限杀进程树并失败对应 run。"""
from __future__ import annotations

import subprocess
import sys
import time

from anvil.db import connect
from anvil.services import runs as runs_service
from anvil.services import sandbox


def test_supervisor_kills_resource_breach(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        run = runs_service.create_run(conn, "qa-role", task_id=None)
        runs_service.set_status(conn, run["id"], "running")
        iid = sandbox.create_instance(
            conn, role_slug="qa-role", scope_kind="role_session",
            scope_id="k", backend="host",
            rss_limit_mib=1, pids_limit=256,
            workspace_path=str(tmp_path))
        sandbox.bind_instance(
            conn, iid, pid=proc.pid, native_handle=f"pid:{proc.pid}")
        runs_service.set_sandbox_instance(conn, run["id"], iid)

        # 刚启动的 Python 进程 RSS 可能还是 0；等它真正分配内存再采样。
        from anvil.sandbox import monitor as _monitor
        deadline = time.time() + 5
        while time.time() < deadline:
            sample = _monitor.sample_tree(proc.pid)
            if sample and sample["rss_mib"] > 1:
                break
            time.sleep(0.05)
        assert sandbox.sample_active(conn) == 1

        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "killed"
        assert row["exit_reason"] == "resource_rss"
        rr = conn.execute(
            "SELECT status, failure_class FROM runs WHERE id=?",
            (run["id"],)).fetchone()
        assert rr["status"] == "failed"
        assert (rr["failure_class"] or "").startswith("sandbox_")
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        conn.close()


def test_supervisor_keeps_instance_within_limits(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        iid = sandbox.create_instance(
            conn, role_slug="qa-role", scope_kind="role_session",
            scope_id="k2", backend="host",
            rss_limit_mib=4096, pids_limit=256,
            workspace_path=str(tmp_path))
        sandbox.bind_instance(
            conn, iid, pid=proc.pid, native_handle=f"pid:{proc.pid}")
        assert sandbox.sample_active(conn) == 0
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "running"
        assert row["peak_json"] != "{}"
    finally:
        if proc.poll() is None:
            proc.kill()
        conn.close()
