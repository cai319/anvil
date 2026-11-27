"""持久 spawn 在 enforce 模式下经 Seatbelt 并重定向 HOME（M1 集成）。"""
from __future__ import annotations

import sys
import time

import pytest

from anvil import config
from anvil.db import connect
from anvil.sandbox import policy
from anvil.sandbox.backends.seatbelt import SeatbeltBackend, available
from anvil.services import daemon as daemon_service
from anvil.services import sandbox

pytestmark = pytest.mark.skipif(
    not available(), reason="sandbox-exec unavailable")

RUNTIME = '''
import json, os, sys, time
from pathlib import Path
Path(os.environ["HOME_PROBE"]).write_text(os.environ.get("HOME", ""))
sid = sys.argv[sys.argv.index("--session-id") + 1]
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
for _line in sys.stdin:
    pass
'''


def test_persistent_spawn_uses_seatbelt_and_workspace_home(tmp_path, monkeypatch):
    for name, value in {
        "SANDBOX_INSTANCES_DIR": tmp_path / "sandbox",
        "AGENTS_DIR": tmp_path / "agents",
        "AUTH_TOKEN_FILE": tmp_path / "auth-token",
        "SQLITE_PATH": tmp_path / "t.db",
        "GRAPH_DB_PATH": tmp_path / "graph.db",
        "DATA_DIR": tmp_path / "data",
        "TASK_WORKSPACES_DIR": tmp_path / "data" / "task-workspaces",
        "ROLE_EVAL_DIR": tmp_path / "data" / "eval",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_MODE", "enforce")

    sandbox.reset_for_tests(SeatbeltBackend())
    conn = connect(str(tmp_path / "t.db"))
    mgr = None
    try:
        ws = tmp_path / "ws"; ws.mkdir()
        probe = ws / "home.txt"
        script = tmp_path / "runtime.py"
        script.write_text(RUNTIME, encoding="utf-8")
        mgr = daemon_service.DaemonManager(str(tmp_path / "t.db"), enabled=False)
        role = mgr._role("qa-role")
        pol = policy.resolve_standard_policy()
        spec = daemon_service._SpawnSpec(
            executable=sys.executable,
            args_new=[str(script), "--session-id", "{{session_id}}"],
            args_resume=[str(script), "--resume", "{{session_id}}"],
            ctx={}, cwd=str(ws),
            env={"HOME_PROBE": str(probe)},
            workspace_path=str(ws),
            sandbox_policy=policy.as_dict(pol),
            sandbox_policy_digest=pol.policy_digest,
        )
        mgr._spawn(conn, role, spec)
        iid = role.sandbox_instance_id
        assert iid

        deadline = time.time() + 5
        while time.time() < deadline and not probe.exists():
            time.sleep(0.05)
        assert probe.exists()
        assert probe.read_text(encoding="utf-8") == str(ws / ".anvil" / "home")

        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["backend"] == "seatbelt"
        assert row["workspace_path"] == str(ws)
        assert (tmp_path / "sandbox" / "qa-role" / iid / "profile.sb").exists()

        mgr._kill_locked(role)
        row = conn.execute(
            "SELECT * FROM sandbox_instances WHERE id=?", (iid,)).fetchone()
        assert row["status"] in ("exited", "killed")
    finally:
        if mgr is not None:
            mgr.shutdown()
        sandbox.reset_for_tests(None)
        conn.close()
