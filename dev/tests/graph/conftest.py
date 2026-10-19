"""Graph 测试共享 fixture：临时 DATA_DIR + fake ProcessManager + 临时 db/graph-db。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config
from anvil.db import connect, open_conn
from anvil.graph import build_graph, get_checkpointer
from anvil.graph.orchestrator import Orchestrator
from anvil.platform import factory
from anvil.services import roles as roles_service
from anvil.services import runtime as runtime_service


class FakePM:
    """记录 argv/env、可控 probe 的假进程管理器。"""

    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.alive = True
        self.exit_code = 0
        self.terminated: list[str] = []

    def spawn(self, executable: str, args: list[str], cwd: str, env: dict[str, str],
              stdout=None, stderr=None):
        self.spawned.append({"executable": executable, "args": args, "cwd": cwd, "env": env})
        self.alive = True
        return type("R", (), {"pid": 12345, "native_session_id": "pg:12345"})()

    def probe(self, native_session_id: str):
        return type(
            "R", (),
            {"alive": self.alive, "pid": 12345, "exit_code": None if self.alive else self.exit_code},
        )()

    def terminate(self, native_session_id: str) -> None:
        self.terminated.append(native_session_id)
        self.alive = False


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    (data / "agents").mkdir(parents=True, exist_ok=True)
    (data / "repos").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(config, "REPOS_DIR", data / "repos")

    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    runtime_service.register(conn, id="claude-code", display_name="Claude",
                             executable_path="/bin/echo", status="ready", capabilities={})
    roles_service.create(conn, slug="dev", display_name="Dev",
                         definition={"runtime": {"id": "claude-code"},
                                     "workspace": {"home": "agents/dev/", "repos": []},
                                     "tools": {}, "concurrency": {}})
    conn.close()

    fake = FakePM()
    monkeypatch.setattr(factory, "get_platform", lambda: (fake, None))

    graph_db_path = str(tmp_path / "graph.db")
    checkpointer = get_checkpointer(graph_db_path)
    graph = build_graph(checkpointer)
    orch = Orchestrator(graph, checkpointer)

    return {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "graph_db_path": graph_db_path,
        "graph": graph,
        "orch": orch,
        "fake": fake,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def open_run(env, run_id):
    conn = open_conn(env["db_path"])
    try:
        r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def seed_task_run(env, task_id="T-1", role="dev", title="task"):
    conn = open_conn(env["db_path"])
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, scope_paths_json, acceptance_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, title, "todo", "[]", "[]", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    conn = open_conn(env["db_path"])
    try:
        from anvil.services import runs as runs_service
        run = runs_service.create_run(conn, role, task_id)
        return run
    finally:
        conn.close()
